import json
from datetime import datetime

from django.db.models import Count, F, Max, Prefetch, Q
from django.db.models.functions import Coalesce
from django.http import Http404
from django.shortcuts import render
from django.utils import timezone

from images.models import (
    UNDISPLAYED_IMAGE_FIELDS,
    AerialGeoreferenceValidation,
    Comment,
    GeoreferenceValidation,
    SubjectMappingActivity,
    in_region_q,
)
from regions.context_processors import get_current_region

from .models import (
    CollectionIntroduction,
    GeoreferenceGroup,
    GeoreferenceGroupMember,
    SitewideMilestone,
    SubjectIntroduction,
    SubjectMappingActivityGroup,
    UserMilestone,
)

ITEMS_PER_PAGE = 20
ALL_EVENT_TYPES = {
    "group",
    "comment",
    "milestone",
    "sitewide",
    "validation",
    "subject",
    "new_subject",
    "new_collection",
}
DEFAULT_EVENT_TYPES = {
    "group",
    "comment",
    "milestone",
    "sitewide",
    "new_subject",
    "new_collection",
}

# How many groups a regional feed examines first when looking for the
# latest in-region activity (see _latest_regional_groups); each further
# batch doubles. Comfortably more than a page, so a busy region needs one.
REGIONAL_GROUP_BATCH = 25


def activity_feed(request):
    """Display the activity feed showing recent site activity."""
    region = get_current_region(request)
    before_param = request.GET.get("before")
    types_param = request.GET.get("types", "")
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    # Parse event types filter
    none_selected = False
    if types_param:
        selected_types = set(types_param.split(",")) & ALL_EVENT_TYPES
        if not selected_types:
            # Explicit types param with no valid types = none selected
            none_selected = True
            selected_types = set()
    else:
        selected_types = DEFAULT_EVENT_TYPES

    # Parse 'before' timestamp if provided
    before = None
    if before_param:
        try:
            before = datetime.fromisoformat(before_param)
            if timezone.is_naive(before):
                before = timezone.make_aware(before)
        except ValueError, TypeError:
            if is_ajax:
                raise Http404("Invalid timestamp")
            # For non-AJAX, just ignore invalid timestamp

    if none_selected:
        events = []
    else:
        events = get_activity_events(
            before=before,
            limit=ITEMS_PER_PAGE,
            event_types=selected_types,
            region=region,
        )

    if is_ajax:
        return render(
            request,
            "activity/partials/activity_items.html",
            {"events": events, "has_more": len(events) == ITEMS_PER_PAGE},
        )

    # Build filter states for JavaScript initialization
    filter_states = json.dumps({t: t in selected_types for t in ALL_EVENT_TYPES})

    return render(
        request,
        "activity/feed.html",
        {
            "events": events,
            "has_more": len(events) == ITEMS_PER_PAGE,
            "filter_states_json": filter_states,
            "none_selected": none_selected,
        },
    )


def _deferred_image_fields(*paths):
    """UNDISPLAYED_IMAGE_FIELDS of the images select_related through ``paths``."""
    return [f"{path}__{field}" for path in paths for field in UNDISPLAYED_IMAGE_FIELDS]


