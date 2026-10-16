import logging
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, connection, models, transaction
from django.db.models import (
    Case,
    Count,
    Exists,
    IntegerField,
    OuterRef,
    Q,
    Subquery,
    Value,
    When,
)
from django.db.models.functions import Lower
from django.http import (
    Http404,
    HttpResponseBadRequest,
    HttpResponsePermanentRedirect,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.cache import cache_control
from django.views.decorators.http import require_http_methods
from django_ratelimit.decorators import ratelimit
from psycopg import sql

from activity.models import (
    GROUPING_WINDOW,
    SubjectIntroduction,
    SubjectMappingActivityGroup,
)
from images.embeddings import QUERY_VECTOR_SQL, VECTOR_SQL
from images.models import (
    Georeference,
    Image,
    SubjectMapping,
    SubjectMappingActivity,
)
from images.policies import (
    editable_images_for,
    get_image_or_404,
    get_images_or_404,
    parse_image_ids,
    representable_images,
)
from images.validation import InvalidInput, parse_json_body, validate_text
from regions.context_processors import get_current_region

from .forms import SubjectForm
from .models import Subject, SubjectAncestor, WikidataItem
from .name_search import annotate_name_match
from .similarity import build_subject_query_embedding
from .sparql_safety import (
    UnsafeSparqlInput,
    validate_qid,
)
from .subject_facts import fetch_authority_ids, fetch_subject_facts

logger = logging.getLogger(__name__)


def _record_subject_activity(
    *, user, image, subject, action, previous_order=None, new_order=None
):
    """Record a SubjectMappingActivity and attach it to a group.

    Adds and removes are bunched into a group with matching (user, subject,
    action) whose `ended_at` is within GROUPING_WINDOW. Reorders always
    create their own count=1 group.
    """
    now = timezone.now()

    if action == SubjectMappingActivity.ACTION_REORDERED:
        group = SubjectMappingActivityGroup.objects.create(
            user=user,
            subject=None,
            action=action,
            started_at=now,
            ended_at=now,
            count=1,
        )
    else:
        latest_group = (
            SubjectMappingActivityGroup.objects.filter(
                user=user, subject=subject, action=action
            )
            .order_by("-ended_at")
            .first()
        )
        if latest_group and (now - latest_group.ended_at) < GROUPING_WINDOW:
            latest_group.ended_at = now
            latest_group.count += 1
            latest_group.save(update_fields=["ended_at", "count"])
            group = latest_group
        else:
            group = SubjectMappingActivityGroup.objects.create(
                user=user,
                subject=subject,
                action=action,
                started_at=now,
                ended_at=now,
                count=1,
            )

    activity = SubjectMappingActivity.objects.create(
        user=user,
        image=image,
        subject=subject,
        action=action,
        previous_order=previous_order,
        new_order=new_order,
        group=group,
    )

    if (
        action == SubjectMappingActivity.ACTION_ADDED
        and subject is not None
        and SubjectMapping.objects.filter(subject=subject).count() == 1
    ):
        SubjectIntroduction.objects.get_or_create(
            subject=subject,
            defaults={"user": user, "image": image, "created_at": now},
        )

    return activity


# Queries longer than this are rejected outright by both autocompletes. The
# string only ever travels as a query parameter, so this is just a DoS bound,
# sized like the old SPARQL free-text cap.
_MAX_AUTOCOMPLETE_QUERY_LEN = 100

# The tagging autocomplete shows a longer, scrollable list than the browse
# dropdown, which shares its space with category matches.
_SUBJECT_AUTOCOMPLETE_LIMIT = 15


def _matching_subjects(query, limit=10):
    """Top Subjects by title-or-alias match, as both autocompletes list them."""
    return (
        annotate_name_match(
            Subject.objects.all(),
            query,
            title_field="title",
            item_field="wikidata_item",
        )
        .annotate(lower_title=Lower("title"))
        .select_related("wikidata_item")
        .order_by("match_rank", "-similarity", "lower_title")[:limit]
    )


def subject_autocomplete(request):
    if "q" not in request.GET:
        return JsonResponse([], safe=False)

    query = request.GET.get("q", "")
    if len(query) < 2 or len(query) > _MAX_AUTOCOMPLETE_QUERY_LEN:
        return JsonResponse([], safe=False)

    results = []
    for subject in _matching_subjects(query, limit=_SUBJECT_AUTOCOMPLETE_LIMIT):
        result = {
            "id": subject.id,
            "title": subject.title,
            "description": subject.get_description(),
            "wikidata_id": subject.wikidata_item.wikidata_id
            if subject.wikidata_item
            else None,
            "matched_alias": subject.matched_alias,
        }
        results.append(result)

    return JsonResponse(results, safe=False)


# Top-of-hierarchy Wikidata classes that appear in nearly every entity's
# P31/P279* closure but are too abstract to be useful filters. They get
# stripped out of the category autocomplete.
_AUTOCOMPLETE_CATEGORY_DENYLIST = (
    "Q35120",  # entity
    "Q488383",  # object
    "Q4406616",  # concrete object
    "Q830077",  # subject (philosophy)
    "Q99527517",  # collective entity
    "Q124711467",  # immaterial entity
    "Q58415929",  # spatio-temporal entity
    "Q27096235",  # artificial geographic entity
    "Q27096213",  # geographic entity
    "Q7048977",  # abstract entity
    "Q53617407",  # material entity
    "Q123349660",  # geolocatable entity
    "Q386724",  # work
    "Q17537576",  # creative work
    "Q15621286",  # intellectual work
)


def browse_autocomplete(request):
    """JSON autocomplete for the subject browse page.

    Returns both ``subjects`` (Subjects whose title or Wikidata alias
    matches) and ``categories`` (ancestors of any Subject whose label or
    alias matches, ranked by match quality and then subject-count).
    Subjects are returned with their detail-page URL so the frontend can
    navigate directly; categories with their Q-ID so the frontend can apply
    a ``?category=`` filter.

    Both halves rank matches the same way as ``subject_autocomplete`` (see
    ``subjects.name_search``); categories come from the ``SubjectAncestor``
    materialization, so neither half touches the graph.
    """
    q = (request.GET.get("q") or "").strip()
    if len(q) < 2:
        return JsonResponse({"categories": [], "subjects": []})
    if len(q) > _MAX_AUTOCOMPLETE_QUERY_LEN:
        return HttpResponseBadRequest(
            f"invalid q: exceeds {_MAX_AUTOCOMPLETE_QUERY_LEN} characters"
        )

    subjects_data = [
        {
            "url": s.get_absolute_url(),
            "title": s.title,
            "wikidata_id": s.wikidata_item.wikidata_id if s.wikidata_item else None,
            "matched_alias": s.matched_alias,
        }
        for s in _matching_subjects(q)
    ]

    # Ancestors that are themselves project Subjects belong in the subjects
    # half of the dropdown, not the categories half.
    subject_count = (
        SubjectAncestor.objects.filter(ancestor=OuterRef("pk"))
        .order_by()
        .values("ancestor")
        .annotate(n=Count("pk"))
        .values("n")
    )
    categories = (
        annotate_name_match(
            WikidataItem.objects.filter(
                Exists(SubjectAncestor.objects.filter(ancestor=OuterRef("pk"))),
                subject__isnull=True,
            ).exclude(wikidata_id__in=_AUTOCOMPLETE_CATEGORY_DENYLIST),
            q,
            title_field="title",
            item_field="pk",
        )
        .annotate(subject_count=Subquery(subject_count, output_field=IntegerField()))
        .order_by("match_rank", "-subject_count", "title")[:10]
    )
    categories_data = [
        {
            "qid": item.wikidata_id,
            "label": item.title,
            "subject_count": item.subject_count,
            "matched_alias": item.matched_alias,
        }
        for item in categories
    ]

    return JsonResponse({"categories": categories_data, "subjects": subjects_data})


def wikidata_lookup(request):
    """Look up a Wikidata item by ID and return subject info (creates if needed)"""
    wikidata_id = request.GET.get("id", "").strip().upper()

    if not wikidata_id or not wikidata_id.startswith("Q"):
        return JsonResponse(
            {
                "success": False,
                "error": "Invalid Wikidata ID format. Must start with 'Q'.",
            },
            status=400,
        )

    try:
        # Get or create WikidataItem (this fetches from Wikidata API if new)
        wikidata_item, item_created = WikidataItem.objects.get_or_create(
            wikidata_id=wikidata_id
        )
        if not item_created:
            wikidata_item.ensure_display_label()

        # Get or create Subject
        subject, subject_created = Subject.objects.get_or_create(
            wikidata_item=wikidata_item,
            defaults={"title": wikidata_item.title},
        )
        if not subject_created and subject.title == wikidata_id:
            subject.title = wikidata_item.title
            if subject.slug == slugify(wikidata_id):
                subject.slug = ""
            subject.save()

        return JsonResponse(
            {
                "success": True,
                "subject": {
                    "id": subject.id,
                    "title": subject.title,
                    "description": subject.get_description(),
                    "wikidata_id": wikidata_id,
                },
            }
        )

    except ValidationError as e:
        return JsonResponse({"success": False, "error": str(e)}, status=400)
    except Exception as e:
        return JsonResponse(
            {"success": False, "error": f"Error looking up Wikidata item: {str(e)}"},
            status=500,
        )


def all_subjects_api(request):
    """API endpoint to get all subjects as JSON"""
    subjects = Subject.objects.all().values("id", "title")
    return JsonResponse(list(subjects), safe=False)


def _resolve_subject(wikidata_id):
    """Get or create the Subject behind a Wikidata Q-ID.

    Raises :class:`InvalidInput` if the Q-ID is malformed or Wikidata refuses
    to yield an item for it.
    """
    try:
        wikidata_id = validate_qid(wikidata_id)
    except UnsafeSparqlInput:
        raise InvalidInput("Invalid Wikidata ID format. Must look like 'Q42'.")

    try:
        wikidata_item, created = WikidataItem.objects.get_or_create(
            wikidata_id=wikidata_id
        )
        if not created:
            wikidata_item.ensure_display_label()
    except ValidationError as exc:
        raise InvalidInput("; ".join(exc.messages))

    # select_related so the subject's URL (keyed by Q-ID) costs no extra query
    # when the caller renders a card for it.
    subject, subject_created = Subject.objects.select_related(
        "wikidata_item"
    ).get_or_create(
        wikidata_item=wikidata_item,
        defaults={"title": wikidata_item.title},
    )

    # Heal the title if an older row still has the Q-ID placeholder.
    if not subject_created and subject.title == wikidata_id:
        subject.title = wikidata_item.title
        if subject.slug == slugify(wikidata_id):
            subject.slug = ""
        subject.save()

    return subject


def _append_subject_mapping(*, user, image, subject):
    """Attach ``subject`` to ``image`` at the end of its subject order.

    Returns the new mapping, or ``None`` if the pair already existed.
    """
    max_order = (
        SubjectMapping.objects.filter(image=image).aggregate(
            max_order=models.Max("order")
        )["max_order"]
        or 0
    )

    try:
        # Savepoint, so a duplicate rolls back only this insert rather than
        # poisoning an enclosing transaction (e.g. the bulk-add loop).
        with transaction.atomic():
            mapping = SubjectMapping.objects.create(
                image=image, subject=subject, order=max_order + 1
            )
    except IntegrityError:
        # unique_together (image, subject): the pair already exists, either
        # from an earlier request or a concurrent one.
        return None

    _record_subject_activity(
        user=user,
        image=image,
        subject=subject,
        action=SubjectMappingActivity.ACTION_ADDED,
    )
    return mapping


@require_http_methods(["POST"])
def bulk_add_subject_to_images(request):
    """Add a subject to multiple images at once (logged-in users only)"""
    if not request.user.is_authenticated:
        return JsonResponse(
            {"success": False, "error": "You must be logged in to edit subjects"},
            status=403,
        )

    try:
        data = parse_json_body(request)
        image_ids = parse_image_ids(data.get("image_ids"))
        wikidata_id = validate_text(data, "wikidata_id", max_length=32, required=True)
    except (InvalidInput, ValueError) as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=400)

    # Authorize the whole set before touching Wikidata or writing anything, so
    # a request mixing authorized and unauthorized IDs tags nothing at all.
    images = get_images_or_404(editable_images_for(request.user), image_ids)

    try:
        subject = _resolve_subject(wikidata_id)
    except InvalidInput as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=400)

    added_count = 0
    already_exists_count = 0

    with transaction.atomic():
        for image in images:
            if _append_subject_mapping(user=request.user, image=image, subject=subject):
                added_count += 1
            else:
                already_exists_count += 1

    return JsonResponse(
        {
            "success": True,
            "added_count": added_count,
            "already_exists_count": already_exists_count,
            "subject": {
                "id": subject.id,
                "title": subject.title,
                "description": subject.get_description(),
            },
        },
        status=200,
    )


