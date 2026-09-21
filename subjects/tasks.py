"""
Celery tasks for refreshing external metadata (Wikidata, OSM).

Rate limiting is achieved through Celery Beat scheduling: Beat triggers
each refresh task at a fixed interval (e.g., every 15 seconds for 4/minute),
ensuring global rate limits regardless of worker count.
"""

import json
import logging
import re
from datetime import timedelta

import requests
from celery import shared_task
from django.conf import settings
from django.contrib.gis.geos import GEOSGeometry
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from images.tasks import reconcile_collection_region_stats
from regions.models import Region, RegionAncestor
from regions.region_ancestors import update_region_ancestors

from .memgraph import GRAPH_ERRORS, MemgraphClient
from .models import OsmElement, Subject, WikidataItem
from .project_graph import rebuild_project_graph
from .subject_ancestors import update_subject_ancestors
from .wikidata_closure import (
    SEED_METADATA_FIELDS,
    ClosureLoadError,
    commit_closure_to_memgraph,
    fetch_seed_data,
)

logger = logging.getLogger(__name__)


def get_stale_threshold_hours():
    """Get the number of hours after which metadata is considered stale."""
    return getattr(settings, "METADATA_REFRESH_STALE_HOURS", 24)


def get_max_failures():
    """Get the maximum consecutive failures before stopping retries."""
    return getattr(settings, "METADATA_REFRESH_MAX_FAILURES", 5)


def create_request_session():
    """Create a requests session with retry strategy."""
    session = requests.Session()
    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


# =============================================================================
# Wikidata Refresh Tasks
# =============================================================================


def _region_ancestor_ids(region):
    """The set of WikidataItem ids this region currently rolls up into."""
    return set(
        RegionAncestor.objects.filter(region=region).values_list(
            "ancestor_id", flat=True
        )
    )


