from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import (
    Case,
    Count,
    Exists,
    F,
    IntegerField,
    OuterRef,
    Q,
    Subquery,
    Value,
    When,
)
from django.db.models.functions import Coalesce
from django.shortcuts import render

from images.models import Georeference, GeoreferenceValidation

QUEUE_SIZE = 50


@staff_member_required
def validation_queue(request):
    """Staff-facing queue of georeferences that most need validation.

    Surfaces the current (most recent) point georeference for each non-aerial,
    non-duplicate image. Aerial (polygon) georeferences are excluded because
    there is no front-end UI to validate them yet.

    By default the queue hides georeferences the current user can't act on —
    ones they placed or have already validated. The "include mine" toggle on
    the page re-requests the list with ?include_mine=1 to bring those back.
    Either way the list is capped at QUEUE_SIZE.

    Ordering (most in need of a vote first):
      1. correct minus incorrect votes, ascending
      2. value of the most recent validation, ascending (a negative latest
         vote sorts first; no votes is treated as neutral)
      3. correct minus uncertain votes, ascending
      4. confidence, ascending (low, then medium, then high)
      5. anonymous georeferences before logged-in ones
      6. newest first

    Responds with just the list partial when ?partial=1, so the toggle can
    swap the list in place without a full page reload.
    """
    include_mine = request.GET.get("include_mine") == "1"

    # The most recent georeference per image is the one shown to validators, so
    # the queue only considers those. distinct("image_id") relies on Postgres.
    current_ids = (
        Georeference.objects.filter(
            image__aerial=False, image__duplicate_of__isnull=True
        )
        .order_by("image_id", "-georeferenced_at")
        .distinct("image_id")
        .values_list("id", flat=True)
    )

    queue = (
        Georeference.objects.filter(id__in=list(current_ids))
        .select_related(
            "image",
            "image__collection",
            "image__collection__source",
            "georeferenced_by",
        )
        .annotate(
            correct_count=Count(
                "validations", filter=Q(validations__validation="correct")
            ),
            uncertain_count=Count(
                "validations", filter=Q(validations__validation="uncertain")
            ),
            incorrect_count=Count(
                "validations", filter=Q(validations__validation="incorrect")
            ),
            total_count=Count("validations"),
            already_validated=Exists(
                GeoreferenceValidation.objects.filter(
                    georeference=OuterRef("pk"), validated_by=request.user
                )
            ),
        )
        .annotate(
            net_correct=F("correct_count") - F("incorrect_count"),
            net_uncertain=F("correct_count") - F("uncertain_count"),
            # Signed value of the chronologically most recent validation, so a
            # georeference whose latest vote was "incorrect" sorts ahead of one
            # whose latest vote was "correct". No validations counts as neutral.
            last_validation_value=Coalesce(
                Subquery(
                    GeoreferenceValidation.objects.filter(georeference=OuterRef("pk"))
                    .order_by("-validated_at", "-id")
                    .annotate(
                        value=Case(
                            When(validation="incorrect", then=Value(-1)),
                            When(validation="correct", then=Value(1)),
                            default=Value(0),
                            output_field=IntegerField(),
                        )
                    )
                    .values("value")[:1]
                ),
                Value(0),
                output_field=IntegerField(),
            ),
            confidence_rank=Case(
                When(confidence="low", then=Value(0)),
                When(confidence="medium", then=Value(1)),
                When(confidence="high", then=Value(2)),
                default=Value(3),
                output_field=IntegerField(),
            ),
            # 0 for anonymous (no georeferencer), 1 for logged-in placements,
            # so anonymous sorts first under ascending order.
            anonymous_rank=Case(
                When(georeferenced_by__isnull=True, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            ),
        )
        .order_by(
            "net_correct",
            "last_validation_value",
            "net_uncertain",
            "confidence_rank",
            "anonymous_rank",
            "-georeferenced_at",
        )
    )

    if not include_mine:
        # Hide georeferences the current user can't validate: their own
        # placements and ones they've already voted on.
        queue = queue.exclude(georeferenced_by=request.user).exclude(
            validations__validated_by=request.user
        )

    context = {
        "georeferences": list(queue[:QUEUE_SIZE]),
        "include_mine": include_mine,
    }
    template = (
        "images/partials/validation_queue_list.html"
        if request.GET.get("partial")
        else "images/validation_queue.html"
    )
    return render(request, template, context)