@require_http_methods(["POST"])
def add_subject_to_image(request, image_id):
    """Add a subject to an image via Wikidata ID (logged-in users only)"""
    if not request.user.is_authenticated:
        return JsonResponse(
            {"success": False, "error": "You must be logged in to edit subjects"},
            status=403,
        )

    image = get_image_or_404(editable_images_for(request.user), image_id)

    try:
        data = parse_json_body(request)
        wikidata_id = validate_text(data, "wikidata_id", max_length=32, required=True)
        subject = _resolve_subject(wikidata_id)
    except InvalidInput as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=400)

    subject_mapping = _append_subject_mapping(
        user=request.user, image=image, subject=subject
    )
    if subject_mapping is None:
        return JsonResponse(
            {
                "success": False,
                "error": "This subject is already associated with this image.",
            },
            status=400,
        )

    # Render the subject card partial for live insertion
    html = render_to_string(
        "subjects/partials/subject_card.html",
        {"subject_relation": subject_mapping, "request": request},
        request=request,
    )

    return JsonResponse(
        {
            "success": True,
            "message": f"Subject '{subject.title}' added to image",
            "html": html,
        }
    )


@require_http_methods(["POST"])
def remove_subject_from_image(request, subject_mapping_id):
    """Remove a subject from an image (logged-in users only)"""
    if not request.user.is_authenticated:
        return JsonResponse(
            {"success": False, "error": "You must be logged in to edit subjects"},
            status=403,
        )

    # Resolved through the mapping's image, so a mapping on an image the caller
    # cannot edit is as invisible as one that does not exist.
    try:
        subject_relation = SubjectMapping.objects.select_related(
            "image", "subject"
        ).get(
            id=subject_mapping_id,
            image__in=editable_images_for(request.user),
        )
    except SubjectMapping.DoesNotExist, TypeError, ValueError:
        raise Http404("Subject mapping not found")

    subject_title = subject_relation.subject.title
    removed_image = subject_relation.image
    removed_subject = subject_relation.subject

    with transaction.atomic():
        subject_relation.delete()
        _record_subject_activity(
            user=request.user,
            image=removed_image,
            subject=removed_subject,
            action=SubjectMappingActivity.ACTION_REMOVED,
        )

    return JsonResponse(
        {
            "success": True,
            "message": f"Subject '{subject_title}' removed from image.",
        }
    )