def _do_refresh_wikidata_item(item):
    """Re-pull a WikidataItem's closure from WDQS and refresh its mirror.

    One CONSTRUCT to WDQS gives us the seed's metadata fields and its
    closure neighbourhood; the seed row gets the metadata, each entity's
    Memgraph mirror gets atomically swapped, and ancestors discovered
    along the way are linked back to the seed's Subject via
    ``discovered_via``.
    """

    logger.info(f"Refreshing WikidataItem {item.wikidata_id}")

    # Bump the freshness timestamp up front so a concurrent Beat tick
    # doesn't pick the same row while we're talking to WDQS. Targeted
    # UPDATE avoids the full-row save that ``item.save()`` does. Any
    # manual queue request is consumed here rather than on success: a
    # request that survived a failure would pin the item at the front of
    # the rotation forever, starving every other seed.
    now = timezone.now()
    WikidataItem.objects.filter(pk=item.pk).update(
        sparql_last_loaded_at=now, sparql_refresh_requested_at=None
    )
    item.sparql_last_loaded_at = now
    item.sparql_refresh_requested_at = None

    # Region-ness decides the query shape: region seeds also walk their
    # P131 containment chain, so resolve it before talking to WDQS.
    try:
        seed_region = item.region
    except Region.DoesNotExist:
        seed_region = None

    try:
        data = fetch_seed_data(item.wikidata_id, include_p131=seed_region is not None)
    except requests.RequestException as e:
        WikidataItem.objects.filter(pk=item.pk).update(
            sparql_fetch_failures=F("sparql_fetch_failures") + 1,
        )
        logger.error(f"WDQS fetch failed for {item.wikidata_id}: {e}")
        return {"status": "error", "wikidata_id": item.wikidata_id, "message": str(e)}
    except ClosureLoadError as e:
        WikidataItem.objects.filter(pk=item.pk).update(
            sparql_fetch_failures=F("sparql_fetch_failures") + 1,
        )
        logger.warning(f"Unusable closure for {item.wikidata_id}: {e}")
        return {"status": "no_data", "wikidata_id": item.wikidata_id}

    try:
        seed_subject = item.subject
    except Subject.DoesNotExist:
        seed_subject = None

    try:
        with transaction.atomic():
            item._apply_seed_metadata(data["metadata"])
            item.sparql_last_loaded_at = timezone.now()
            item.sparql_fetch_failures = 0
            item.save(update_fields=list(SEED_METADATA_FIELDS))
            coordinate = data["metadata"]["coordinate_location"]
            if seed_region is not None and coordinate is not None:
                # P625 rides along in the seed metadata but belongs to the
                # Region, not the item. Only written when the claim is
                # present: a P625 that vanishes upstream (vandalism, a
                # rename, a bad edit) is far more likely to be a mistake
                # than a real correction, so the last known coordinate
                # stands — and clearing it would breach
                # region_has_a_coordinate for a region with no custom
                # centerpoint, turning that edit into an outage. An
                # admin's
                # custom_coordinate_location is a separate column and
                # untouched here — a refresh must never walk a
                # hand-placed map center. Targeted UPDATE for the same
                # reason as above, with updated_at set by hand since
                # auto_now doesn't fire on queryset updates.
                Region.objects.filter(pk=seed_region.pk).update(
                    wikidata_coordinate_location=coordinate,
                    updated_at=timezone.now(),
                )
            commit_closure_to_memgraph(
                item.wikidata_id,
                data["groups"],
                data["labels"],
                discovered_via=seed_subject,
            )
    except GRAPH_ERRORS as e:
        WikidataItem.objects.filter(pk=item.pk).update(
            sparql_fetch_failures=F("sparql_fetch_failures") + 1,
        )
        logger.error(f"Memgraph update failed for {item.wikidata_id}: {e}")
        return {"status": "error", "wikidata_id": item.wikidata_id, "message": str(e)}

    # Project the seed's category ancestors into Postgres. Best-effort:
    # if this fails we keep the successful Memgraph commit, and the next
    # refresh of the same Subject will re-attempt the projection.
    if seed_subject is not None:
        try:
            with MemgraphClient() as client:
                update_subject_ancestors(seed_subject, client)
        except GRAPH_ERRORS as e:
            logger.warning(
                "SubjectAncestor refresh failed for %s: %s — "
                "will retry on next refresh of this Subject",
                item.wikidata_id,
                e,
            )

    # Same deal for the region containment projection.
    if seed_region is not None:
        # Snapshotted around the call rather than reported back by
        # update_region_ancestors, which is also driven by the shell-only
        # rebuild_all_region_ancestors() in a loop — a reconcile per region
        # there would be pure waste.
        ancestors_before = _region_ancestor_ids(seed_region)
        try:
            with MemgraphClient() as client:
                update_region_ancestors(seed_region, client)
        except GRAPH_ERRORS as e:
            logger.warning(
                "RegionAncestor refresh failed for %s: %s — "
                "will retry on next refresh of this Region",
                item.wikidata_id,
                e,
            )
        else:
            if _region_ancestor_ids(seed_region) != ancestors_before:
                # Which regions this region's images roll up into just
                # changed, and no image, collection, or source row moved to
                # signal it. This call site sits outside the atomic block
                # above, so on_commit fires straight away.
                transaction.on_commit(reconcile_collection_region_stats.delay)

    logger.info(f"Successfully refreshed WikidataItem {item.wikidata_id}")
    return {"status": "success", "wikidata_id": item.wikidata_id}


# =============================================================================
# OSM Refresh Tasks
# =============================================================================


def get_postpass_url():
    """Get Postpass API URL from settings."""
    return getattr(
        settings,
        "METADATA_REFRESH_POSTPASS_URL",
        "https://postpass.geofabrik.de/api/interpreter",
    )


def get_postpass_timeout():
    """Get Postpass API timeout from settings."""
    return getattr(settings, "METADATA_REFRESH_POSTPASS_TIMEOUT", 60)


def get_postpass_max_features():
    """Most OSM elements accepted for a single Wikidata item."""
    return getattr(settings, "METADATA_REFRESH_POSTPASS_MAX_FEATURES", 100)


class PostpassResultTruncated(RuntimeError):
    """Postpass matched more elements than we're willing to import.

    Raised instead of returning a partial list: the elements a truncated
    result omits would look stale and get deleted.
    """


