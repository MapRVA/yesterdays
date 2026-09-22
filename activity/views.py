import json
from datetime import datetime

from django.db.models import Count, F, Max, Prefetch, Q
from django.db.models.functions import Coalesce
from django.http import Http404
from django.shortcuts import render
from django.utils import timezone

from images.models import (
    AerialGeoreferenceValidation,
    Comment,
    GeoreferenceValidation,
    Image,
    SubjectMappingActivity,
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
    group_filter = {}
    comment_filter = {}
    milestone_filter = {}
    sitewide_filter = {}
    validation_filter = {}
    subject_filter = {}
    new_subject_filter = {}
    new_collection_filter = {}

    regional_image_ids = None
    region_ids = None
    if region is not None:
        region_ids = region.self_and_descendant_ids()
        regional_image_ids = Image.objects.in_region(region).values("pk")
        comment_filter["image_id__in"] = regional_image_ids
        validation_filter["georeference__image_id__in"] = regional_image_ids
        new_subject_filter["image_id__in"] = regional_image_ids

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
        group_members = GeoreferenceGroupMember.objects.select_related(
            "georeference__image__collection__source",
            "aerial_georeference__image__collection__source",
        ).order_by("-added_at")
        if regional_image_ids is None:
            group_annotations = {
                "feed_count": F("count"),
                "feed_ended_at": F("ended_at"),
            }
        else:
            group_member_filter = Q(
                members__georeference__image_id__in=regional_image_ids
            ) | Q(members__aerial_georeference__image_id__in=regional_image_ids)
            member_filter = Q(georeference__image_id__in=regional_image_ids) | Q(
                aerial_georeference__image_id__in=regional_image_ids
            )
            group_annotations = {
                "feed_count": Count("members", filter=group_member_filter),
                "feed_ended_at": Max("members__added_at", filter=group_member_filter),
            }
            group_members = group_members.filter(member_filter)

        groups = (
            GeoreferenceGroup.objects.filter(**group_filter)
            .annotate(**group_annotations)
            .select_related("user")
            .prefetch_related(
                Prefetch(
                    "members",
                    queryset=group_members,
                    to_attr="feed_members",
                )
            )
        )
        if regional_image_ids is not None:
            groups = groups.filter(feed_count__gt=0)
        if before:
            groups = groups.filter(feed_ended_at__lt=before)
        groups = groups.order_by("-feed_ended_at")[:limit]
        events.extend(("group", g, g.feed_ended_at) for g in groups)

    if "comment" in event_types:
        comments = (
            Comment.objects.filter(**comment_filter)
            .select_related("commented_by", "image__collection__source")
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
            GeoreferenceValidation.objects.filter(**validation_filter)
            .select_related("validated_by", "georeference__image")
            .order_by("-validated_at")[:limit]
        )
        events.extend(("validation", v, v.validated_at) for v in georef_validations)

        aerial_validations = (
            AerialGeoreferenceValidation.objects.filter(**validation_filter)
            .select_related("validated_by", "georeference__image")
            .order_by("-validated_at")[:limit]
        )
        events.extend(("validation", v, v.validated_at) for v in aerial_validations)

    if "subject" in event_types:
        subject_members = SubjectMappingActivity.objects.select_related(
            "image__collection__source"
        ).order_by("-created_at")
        if regional_image_ids is None:
            subject_annotations = {
                "feed_count": F("count"),
                "feed_ended_at": F("ended_at"),
            }
        else:
            subject_member_filter = Q(members__image_id__in=regional_image_ids)
            subject_annotations = {
                "feed_count": Count("members", filter=subject_member_filter),
                "feed_ended_at": Max(
                    "members__created_at", filter=subject_member_filter
                ),
            }
            subject_members = subject_members.filter(image_id__in=regional_image_ids)

        subject_groups = (
            SubjectMappingActivityGroup.objects.filter(**subject_filter)
            .annotate(**subject_annotations)
            .select_related("user", "subject")
            .prefetch_related(
                Prefetch(
                    "members",
                    queryset=subject_members,
                    to_attr="feed_members",
                )
            )
        )
        if regional_image_ids is not None:
            subject_groups = subject_groups.filter(feed_count__gt=0)
        if before:
            subject_groups = subject_groups.filter(feed_ended_at__lt=before)
        subject_groups = subject_groups.order_by("-feed_ended_at")[:limit]
        events.extend(("subject", g, g.feed_ended_at) for g in subject_groups)

    if "new_subject" in event_types:
        introductions = (
            SubjectIntroduction.objects.filter(**new_subject_filter)
            .select_related(
                "user",
                "image",
                "subject",
                "subject__wikidata_item",
                "subject__representative_image",
            )
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