@require_http_methods(["POST"])
def set_representative_image(request, subject_id, image_id):
    """Set the representative image for a subject (logged-in users only)"""
    if not request.user.is_authenticated:
        return JsonResponse(
            {"success": False, "error": "You must be logged in to do this."},
            status=403,
        )

    subject = get_object_or_404(Subject, id=subject_id)
    # representable_images rather than the caller's own access: this image is
    # rendered on a public subject page, so a staff member must not be able to
    # promote a staged image into public view.
    image = get_image_or_404(representable_images(), image_id)

    # Verify the image is actually mapped to this subject
    if not SubjectMapping.objects.filter(subject=subject, image=image).exists():
        return JsonResponse(
            {"success": False, "error": "This image is not tagged with this subject."},
            status=400,
        )

    subject.representative_image = image
    subject.save(update_fields=["representative_image"])

    return JsonResponse(
        {
            "success": True,
            "message": (
                f"{image.title} is now the representative image for {subject.title}."
            ),
            "thumbnail": image.thumbnail or "",
        }
    )


@require_http_methods(["POST"])
def reorder_subjects(request, image_id):
    """API endpoint to reorder subjects for an image (logged-in users only)"""
    if not request.user.is_authenticated:
        return JsonResponse(
            {"success": False, "error": "You must be logged in to edit subjects"},
            status=403,
        )

    image = get_image_or_404(editable_images_for(request.user), image_id)

    try:
        data = parse_json_body(request)
    except InvalidInput as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=400)

    ordered_ids = data.get("order")
    if not isinstance(ordered_ids, list):
        return JsonResponse(
            {
                "success": False,
                "error": "Invalid data format: 'order' must be a list.",
            },
            status=400,
        )

    with transaction.atomic():
        # Get all subject relations for this image, in their current order
        subject_relations = list(
            SubjectMapping.objects.filter(image=image).order_by(
                "order", "subject__title"
            )
        )

        # Create a map of ID to instance
        relation_map = {str(relation.id): relation for relation in subject_relations}

        # The submitted list must be exactly this image's mappings — which also
        # means a mapping belonging to another image can never be reordered
        # into this one.
        submitted_ids = [str(relation_id) for relation_id in ordered_ids]
        if sorted(relation_map.keys()) != sorted(submitted_ids):
            return JsonResponse(
                {
                    "success": False,
                    "error": (
                        "Submitted subject IDs do not match existing subjects "
                        "for this image."
                    ),
                },
                status=400,
            )

        previous_subject_ids = [r.subject_id for r in subject_relations]
        new_subject_ids = [relation_map[rid].subject_id for rid in submitted_ids]

        # Update the order field based on the new order
        for index, relation_id in enumerate(submitted_ids):
            relation = relation_map[relation_id]
            relation.order = index
            relation.save(update_fields=["order"])

        if previous_subject_ids != new_subject_ids:
            _record_subject_activity(
                user=request.user,
                image=image,
                subject=None,
                action=SubjectMappingActivity.ACTION_REORDERED,
                previous_order=previous_subject_ids,
                new_order=new_subject_ids,
            )

    return JsonResponse(
        {"success": True, "message": "Subject order updated successfully."}
    )