def sync_osm_elements(subject, features):
    """Reconcile ``subject``'s OsmElements with the features Postpass returned.

    Identity is ``(osm_type, osm_id)``. A row that predates ``osm_type``
    (NULL) and belongs to this subject is claimed by the matching typed
    feature rather than replaced, so its primary key survives. Untyped rows
    under other subjects are left alone — they resolve when that subject
    refreshes — and new rows are always typed. Whatever the response didn't
    mention is deleted as stale, which for an empty response is everything.

    Returns ``(created, updated, deleted)`` counts.
    """
    untyped = {
        element.osm_id: element
        for element in subject.osm_elements.filter(osm_type__isnull=True)
    }
    kept_pks = set()
    created = updated = 0

    for feature in features:
        properties = feature["properties"]
        osm_type = properties["osm_type"]
        osm_id = properties["osm_id"]
        geometry = GEOSGeometry(json.dumps(feature["geometry"]))

        element = OsmElement.objects.filter(osm_type=osm_type, osm_id=osm_id).first()
        if element is None:
            element = untyped.pop(osm_id, None)
        if element is None:
            element, _ = OsmElement.objects.get_or_create(
                osm_type=osm_type,
                osm_id=osm_id,
                defaults={"subject": subject, "geometry": geometry},
            )
            created += 1
        else:
            element.osm_type = osm_type
            element.subject = subject
            element.geometry = geometry
            element.save()
            updated += 1
        kept_pks.add(element.pk)

    deleted = subject.osm_elements.exclude(pk__in=kept_pks).delete()[0]
    return created, updated, deleted


def _do_populate_osm_for_subject(subject):
    """
    Populate OSM elements for a subject that has a Wikidata item but no OSM elements.

    Creates an OsmElement for each OSM feature found with the subject's Wikidata ID.
    This handles cases like roads that span multiple OSM ways.
    """
    wikidata_id = subject.wikidata_item.wikidata_id
    logger.info(f"Populating OSM elements for {subject.title} ({wikidata_id})")

    # Mark as checked immediately to prevent concurrent tasks from picking this up
    subject.osm_last_checked = timezone.now()
    subject.save(update_fields=["osm_last_checked"])

    session = create_request_session()
    try:
        try:
            features = fetch_osm_features(session, wikidata_id)
        except PostpassResultTruncated as exc:
            logger.warning(f"Skipping {subject.title}: {exc}")
            return {"status": "skipped", "subject": subject.title, "message": str(exc)}

        if not features:
            logger.info(f"No OSM features found for {subject.title} ({wikidata_id})")
            return {"status": "no_data", "subject": subject.title}

        created, updated, _ = sync_osm_elements(subject, features)
        logger.info(f"Linked {created + updated} OSM element(s) to {subject.title}")

        return {
            "status": "success",
            "subject": subject.title,
            "created": created,
            "updated": updated,
        }

    finally:
        session.close()


def _do_refresh_osm_for_subject(subject):
    """
    Refresh all OSM elements for a subject.

    Fetches fresh data from OSM and updates/creates/deletes OsmElements as needed.
    This refreshes all elements for the subject in a single API call.
    """
    if not subject.wikidata_item:
        logger.warning(f"Subject {subject.title} has no Wikidata item")
        return {"status": "skipped", "message": "No Wikidata item"}

    wikidata_id = subject.wikidata_item.wikidata_id
    logger.info(f"Refreshing OSM elements for {subject.title} ({wikidata_id})")

    # Mark subject as checked immediately to prevent concurrent tasks
    subject.osm_last_checked = timezone.now()
    subject.save(update_fields=["osm_last_checked"])

    session = create_request_session()
    try:
        try:
            features = fetch_osm_features(session, wikidata_id)
        except PostpassResultTruncated as exc:
            logger.warning(f"Skipping {subject.title}: {exc}")
            return {"status": "skipped", "subject": subject.title, "message": str(exc)}

        created, updated, deleted = sync_osm_elements(subject, features)

        if not features:
            # An empty answer means the tag is gone from OSM; sync dropped everything
            logger.info(
                f"No OSM features found for {subject.title}; "
                f"deleted {deleted} element(s) no longer in OSM"
            )
            return {"status": "no_data", "subject": subject.title, "deleted": deleted}

        logger.info(
            f"Refreshed OSM elements for {subject.title}: "
            f"created {created}, updated {updated}, deleted {deleted}"
        )
        return {
            "status": "success",
            "subject": subject.title,
            "created": created,
            "updated": updated,
            "deleted": deleted,
        }

    finally:
        session.close()


