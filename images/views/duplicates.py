from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods

from ..models import DismissedDuplicatePair, DuplicateImagePair, Image


def _redirect_after_pair_action(request, default="images:duplicate_image_pairs"):
    """Return to the ``next`` url the action was launched from, if it's safe."""
    next_url = request.POST.get("next")
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_url)
    return redirect(default)


@staff_member_required
def duplicate_image_pairs(request):
    """
    Staff review page for nightly-computed candidate visual-duplicate pairs.

    Pairs whose image_a or image_b has since been marked duplicate_of are
    hidden, so the list reflects only unresolved candidates even between the
    nightly scans that rebuild the table. Pairs a reviewer has dismissed as
    not-a-duplicate are also hidden by default; ``?show=dismissed`` lists them
    for restoring.
    """
    show_dismissed = request.GET.get("show") == "dismissed"
    dismissed_keys = {
        tuple(sorted(key))
        for key in DismissedDuplicatePair.objects.values_list(
            "image_a_id", "image_b_id"
        )
    }

    unresolved = (
        DuplicateImagePair.objects.select_related(
            "image_a",
            "image_b",
            "image_a__collection",
            "image_a__collection__source",
            "image_b__collection",
            "image_b__collection__source",
        )
        .filter(
            image_a__duplicate_of__isnull=True,
            image_b__duplicate_of__isnull=True,
        )
        .order_by("distance")
    )

    active, dismissed = [], []
    for pair in unresolved:
        key = tuple(sorted((pair.image_a_id, pair.image_b_id)))
        (dismissed if key in dismissed_keys else active).append(pair)

    total = DuplicateImagePair.objects.count()
    last_run = DuplicateImagePair.objects.aggregate(Max("computed_at"))[
        "computed_at__max"
    ]
    context = {
        "pairs": dismissed if show_dismissed else active,
        "show_dismissed": show_dismissed,
        "active_count": len(active),
        "dismissed_count": len(dismissed),
        "last_run": last_run,
        "resolved_count": total - len(active) - len(dismissed),
    }
    return render(request, "images/duplicate_image_pairs.html", context)


def _resolve_blockers(image):
    """
    Reasons ``image`` cannot be retired as the duplicate (losing) side of a
    pair, mirroring the chain guards in ``Image.clean()``.

    Being georeferenced is deliberately *not* a blocker: on resolve the loser's
    georeferences are transferred to the keeper first (see
    ``resolve_duplicate_pair``), so the guard in ``Image.clean()`` passes. An
    empty list means the image may be retired as the duplicate.
    """
    reasons = []
    if image.duplicate_of_id:
        reasons.append("already marked as a duplicate")
    if Image.objects.filter(duplicate_of=image).exists():
        reasons.append("other images are marked as duplicates of it")
    return reasons


@staff_member_required
def duplicate_pair_detail(request, pair_uuid):
    """
    Focused review page for a single candidate duplicate pair.

    Addressed by the pair's ``uuid`` (assigned by the nightly scan) so the url
    doesn't expose raw image ids. ``image_a``/``image_b`` are stored canonically
    (image_a_id < image_b_id), matching how dismissals are keyed.
    """
    related = []
    for side in ("image_a", "image_b"):
        related += [
            side,
            f"{side}__collection",
            f"{side}__collection__source",
            f"{side}__license",
            f"{side}__duplicate_of",
        ]
    pair = get_object_or_404(
        DuplicateImagePair.objects.select_related(*related).prefetch_related(
            "image_a__subjects", "image_b__subjects"
        ),
        uuid=pair_uuid,
    )
    image_a, image_b = pair.image_a, pair.image_b

    dismissed_pair = (
        DismissedDuplicatePair.objects.select_related("dismissed_by")
        .filter(image_a_id=image_a.id, image_b_id=image_b.id)
        .first()
    )

    for image in (image_a, image_b):
        image.hard_blockers = _resolve_blockers(image)
        image.point_georef_count = image.georeferences.count()
        image.aerial_georef_count = image.aerial_georeferences.count()
        image.total_georef_count = image.point_georef_count + image.aerial_georef_count

    context = {
        "image_a": image_a,
        "image_b": image_b,
        # Ordered pair for the side-by-side previews, plus the "keep this one"
        # choices as (keeper, loser): keeping the first retires the second as a
        # duplicate and moves its georeferences onto the first.
        "image_a_and_b": [image_a, image_b],
        "resolve_choices": [(image_a, image_b), (image_b, image_a)],
        "pair": pair,
        "distance": pair.distance,
        "computed_at": pair.computed_at,
        "same_collection": image_a.collection_id == image_b.collection_id,
        # One image has since been marked as a duplicate elsewhere.
        "resolved": bool(image_a.duplicate_of_id or image_b.duplicate_of_id),
        "dismissed_pair": dismissed_pair,
    }
    return render(request, "images/duplicate_pair_detail.html", context)