def _public_image_count_annotations():
    """Annotations counting a subject's publicly visible images.

    Used by the subjects-map info panel. Counts originals only (no
    duplicates), from public collections of public sources; an image is
    georeferenced if it has any point georeference. _browse_subject_counts
    must count the same way so the panel and the browse cards agree.
    """
    public_images = Q(
        image_mappings__image__duplicate_of__isnull=True,
        image_mappings__image__collection__public=True,
        image_mappings__image__collection__source__public=True,
    )

    return {
        "total_images": models.Count(
            "image_mappings__image",
            filter=public_images,
            distinct=True,
        ),
        "georeferenced_images": models.Count(
            "image_mappings__image",
            filter=public_images
            & Q(image_mappings__image__georeferences__isnull=False),
            distinct=True,
        ),
    }


def _browse_subject_counts(region=None):
    """Ranked ``(subject_id, total_images, georeferenced_images)`` rows.

    Covers every subject eligible for the browse grid, most images first.
    When ``region`` is supplied, only images assigned to that region or any
    region inside it count, using Image.effective_region semantics.

    Aggregates SubjectMapping directly rather than annotating Subject: the
    eligible images are resolved once as a semi-join, and "georeferenced" is
    an EXISTS rather than a join, so an image with several georeferences
    doesn't multiply the rows being grouped. Counting mappings is a distinct
    image count because (image, subject) is unique.
    """
    images = Image.objects.filter(duplicate_of__isnull=True)
    if region is not None:
        images = images.in_region(region)
    public_image_ids = images.filter(
        collection__public=True,
        collection__source__public=True,
    ).values("pk")

    return (
        SubjectMapping.objects.filter(image_id__in=public_image_ids)
        # A city (or other broad subject) that is an ancestor of a more
        # specific Subject is represented by its descendants in this grid.
        # NOT IN rather than NOT EXISTS: Postgres misestimates the anti-join
        # as a single row and re-runs the aggregate for every subject.
        .exclude(
            subject__wikidata_item_id__in=SubjectAncestor.objects.values("ancestor_id")
        )
        .values("subject_id")
        .annotate(
            total_images=Count("pk"),
            georeferenced_images=Count(
                "pk",
                filter=Exists(
                    Georeference.objects.filter(image_id=OuterRef("image_id"))
                ),
            ),
        )
        .order_by("-total_images", "subject__title", "subject_id")
        .values_list("subject_id", "total_images", "georeferenced_images")
    )


def _browse_subject_cards(rows):
    """Load the Subjects for ``rows``, in order, carrying their counts."""
    subjects = (
        Subject.objects.select_related("wikidata_item", "representative_image")
        # Only what the cards and og: tags read; a deferred field read in the
        # template costs a query per subject. Notably skips the representative
        # image's embedding.
        .only(
            "title",
            "slug",
            "wikidata_item__wikidata_id",
            "wikidata_item__description",
            "wikidata_item__inception",
            "wikidata_item__demolished",
            "representative_image__title",
            "representative_image__thumbnail",
        )
        .in_bulk([subject_id for subject_id, _, _ in rows])
    )

    cards = []
    for subject_id, total_images, georeferenced_images in rows:
        subject = subjects.get(subject_id)
        if subject is None:
            # Deleted since the counts were read.
            continue
        subject.total_images = total_images
        subject.georeferenced_images = georeferenced_images
        cards.append(subject)
    return cards