def fetch_osm_features(
    session,
    wikidata_id: str,
    postpass_url: str | None = None,
    timeout: int | None = None,
) -> list:
    """Fetch OSM features worldwide from Postpass for a Wikidata item.

    Each feature's properties carry ``osm_id`` and ``osm_type`` (``N``, ``W``
    or ``R``). An empty list is a real answer — nothing in OSM carries the
    tag — so callers may delete on it; anything doubtful raises instead.

    Args:
        session: requests Session object
        wikidata_id: Wikidata ID (e.g., Q42)
        postpass_url: Optional override for Postpass API URL (defaults to settings)
        timeout: Optional override for request timeout (defaults to settings)

    Raises:
        PostpassResultTruncated: more matches than METADATA_REFRESH_POSTPASS_MAX_FEATURES
    """
    # Validate wikidata_id format to prevent SQL injection
    # Wikidata IDs are always Q followed by one or more digits (e.g., Q42, Q12345)
    if not re.match(r"^Q\d+$", wikidata_id):
        raise ValueError(f"Invalid Wikidata ID format: {wikidata_id}")

    postpass_url = postpass_url or get_postpass_url()
    timeout = timeout or get_postpass_timeout()
    max_features = get_postpass_max_features()
    tag_filter = json.dumps({"wikidata": wikidata_id})

    # JSON containment uses Postpass's GIN tag indexes; extracting text with
    # ->> cannot use those indexes and makes worldwide lookups expensive.
    # The combined view emits one row per geometry table, so a boundary
    # relation appears as both its outline and its area; DISTINCT ON keeps the
    # highest-dimension row. Fetching one past the cap tells a truncated
    # result apart from an exact fit.
    sql_query = f"""
    SELECT DISTINCT ON (osm_type, osm_id) osm_id, osm_type, tags, geom
    FROM postpass_pointlinepolygon
    WHERE tags @> '{tag_filter}'::jsonb
    ORDER BY osm_type, osm_id, ST_Dimension(geom) DESC
    LIMIT {max_features + 1}
    """

    response = session.post(
        postpass_url,
        data={"data": sql_query},
        timeout=timeout,
        headers={
            "User-Agent": "GeoreferenceTool/1.0 (https://github.com/mapRVA/georeference-tool)"
        },
    )
    response.raise_for_status()

    data = response.json()
    if "features" not in data:
        raise ValueError(f"Postpass response for {wikidata_id} has no features key")

    features = data["features"]
    if len(features) > max_features:
        raise PostpassResultTruncated(
            f"{wikidata_id} matches more than {max_features} OSM elements"
        )

    return features


# =============================================================================
# Coordinator Tasks (Beat-driven rate limiting)
# =============================================================================


def get_next_requested_wikidata_item():
    """Find the next WikidataItem an admin has manually queued.

    These jump ahead of the staleness rotation and ignore both the
    freshness threshold and the failure cap — the point of the button is
    to refresh an item *now*, including one that just failed its way out
    of the rotation. Still restricted to Subject/Region seeds, since a
    bare ancestor item has no closure of its own worth fetching.
    """

    return (
        WikidataItem.objects.filter(
            Q(subject__isnull=False) | Q(region__isnull=False),
            sparql_refresh_requested_at__isnull=False,
        )
        .order_by("sparql_refresh_requested_at")
        .first()
    )


def get_next_stale_wikidata_item():
    """
    Find the next WikidataItem whose graph closure needs refreshing.

    Only items attached to a Subject or a Region are refreshed on their
    own schedule. Ancestor items discovered via closure fetches don't need
    one: their mirrored data is replaced whenever a seed's closure includes
    them, and ``commit_closure_to_memgraph`` bumps their freshness
    timestamps then. Enrolling them here made the rotation unbounded —
    each ancestor refresh fetched *its* closure, discovering ever-deeper
    ancestors, until the queue (200k+ items) could never drain within the
    staleness window.
    """

    stale_hours = get_stale_threshold_hours()
    max_failures = get_max_failures()
    stale_threshold = timezone.now() - timedelta(hours=stale_hours)

    return (
        WikidataItem.objects.filter(
            Q(sparql_last_loaded_at__isnull=True)
            | Q(sparql_last_loaded_at__lt=stale_threshold),
            Q(subject__isnull=False) | Q(region__isnull=False),
            sparql_fetch_failures__lt=max_failures,
        )
        .order_by("sparql_last_loaded_at")
        .first()
    )