@require_http_methods(["POST"])
@staff_member_required
def resolve_duplicate_pair(request, pair_uuid):
    """
    Pick the image to keep; retire the other as a duplicate of it.

    ``duplicate_id`` (POST) is the image being retired (the loser); the other is
    the keeper. Any georeferences on the loser — point and aerial — are moved
    onto the keeper first, so no georeferencing work is stranded on a hidden
    duplicate and ``Image.clean()``'s not-georeferenced guard passes without
    being loosened.
    """
    pair = get_object_or_404(
        DuplicateImagePair.objects.select_related("image_a", "image_b"),
        uuid=pair_uuid,
    )
    image_a, image_b = pair.image_a, pair.image_b

    dup_id = (request.POST.get("duplicate_id") or "").strip()
    if dup_id == str(image_a.id):
        duplicate, keeper = image_a, image_b
    elif dup_id == str(image_b.id):
        duplicate, keeper = image_b, image_a
    else:
        messages.error(request, "Choose which image to keep.")
        return redirect("images:duplicate_pair_detail", pair_uuid=pair_uuid)

    moved = duplicate.georeferences.count() + duplicate.aerial_georeferences.count()
    try:
        with transaction.atomic():
            # Re-point each georeference one row at a time (not a bulk update)
            # so the post_save signals that refresh the tile view and
            # CollectionStats fire for the move. Validations stay attached
            # because they foreign-key the georeference, not the image.
            for georef in duplicate.georeferences.all():
                georef.image = keeper
                georef.save()
            for aerial in duplicate.aerial_georeferences.all():
                aerial.image = keeper
                aerial.save()

            duplicate.duplicate_of = keeper
            # Georeferences are now off the loser, so the not-georeferenced
            # guard passes; clean() still enforces the no-chains rules.
            duplicate.clean()
            duplicate.save()
    except ValidationError as e:
        messages.error(request, " ".join(e.messages))
        return redirect("images:duplicate_pair_detail", pair_uuid=pair_uuid)

    if moved:
        messages.success(
            request,
            f"Kept image #{keeper.id} and retired #{duplicate.id} as a "
            f"duplicate, moving {moved} georeference{'' if moved == 1 else 's'} "
            f"to #{keeper.id}.",
        )
    else:
        messages.success(
            request,
            f"Kept image #{keeper.id} and retired #{duplicate.id} as a duplicate.",
        )
    return redirect("images:duplicate_image_pairs")


@require_http_methods(["POST"])
@staff_member_required
def dismiss_duplicate_pair(request, pair_uuid):
    """Record a pair as not-a-duplicate so the scan stops surfacing it."""
    pair = get_object_or_404(DuplicateImagePair, uuid=pair_uuid)
    a_id, b_id = sorted((pair.image_a_id, pair.image_b_id))
    DismissedDuplicatePair.objects.get_or_create(
        image_a_id=a_id,
        image_b_id=b_id,
        defaults={"dismissed_by": request.user},
    )
    messages.success(request, f"Dismissed the pair #{a_id} & #{b_id}.")
    return _redirect_after_pair_action(request)


@require_http_methods(["POST"])
@staff_member_required
def restore_duplicate_pair(request, pair_uuid):
    """Undo a dismissal so the pair reappears in the review list."""
    pair = get_object_or_404(DuplicateImagePair, uuid=pair_uuid)
    a_id, b_id = sorted((pair.image_a_id, pair.image_b_id))
    DismissedDuplicatePair.objects.filter(image_a_id=a_id, image_b_id=b_id).delete()
    messages.success(request, f"Restored the pair #{a_id} & #{b_id}.")
    return _redirect_after_pair_action(request)