def browse_subjects(request):
    """Browse subjects in the selected region, with search and load more."""
    PER_PAGE = 100

    browse_region = get_current_region(request)
    region_fallback = False
    # One aggregate serves the whole request: the fallback check, the overall
    # statistics, and the filtered page all derive from these rows.
    rows = list(_browse_subject_counts(browse_region))
    # Decide the fallback against the unsearched directory. A title or
    # category with no matches should stay an empty regional search rather
    # than unexpectedly replacing the directory with global results.
    if browse_region is not None and not rows:
        browse_region = None
        region_fallback = True
        rows = list(_browse_subject_counts())

    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    # Overall statistics come from the directory before any search filtering.
    if not is_ajax:
        overall_stats = {
            "total_subjects": len(rows),
            "total_images": sum(total for _, total, _ in rows),
        }

    # Apply category filter via the SubjectAncestor materialization. If the
    # category Q-ID is itself a project Subject, include it too and pin it
    # to the top of the page (it won't be in its own ancestor table).
    category_qid = (request.GET.get("category") or "").strip()
    selected_category = None
    if category_qid:
        try:
            validate_qid(category_qid)
        except UnsafeSparqlInput:
            return HttpResponseBadRequest("invalid category")

        matching_subject_ids = set(
            SubjectAncestor.objects.filter(
                ancestor__wikidata_id=category_qid,
            ).values_list("subject_id", flat=True)
        )
        category_subject_ids = set(
            Subject.objects.filter(
                wikidata_item__wikidata_id=category_qid,
            ).values_list("pk", flat=True)
        )

        category_item = WikidataItem.objects.filter(wikidata_id=category_qid).first()
        category_label = (
            category_item.title
            if category_item and category_item.title
            else category_qid
        )
        selected_category = {"qid": category_qid, "label": category_label}

        rows = [
            row
            for row in rows
            if row[0] in matching_subject_ids or row[0] in category_subject_ids
        ]
        # Stable sort, so the rest keep their ranking.
        rows.sort(key=lambda row: row[0] not in category_subject_ids)

    # Apply search filter. Matched in the database so icontains keeps
    # Postgres's case-folding rules; there are only ~1,200 subjects.
    query = request.GET.get("filter", "").strip()
    if query:
        title_matches = set(
            Subject.objects.filter(title__icontains=query).values_list("pk", flat=True)
        )
        rows = [row for row in rows if row[0] in title_matches]

    # Offset-based pagination
    try:
        offset = max(0, int(request.GET.get("offset", 0)))
    except ValueError, TypeError:
        offset = 0

    has_more = len(rows) > offset + PER_PAGE
    subject_list = _browse_subject_cards(rows[offset : offset + PER_PAGE])

    if is_ajax:
        return render(
            request,
            "subjects/partials/browse_subject_cards.html",
            {"subjects": subject_list, "has_more": has_more},
        )

    context = {
        "subjects": subject_list,
        "has_more": has_more,
        "per_page": PER_PAGE,
        "overall_stats": overall_stats,
        "selected_category": selected_category,
        "browse_region": browse_region,
        "region_fallback": region_fallback,
    }
    return render(request, "subjects/browse_subjects.html", context)


def subjects_map(request):
    """Standalone interactive map of all subjects and their georeferenced images.

    The map's data comes entirely from vector tiles fetched client-side, so the
    only per-request context is the zoom below which the tile endpoint answers
    empty; ``protomaps_api_key`` and ``tile_version`` come from
    ``images.context_processors.site_settings``.
    """
    return render(
        request,
        "subjects/subjects_map.html",
        {"osm_element_min_zoom": settings.SUBJECT_MAP_MIN_ZOOM},
    )


@cache_control(public=True, max_age=settings.SUBJECT_MAP_INFO_CACHE_SECONDS)
def subject_map_info(request, qid):
    """Compact subject summary for the subjects-map hover/pin panel.

    Vector tiles only carry a subject's title and URL key, so the panel fetches
    the rest here. Kept deliberately cheap — no graph round-trips — because
    it is requested on hover.
    """
    subject = get_object_or_404(
        Subject.objects.select_related(
            "wikidata_item", "representative_image"
        ).annotate(**_public_image_count_annotations()),
        wikidata_item__wikidata_id=qid,
    )

    representative_image = subject.get_representative_image()
    wikidata_item = subject.wikidata_item if subject.wikidata_item_id else None

    return JsonResponse(
        {
            "key": subject.url_key,
            "title": subject.title,
            "url": subject.get_absolute_url(),
            "description": subject.get_description(),
            "thumbnail": representative_image.thumbnail
            if representative_image
            else None,
            "total_images": subject.total_images,
            "georeferenced_images": subject.georeferenced_images,
            "wikidata_id": wikidata_item.wikidata_id if wikidata_item else None,
            "wikidata_url": wikidata_item.wikidata_url if wikidata_item else None,
            "date_range": wikidata_item.date_range if wikidata_item else "",
        }
    )


def legacy_subject_redirect(request, legacy_slug, view_name):
    """Permanently redirect a pre-Q-ID ``/subjects/<slug>/`` URL.

    Slugs are no longer unique. Any link of this shape was minted before
    duplicates were possible, so the oldest subject with the slug is the one
    it meant.
    """
    subject = (
        Subject.objects.select_related("wikidata_item")
        .filter(slug=legacy_slug)
        .order_by("pk")
        .first()
    )
    if subject is None:
        raise Http404("No Subject matches the given query.")

    url = reverse(view_name, kwargs={"qid": subject})
    query_string = request.META.get("QUERY_STRING")
    if query_string:
        url = f"{url}?{query_string}"
    return HttpResponsePermanentRedirect(url)


@staff_member_required
def subject_edit(request, qid):
    """Edit a subject's locally-stored settings."""
    subject = get_object_or_404(
        Subject.objects.select_related("wikidata_item"),
        wikidata_item__wikidata_id=qid,
    )
    if request.method == "POST":
        form = SubjectForm(request.POST, instance=subject)
        if form.is_valid():
            subject = form.save()
            messages.success(request, f'Saved subject "{subject.title}".')
            return redirect(subject)
    else:
        form = SubjectForm(instance=subject)
    return render(
        request, "subjects/subject_form.html", {"subject": subject, "form": form}
    )