def get_next_subject_needing_osm():
    """Find the next Subject that has a Wikidata item but no OSM elements yet.

    Excludes subjects that have been checked recently (within stale threshold).
    """
    stale_hours = get_stale_threshold_hours()
    stale_threshold = timezone.now() - timedelta(hours=stale_hours)

    return (
        Subject.objects.filter(
            wikidata_item__isnull=False,
            osm_elements__isnull=True,
        )
        .filter(
            Q(osm_last_checked__isnull=True) | Q(osm_last_checked__lt=stale_threshold)
        )
        .order_by("osm_last_checked", "created_at")
        .first()
    )


def get_next_subject_needing_osm_refresh():
    """Find the next Subject with OSM elements that need refreshing.

    Returns subjects that have OSM elements and either:
    - osm_last_checked is null, or
    - osm_last_checked is older than the stale threshold
    """
    stale_hours = get_stale_threshold_hours()
    stale_threshold = timezone.now() - timedelta(hours=stale_hours)

    return (
        Subject.objects.filter(
            wikidata_item__isnull=False,
            osm_elements__isnull=False,
        )
        .filter(
            Q(osm_last_checked__isnull=True) | Q(osm_last_checked__lt=stale_threshold)
        )
        .distinct()
        .order_by("osm_last_checked")
        .first()
    )


@shared_task(ignore_result=True)
def refresh_next_wikidata_item():
    """
    Refresh a single stale WikidataItem.

    Called periodically by Celery Beat at the configured rate limit interval.
    This approach ensures global rate limiting regardless of worker count.
    """
    item = get_next_requested_wikidata_item() or get_next_stale_wikidata_item()
    if item is None:
        logger.debug("No stale WikidataItems to refresh")
        return {"status": "idle", "message": "No stale items"}

    # Perform the refresh inline (not queued) since Beat controls the rate
    return _do_refresh_wikidata_item(item)


@shared_task(ignore_result=True)
def hydrate_wikidata_item(wikidata_id):
    """Load a single WikidataItem's closure into Memgraph.

    Enqueued from ``WikidataItem.save()`` on the urgent queue after the
    cheap entity-JSON validation has already created the row. Same body
    as the Beat-driven refresher; if this task is dropped or fails, the
    Beat tick will eventually pick the row up (it still has
    ``sparql_last_loaded_at IS NULL``).
    """
    item = WikidataItem.objects.filter(wikidata_id=wikidata_id).first()
    if item is None:
        logger.warning(f"hydrate_wikidata_item: no row for {wikidata_id}")
        return {"status": "missing", "wikidata_id": wikidata_id}
    return _do_refresh_wikidata_item(item)


@shared_task(ignore_result=True)
def refresh_next_osm_element():
    """
    Populate or refresh OSM element data for a subject.

    Called periodically by Celery Beat at the configured rate limit interval.
    This approach ensures global rate limiting regardless of worker count.

    Priority:
    1. First, populate OSM elements for subjects that have Wikidata items but no OSM elements
    2. Then, refresh OSM elements for subjects that have stale data
    """
    # Priority 1: Subjects needing initial OSM population
    subject = get_next_subject_needing_osm()
    if subject is not None:
        return _do_populate_osm_for_subject(subject)

    # Priority 2: Subjects with stale OSM elements needing refresh
    subject = get_next_subject_needing_osm_refresh()
    if subject is not None:
        return _do_refresh_osm_for_subject(subject)

    logger.debug("No OSM elements to populate or refresh")
    return {"status": "idle", "message": "No elements to process"}


@shared_task(ignore_result=True)
def reconcile_project_graph():
    """Wholesale-rebuild the project-subject markers from Subject rows.

    The markers are maintained incrementally by post_save/post_delete
    signals, but those don't backfill after a fresh Memgraph volume or
    cover signal failures. This periodic reconcile self-heals any drift.
    """
    count = rebuild_project_graph()
    return {"status": "success", "markers": count}