def _latest_regional_groups(groups, members, timestamp, member_filter, limit, before):
    """The ``limit`` groups whose in-region activity is latest, newest first.

    Serves both kinds of grouped activity (georeference sessions and subject
    tagging): ``groups`` is the group queryset, ``members`` the queryset of
    their members, ``timestamp`` the members' time field, and
    ``member_filter`` the Q selecting in-region members. Each returned group
    carries ``feed_count`` and ``feed_ended_at`` over its in-region members
    only, and the prefetched ``feed_members`` are those members alone.

    Aggregating every group's members in one query means resolving the
    region of every image ever grouped. Instead this walks groups newest
    first along their ``ended_at`` index, in doubling batches, counting
    in-region members for just that batch. A group's ``ended_at`` is its
    latest member's timestamp (activity.signals and subjects.views keep it
    so; for subject tagging, to within the millisecond it takes to save the
    member), so no group left unvisited can have in-region activity later
    than the last candidate's ``ended_at``. Once the ``limit``-th best found
    is at least that late, the walk can stop. In a busy region that is the
    first batch.
    """
    # SiteSettings.home_feed_item_count may be 0; the stop check below
    # indexes the limit-th best, which doesn't exist then.
    if limit <= 0:
        return []
    found = {}
    ordered = groups.order_by("-ended_at", "-id").values_list("id", "ended_at")
    remaining, batch = ordered, REGIONAL_GROUP_BATCH
    while True:
        candidates = list(remaining[:batch])
        if not candidates:
            break
        # Resume after the last (ended_at, id) seen rather than at an offset,
        # so a batch doesn't rescan its predecessors, and groups created
        # mid-walk can't shift ones already visited into the next batch.
        last_id, last_ended_at = candidates[-1]
        remaining = ordered.filter(ended_at__lte=last_ended_at).exclude(
            ended_at=last_ended_at, id__gte=last_id
        )
        batch *= 2

        rows = (
            members.filter(member_filter, group_id__in=[pk for pk, _ in candidates])
            .order_by()
            .values("group_id")
            .annotate(feed_count=Count("id"), feed_ended_at=Max(timestamp))
        )
        for row in rows:
            # A group whose in-region activity runs past ``before`` belongs
            # to an earlier page, whole — the same rule the global feed
            # applies to ended_at.
            if before is None or row["feed_ended_at"] < before:
                found[row["group_id"]] = (row["feed_count"], row["feed_ended_at"])

        latest = sorted(found.values(), key=lambda entry: entry[1], reverse=True)
        if len(latest) >= limit and latest[limit - 1][1] >= candidates[-1][1]:
            break

    chosen = sorted(found, key=lambda pk: found[pk][1], reverse=True)[:limit]
    loaded = groups.filter(id__in=chosen).prefetch_related(
        Prefetch(
            "members",
            queryset=members.filter(member_filter),
            to_attr="feed_members",
        )
    )
    by_id = {group.id: group for group in loaded}
    # A group deleted since it was counted (its user's account, say) is
    # simply missing from the page.
    chosen = [pk for pk in chosen if pk in by_id]
    for pk in chosen:
        by_id[pk].feed_count, by_id[pk].feed_ended_at = found[pk]
    return [by_id[pk] for pk in chosen]