def subject_detail(request, qid):
    """Detail view for a specific subject showing its images"""
    subject = get_object_or_404(
        Subject.objects.select_related("wikidata_item"),
        wikidata_item__wikidata_id=qid,
    )

    # Treat descendant subjects (those whose ancestors include this subject's
    # WikidataItem) as more-specific instances of this subject — their images
    # are folded into the listing here.
    relevant_subject_ids = list(
        Subject.objects.filter(
            Q(pk=subject.pk) | Q(ancestors__ancestor=subject.wikidata_item)
        )
        .values_list("pk", flat=True)
        .distinct()
    )

    # Get filter parameters from URL
    georeference_status = (
        request.GET.get("georeference_status", "").split(",")
        if request.GET.get("georeference_status")
        else []
    )
    start_year = request.GET.get("start_year")
    end_year = request.GET.get("end_year")
    with_subjects = (
        request.GET.get("with_subjects", "").split(",")
        if request.GET.get("with_subjects")
        else []
    )
    without_subjects = (
        request.GET.get("without_subjects", "").split(",")
        if request.GET.get("without_subjects")
        else []
    )
    no_subjects = request.GET.get("no_subjects") == "true"

    # Images associated with this subject or its descendants (only from
    # public collections, excluding duplicates)
    all_images = Image.objects.filter(
        subject_mappings__subject_id__in=relevant_subject_ids,
        duplicate_of__isnull=True,
        collection__public=True,
        collection__source__public=True,
    ).distinct()

    # Scope the grid and its counts to the navbar region, falling back to all
    # of the subject's images when none of them are in it. The map stays global.
    region = get_current_region(request)
    region_fallback = False
    scoped_images = all_images
    if region is not None:
        scoped_images = all_images.in_region(region)
        if not scoped_images.exists():
            region_fallback = True
            region = None
            scoped_images = all_images

    images = (
        scoped_images.select_related("collection__source")
        .prefetch_related("subjects")
        .annotate(
            has_georeference=Case(
                When(
                    Q(georeferences__isnull=False)
                    | Q(aerial=True, aerial_georeferences__isnull=False),
                    then=Value(1),
                ),
                default=Value(0),
                output_field=IntegerField(),
            )
        )
        .order_by("will_not_georef", "has_georeference", "id")
    )

    # Apply year filtering
    if start_year:
        try:
            start_year_int = int(start_year)
            images = images.filter(
                Q(fuzzy_start_decdate__gte=start_year_int)
                | Q(start_decdate__gte=start_year_int)
            )
        except ValueError:
            pass

    if end_year:
        try:
            end_year_int = int(end_year)
            images = images.filter(
                Q(fuzzy_end_decdate__lte=end_year_int)
                | Q(end_decdate__lte=end_year_int)
            )
        except ValueError:
            pass

    # Apply additional subject filtering (beyond the main subject)
    if no_subjects:
        # This doesn't make sense for a subject detail view, but keep for consistency
        images = images.filter(subjects__isnull=True)
    elif with_subjects:
        # Include only images with ALL of these subjects (in addition to the main subject)
        for subject_id in with_subjects:
            if subject_id and subject_id != str(subject.id):
                images = images.filter(subjects__id=subject_id)
    elif without_subjects:
        # Exclude images with ANY of these subjects
        exclude_ids = [sid for sid in without_subjects if sid != str(subject.id)]
        if exclude_ids:
            images = images.exclude(subjects__id__in=exclude_ids)

    # Apply georeference status filtering
    if georeference_status:
        # Build the filter conditions based on selected statuses
        filter_conditions = Q()

        if "georeferenced" in georeference_status:
            filter_conditions |= Q(georeferences__isnull=False) | Q(
                aerial=True, aerial_georeferences__isnull=False
            )

        if "pending" in georeference_status:
            filter_conditions |= (
                Q(georeferences__isnull=True)
                & Q(aerial=False)
                & Q(will_not_georef=False)
            )

        if "will_not_georef" in georeference_status:
            filter_conditions |= Q(will_not_georef=True)

        # Apply the filter if any conditions were added
        if filter_conditions:
            images = images.filter(filter_conditions)

    # Get counts before filtering for statistics
    total_subject_images = all_images.count()
    subject_georeferenced_images = (
        all_images.filter(georeferences__isnull=False).distinct().count()
    )
    if region is not None:
        total_images = scoped_images.count()
        georeferenced_images = (
            scoped_images.filter(georeferences__isnull=False).distinct().count()
        )
    else:
        total_images = total_subject_images
        georeferenced_images = subject_georeferenced_images
    pending_images = (
        total_images
        - georeferenced_images
        - scoped_images.filter(will_not_georef=True).count()
    )

    # Paginate the filtered images for browsing
    paginator = Paginator(images.distinct(), 24)  # 24 images per page for grid layout
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    # Check if subject has images with embeddings for similarity search
    has_images_with_embeddings = all_images.filter(embedding__isnull=False).exists()

    representative_image = subject.get_representative_image()

    wikidata_facts = (
        fetch_subject_facts(subject.wikidata_item.wikidata_id)
        if subject.wikidata_item_id
        else {}
    )
    authority_ids = (
        fetch_authority_ids(subject.wikidata_item.wikidata_id)
        if subject.wikidata_item_id
        else []
    )

    georeference_params = {"subject": subject.wikidata_item.wikidata_id}
    if region is not None:
        georeference_params["region"] = region.wikidata_item.wikidata_id
    georeference_url = (
        reverse("images:georeference_interface") + "?" + urlencode(georeference_params)
    )

    context = {
        "subject": subject,
        "browse_region": region,
        "region_fallback": region_fallback,
        "georeference_url": georeference_url,
        "page_obj": page_obj,
        "total_images": total_images,
        "total_subject_images": total_subject_images,
        "georeferenced_images": georeferenced_images,
        "subject_georeferenced_images": subject_georeferenced_images,
        "pending_images": pending_images,
        "completion_percentage": (georeferenced_images / total_images * 100)
        if total_images > 0
        else 0,
        "has_images_with_embeddings": has_images_with_embeddings,
        "representative_image": representative_image,
        "wikidata_facts": wikidata_facts,
        "authority_ids": authority_ids,
    }
    return render(request, "subjects/subject_detail.html", context)


@ratelimit(key="ip", rate="1000/h", method=["GET", "POST"])  # 16/min average
@ratelimit(key="ip", rate="100/5m", method=["GET", "POST"])  # 20/min burst
def find_similar_images_to_subject(request, qid):
    """
    Find and display images with embeddings most similar to the centroid
    of all images associated with a subject.

    Supports filtering via query parameters from filter_cards.html:
    - georeference_status: comma-separated values (georeferenced, pending, will_not_georef)
    - start_year, end_year: year range filtering
    - with_subjects: comma-separated subject IDs (images must have ALL)
    - without_subjects: comma-separated subject IDs (images must not have ANY)
    - no_subjects: if 'true', only images with no subjects

    For AJAX requests (X-Requested-With: XMLHttpRequest), returns just the image
    cards HTML partial for "Load More" functionality.
    """
    # Get the target subject
    subject = get_object_or_404(
        Subject.objects.select_related("wikidata_item"),
        wikidata_item__wikidata_id=qid,
    )

    # Get all images for this subject that have embeddings
    subject_images = Image.objects.filter(
        subject_mappings__subject=subject,
        embedding__isnull=False,
        collection__public=True,
        collection__source__public=True,
        duplicate_of__isnull=True,
    ).distinct()

    if not subject_images.exists():
        messages.error(
            request,
            "This subject has no images with embeddings, so similar images cannot be found.",
        )
        return redirect(subject)

    # Check if this is an AJAX request for "Load More"
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    # Get filter parameters from URL (matching filter_cards.html)
    georeference_status = (
        request.GET.get("georeference_status", "").split(",")
        if request.GET.get("georeference_status")
        else []
    )
    start_year = request.GET.get("start_year")
    end_year = request.GET.get("end_year")
    with_subjects = (
        request.GET.get("with_subjects", "").split(",")
        if request.GET.get("with_subjects")
        else []
    )
    without_subjects = (
        request.GET.get("without_subjects", "").split(",")
        if request.GET.get("without_subjects")
        else []
    )
    no_subjects = request.GET.get("no_subjects") == "true"

    # Pagination parameters - use offset-based pagination for "Load More"
    per_page = 24
    try:
        offset = int(request.GET.get("offset", 0))
        if offset < 0:
            offset = 0
    except ValueError, TypeError:
        offset = 0

    try:
        # Build the style-neutral query embedding from the subject's images:
        # each embedding is centered on its collection's mean so collection-
        # level style cancels out (see subjects/similarity.py)
        query_embedding = build_subject_query_embedding(
            subject_images.values_list("collection_id", "embedding")
        )

        if query_embedding is None:
            messages.error(
                request,
                "Unable to build a similarity query - no valid embeddings found.",
            )
            return redirect(subject)

        # Get IDs of subject images to exclude from results
        subject_image_ids = list(subject_images.values_list("id", flat=True))

        # Scope results to the navbar region, falling back to all images when
        # the region has no candidates at all. Decided before the user's
        # filters (like subject_detail) and on every request, so "Load More"
        # pages agree with the first one. The query embedding stays global.
        region = get_current_region(request)
        region_fallback = False
        if (
            region is not None
            and not Image.objects.filter(is_searchable=True, embedding__isnull=False)
            .exclude(id__in=subject_image_ids)
            .in_region(region)
            .exists()
        ):
            region = None
            region_fallback = True

        with transaction.atomic(), connection.cursor() as cursor:
            # Raise pgvector's HNSW search depth from its default of 40.
            # Scoped to this transaction via SET LOCAL.
            cursor.execute("SET LOCAL hnsw.ef_search = %s", [settings.HNSW_EF_SEARCH])
            # Keep scanning the index until enough rows pass the WHERE clause,
            # in exact distance order. Without this, filters (the region
            # especially) only post-filter the first ef_search candidates, so
            # a region whose images all rank beyond them would come back empty.
            cursor.execute("SET LOCAL hnsw.iterative_scan = strict_order")
            cursor.execute(
                "SET LOCAL hnsw.max_scan_tuples = %s", [settings.HNSW_MAX_SCAN_TUPLES]
            )

            # Convert query embedding to PostgreSQL array format
            embedding_str = "[" + ",".join(map(str, query_embedding)) + "]"

            # Build WHERE conditions
            where_conditions = ["embedding IS NOT NULL", "id != ALL(%s)"]
            where_params = [subject_image_ids]

            # Georeference status filtering (matching filter_cards.html behavior)
            if georeference_status:
                status_conditions = []
                if "georeferenced" in georeference_status:
                    status_conditions.append(
                        "(EXISTS (SELECT 1 FROM images_georeference g WHERE g.image_id = images_image.id) "
                        "OR (aerial = true AND EXISTS (SELECT 1 FROM images_aerialgeoreference ag WHERE ag.image_id = images_image.id)))"
                    )
                if "pending" in georeference_status:
                    status_conditions.append(
                        "((NOT EXISTS (SELECT 1 FROM images_georeference g WHERE g.image_id = images_image.id) "
                        "AND aerial = false AND will_not_georef = false) "
                        "OR (NOT EXISTS (SELECT 1 FROM images_aerialgeoreference ag WHERE ag.image_id = images_image.id) "
                        "AND aerial = true AND will_not_georef = false))"
                    )
                if "will_not_georef" in georeference_status:
                    status_conditions.append("will_not_georef = true")

                if status_conditions:
                    where_conditions.append(f"({' OR '.join(status_conditions)})")

            # Year range conditions
            if start_year:
                try:
                    start_year_int = int(start_year)
                    where_conditions.append(
                        "(fuzzy_start_decdate >= %s OR start_decdate >= %s)"
                    )
                    where_params.extend([start_year_int, start_year_int])
                except ValueError:
                    pass
            if end_year:
                try:
                    end_year_int = int(end_year)
                    where_conditions.append(
                        "(fuzzy_end_decdate <= %s OR end_decdate <= %s)"
                    )
                    where_params.extend([end_year_int, end_year_int])
                except ValueError:
                    pass

            # Subject filtering
            if no_subjects:
                where_conditions.append(
                    "NOT EXISTS (SELECT 1 FROM images_subjectmapping sm WHERE sm.image_id = images_image.id)"
                )
            elif with_subjects:
                # Images must have ALL specified subjects
                for subject_id in with_subjects:
                    if subject_id:
                        where_conditions.append(
                            "EXISTS (SELECT 1 FROM images_subjectmapping sm WHERE sm.image_id = images_image.id AND sm.subject_id = %s)"
                        )
                        where_params.append(subject_id)
            elif without_subjects:
                # Images must not have ANY of the specified subjects
                valid_ids = [sid for sid in without_subjects if sid]
                if valid_ids:
                    placeholders = ", ".join(["%s"] * len(valid_ids))
                    where_conditions.append(
                        f"NOT EXISTS (SELECT 1 FROM images_subjectmapping sm WHERE sm.image_id = images_image.id AND sm.subject_id IN ({placeholders}))"
                    )
                    where_params.extend(valid_ids)

            count_template = """
                SELECT COUNT(id)
                FROM images_image
                WHERE {where_clause}
                AND is_searchable = true
            """

            # Size of the same pool across every region, for the
            # "Showing X of Y images in <region>" banner
            total_image_count = None
            if region is not None and not is_ajax:
                cursor.execute(
                    sql.SQL(count_template).format(
                        where_clause=sql.SQL(" AND ".join(where_conditions))
                    ),
                    where_params,
                )
                total_image_count = cursor.fetchone()[0]

            # Same resolution as ImageQuerySet.in_region. Appended last
            # because where_params is positional: the page query splices it
            # between the two embedding parameters.
            if region is not None:
                where_conditions.append(
                    Image.EFFECTIVE_REGION_SQL.format(alias="images_image.")
                    + " = ANY(%s)"
                )
                where_params.append(region.self_and_descendant_ids())

            where_clause = " AND ".join(where_conditions)

            # Get total count for pagination
            cursor.execute(
                sql.SQL(count_template).format(where_clause=sql.SQL(where_clause)),
                where_params,
            )
            region_image_count = cursor.fetchone()[0]
            # Pagination stops at ef_search results, matching the other
            # similarity views.
            total_count = min(region_image_count, settings.HNSW_EF_SEARCH)

            # SQL-level pagination - only fetch the IDs we need for this page
            query_sql = sql.SQL("""
                SELECT
                    id,
                    ({vector} <=> {query_vector}) as distance
                FROM images_image
                WHERE {where_clause}
                AND is_searchable = true
                ORDER BY {vector} <=> {query_vector}, id ASC
                LIMIT %s OFFSET %s
            """).format(
                where_clause=sql.SQL(where_clause),
                vector=sql.SQL(VECTOR_SQL),
                query_vector=sql.SQL(QUERY_VECTOR_SQL),
            )
            cursor.execute(
                query_sql,
                [embedding_str] + where_params + [embedding_str, per_page, offset],
            )
            page_results = cursor.fetchall()

        # Get the IDs for this page only
        current_page_ids = [row[0] for row in page_results]

        # Get the full Image objects for the current page
        images_on_page = Image.objects.filter(id__in=current_page_ids).select_related(
            "collection__source"
        )

        # Create a dictionary to map IDs to image objects for correct ordering
        images_by_id = {img.id: img for img in images_on_page}

        # Re-order the fetched image objects based on the result order
        ordered_images = [
            images_by_id[img_id]
            for img_id in current_page_ids
            if img_id in images_by_id
        ]

        # Calculate if there are more images to load
        next_offset = offset + per_page
        has_more = next_offset < total_count

        # For AJAX requests, return just the image cards partial
        if is_ajax:
            return render(
                request,
                "images/partials/similar_images_items.html",
                {
                    "images": ordered_images,
                    "has_more": has_more,
                    "georeference_url": "/georeference/",
                    "show_collection_link": True,
                    "badges": True,
                    "buttons": True,
                },
            )

        # Get total number of images for this subject
        total_subject_images = (
            Image.objects.filter(
                subject_mappings__subject=subject,
                collection__public=True,
                collection__source__public=True,
                duplicate_of__isnull=True,
            )
            .distinct()
            .count()
        )

        # For regular requests, return the full page
        context = {
            "subject": subject,
            "subject_image_count": subject_images.count(),
            "total_subject_images": total_subject_images,
            "images": ordered_images,
            "total_similar_count": total_count,
            "has_more": has_more,
            "per_page": per_page,
            "browse_region": region,
            "region_fallback": region_fallback,
            "region_image_count": region_image_count,
            "total_image_count": total_image_count,
        }

        return render(request, "subjects/subject_similar_images.html", context)

    except Exception as e:
        messages.error(
            request, f"An error occurred while finding similar images: {str(e)}"
        )
        return redirect(subject)