def get_activity_events(
    before=None, limit=ITEMS_PER_PAGE, event_types=None, region=None
):
    """
    Fetch activity events, optionally filtered to those before a timestamp.

    Args:
        before: Optional datetime to filter events before
        limit: Maximum number of events to return
        event_types: Set of event types to include (default: all types)
        region: Optional Region whose activity to return. Image-backed events
            use Image.effective_region semantics and include descendant regions.

    Returns a list of (type, object, timestamp) tuples sorted by timestamp descending.
    """
    if event_types is None:
        event_types = ALL_EVENT_TYPES

    # Build filters
    comment_filter = {}
    milestone_filter = {}
    sitewide_filter = {}
    validation_filter = {}
    new_subject_filter = {}
    new_collection_filter = {}

    # Image-backed events reach the region through joins on their own image
    # (see images.models.in_region_q); an empty Q filters nothing.
    region_ids = None
    image_region = Q()
    validation_region = Q()
    if region is not None:
        region_ids = region.self_and_descendant_ids()
        image_region = in_region_q("image", region_ids)
        validation_region = in_region_q("georeference__image", region_ids)

    # User and sitewide milestones have no image-backed event to associate
    # with a region, so they intentionally remain global in regional feeds.

    if before:
        comment_filter["created_at__lt"] = before
        milestone_filter["reached_at__lt"] = before
        sitewide_filter["reached_at__lt"] = before
        validation_filter["validated_at__lt"] = before
        new_subject_filter["created_at__lt"] = before
        new_collection_filter["created_at__lt"] = before

    events = []

    # Fetch the latest `limit` of each type. Each queryset is sorted by the
    # same timestamp used in the merge below, so the merged top-`limit` can
    # never need more than `limit` rows from any one type.
    if "group" in event_types:
        group_members = (
            GeoreferenceGroupMember.objects.select_related(
                "georeference__image__collection__source",
                "aerial_georeference__image__collection__source",
            )
            .defer(
                *_deferred_image_fields(
                    "georeference__image", "aerial_georeference__image"
                )
            )
            .order_by("-added_at")
        )
        groups = GeoreferenceGroup.objects.select_related("user")
        if region_ids is None:
            groups = groups.annotate(
                feed_count=F("count"), feed_ended_at=F("ended_at")
            ).prefetch_related(
                Prefetch("members", queryset=group_members, to_attr="feed_members")
            )
            if before:
                groups = groups.filter(feed_ended_at__lt=before)
            groups = groups.order_by("-feed_ended_at")[:limit]
        else:
            groups = _latest_regional_groups(
                groups,
                group_members,
                "added_at",
                in_region_q("georeference__image", region_ids)
                | in_region_q("aerial_georeference__image", region_ids),
                limit,
                before,
            )
        events.extend(("group", g, g.feed_ended_at) for g in groups)

    if "comment" in event_types:
        comments = (
            Comment.objects.filter(image_region, **comment_filter)
            .select_related("commented_by", "image__collection__source")
            .defer(*_deferred_image_fields("image"))
            .order_by("-created_at")[:limit]
        )
        events.extend(("comment", c, c.created_at) for c in comments)

    if "milestone" in event_types:
        milestones = (
            UserMilestone.objects.filter(**milestone_filter)
            .select_related("user")
            .order_by("-reached_at")[:limit]
        )
        events.extend(("milestone", m, m.reached_at) for m in milestones)

    if "sitewide" in event_types:
        sitewide_milestones = SitewideMilestone.objects.filter(
            **sitewide_filter
        ).order_by("-reached_at")[:limit]
        events.extend(("sitewide", m, m.reached_at) for m in sitewide_milestones)

    if "validation" in event_types:
        georef_validations = (
            GeoreferenceValidation.objects.filter(
                validation_region, **validation_filter
            )
            .select_related("validated_by", "georeference__image")
            .defer(*_deferred_image_fields("georeference__image"))
            .order_by("-validated_at")[:limit]
        )
        events.extend(("validation", v, v.validated_at) for v in georef_validations)

        aerial_validations = (
            AerialGeoreferenceValidation.objects.filter(
                validation_region, **validation_filter
            )
            .select_related("validated_by", "georeference__image")
            .defer(*_deferred_image_fields("georeference__image"))
            .order_by("-validated_at")[:limit]
        )
        events.extend(("validation", v, v.validated_at) for v in aerial_validations)

    if "subject" in event_types:
        subject_members = (
            SubjectMappingActivity.objects.select_related("image__collection__source")
            .defer(*_deferred_image_fields("image"))
            .order_by("-created_at")
        )
        subject_groups = SubjectMappingActivityGroup.objects.select_related(
            "user", "subject", "subject__wikidata_item"
        )
        if region_ids is None:
            subject_groups = subject_groups.annotate(
                feed_count=F("count"), feed_ended_at=F("ended_at")
            ).prefetch_related(
                Prefetch("members", queryset=subject_members, to_attr="feed_members")
            )
            if before:
                subject_groups = subject_groups.filter(feed_ended_at__lt=before)
            subject_groups = subject_groups.order_by("-feed_ended_at")[:limit]
        else:
            subject_groups = _latest_regional_groups(
                subject_groups,
                subject_members,
                "created_at",
                image_region,
                limit,
                before,
            )
        events.extend(("subject", g, g.feed_ended_at) for g in subject_groups)

    if "new_subject" in event_types:
        introductions = (
            SubjectIntroduction.objects.filter(image_region, **new_subject_filter)
            .select_related(
                "user",
                "image",
                "subject",
                "subject__wikidata_item",
                "subject__representative_image",
            )
            .defer(*_deferred_image_fields("image", "subject__representative_image"))
            .order_by("-created_at")[:limit]
        )
        events.extend(("new_subject", i, i.created_at) for i in introductions)

    if "new_collection" in event_types:
        collection_intros = CollectionIntroduction.objects.filter(
            **new_collection_filter
        )
        if region_ids is not None:
            collection_intros = collection_intros.annotate(
                effective_region_id=Coalesce(
                    "collection__region_id", "collection__source__region_id"
                )
            ).filter(effective_region_id__in=region_ids)
        collection_intros = collection_intros.select_related(
            "collection", "collection__source"
        ).order_by("-created_at")[:limit]
        events.extend(("new_collection", c, c.created_at) for c in collection_intros)

    # Sort by timestamp descending and take the requested limit
    events.sort(key=lambda e: e[2], reverse=True)

    return events[:limit]
