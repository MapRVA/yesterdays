"""
Signal handlers keeping derived data up-to-date as the catalogue changes.

Two independent concerns live here:

- The public_georeferences_mvt materialized view, refreshed when a
  Georeference changes or a Collection's / Source's 'public' status does.
- The denormalized stats tables, CollectionStats, CollectionRegionStats, and
  CollectionContributorStats, refreshed whenever images, georeferences, or
  region assignments change.
"""

import logging

from django.db import connection, transaction
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver
from regions.models import Region

from .models import (
    AerialGeoreference,
    Collection,
    CollectionContributorStats,
    CollectionRegionStats,
    CollectionStats,
    Georeference,
    Image,
    Source,
)
from .tasks import (
    reconcile_collection_contributor_stats,
    reconcile_collection_region_stats,
)
from .views.api import bump_tile_version

logger = logging.getLogger(__name__)


def refresh_tile_view(using_concurrent=True):
    """
    Refresh the public_georeferences_mvt materialized view.

    Args:
        using_concurrent (bool): Use CONCURRENT refresh (non-blocking).
                                 Requires unique index on the view.
    """
    try:
        method = (
            "REFRESH MATERIALIZED VIEW CONCURRENTLY"
            if using_concurrent
            else "REFRESH MATERIALIZED VIEW"
        )
        with connection.cursor() as cursor:
            cursor.execute(f"{method} public_georeferences_mvt")
        logger.debug(f"Successfully refreshed public_georeferences_mvt ({method})")
    except Exception as e:
        # Log warning but don't raise - the view will be eventually consistent
        logger.warning(f"Failed to refresh tile view: {str(e)}")


def refresh_tiles(using_concurrent=True):
    """
    Refresh the materialized view, then bump the tile version.

    The version must be bumped AFTER the view refresh completes, otherwise
    the new version's tiles may be served (and cached) with stale data.
    """

    refresh_tile_view(using_concurrent)
    bump_tile_version()


@receiver(post_save, sender=Georeference)
def refresh_view_on_georeference_save(sender, instance, created, **kwargs):
    """
    Refresh materialized view and invalidate tile cache when a georeference
    is created or updated.
    """
    transaction.on_commit(refresh_tiles)


@receiver(post_delete, sender=Georeference)
def refresh_view_on_georeference_delete(sender, instance, **kwargs):
    """
    Refresh materialized view and invalidate tile cache when a georeference
    is deleted.
    """
    transaction.on_commit(refresh_tiles)


@receiver(post_save, sender=Collection)
def refresh_view_on_collection_save(sender, instance, **kwargs):
    """
    Refresh materialized view and invalidate tile cache when a collection's
    public status changes.
    """
    update_fields = kwargs.get("update_fields")
    if update_fields is None or "public" in update_fields:
        transaction.on_commit(refresh_tiles)


@receiver(post_save, sender=Source)
def refresh_view_on_source_save(sender, instance, **kwargs):
    """
    Refresh materialized view and invalidate tile cache when a source's
    public status changes.
    """
    update_fields = kwargs.get("update_fields")
    if update_fields is None or "public" in update_fields:
        transaction.on_commit(refresh_tiles)


# ---------------------------------------------------------------------------
# CollectionStats / CollectionRegionStats / CollectionContributorStats
#
# Keep the denormalized per-collection statistics current the moment the
# underlying data changes, so browse pages always show correct numbers
# without running aggregate queries. Refreshes run on_commit, after the
# write is visible to other transactions.
#
# Every table refreshes a whole collection at a time, so no handler has to
# work out which regions a change moved images between. The one narrowing:
# a georeference write refreshes only its author's contributor rows.
# ---------------------------------------------------------------------------

# Image fields that affect CollectionStats counts. Saves restricted (via
# update_fields) to other fields skip the refresh.
STATS_RELEVANT_IMAGE_FIELDS = {
    "collection",
    "will_not_georef",
    "duplicate_of",
    "aerial",
}

# The same, for CollectionRegionStats, which additionally splits by region.
# Kept separate rather than folded into the set above so that a region-only
# save doesn't drag the sitewide table through a pointless recompute.
REGION_STATS_RELEVANT_IMAGE_FIELDS = STATS_RELEVANT_IMAGE_FIELDS | {"region"}


def _refresh_stats_on_commit(
    collection_ids, *, include_sitewide=True, contributor_ids=None
):
    """Queue a post-commit refresh of the denormalized stats tables.

    All tables refresh from a single callback: the collection-wide aggregate
    is the expensive part and every triggering write pays for it either way.
    Changes that only move images between regions — a collection's or source's
    region assignment — pass include_sitewide=False, since no sitewide count
    can have moved. Pass contributor_ids to limit the contributor refresh to
    those users' rows; by default it covers everyone in the collections.
    """
    ids = [cid for cid in collection_ids if cid is not None]
    if not ids:
        return
    if contributor_ids is not None:
        contributor_ids = [uid for uid in contributor_ids if uid is not None]

    refreshes = [
        (CollectionRegionStats, {}),
        (CollectionContributorStats, {"user_ids": contributor_ids}),
    ]
    if include_sitewide:
        refreshes.insert(0, (CollectionStats, {}))

    def _refresh():
        for model, kwargs in refreshes:
            # Log but don't raise: the triggering write already committed, and
            # a failed refresh only means stale stats until the next event or
            # the periodic reconcile. Caught per table so one failing doesn't
            # skip the others.
            try:
                model.refresh_for(ids, **kwargs)
            except Exception:
                logger.warning(
                    f"Failed to refresh {model.__name__} for {ids}", exc_info=True
                )

    transaction.on_commit(_refresh)


def _collection_id_of_image(image_id):
    return (
        Image.objects.filter(pk=image_id)
        .values_list("collection_id", flat=True)
        .first()
    )


@receiver(pre_save, sender=Georeference)
@receiver(pre_save, sender=AerialGeoreference)
def capture_previous_georeference(sender, instance, **kwargs):
    """Remember the image and author a georeference is moving away from.

    Submissions are append-only, but merging duplicates re-points
    georeferences at the kept image, which can sit in another collection.
    """
    instance._previous_image_author = None
    if not instance._state.adding:
        instance._previous_image_author = (
            sender.objects.filter(pk=instance.pk)
            .values_list("image_id", "georeferenced_by_id")
            .first()
        )


@receiver(post_save, sender=Georeference)
@receiver(post_delete, sender=Georeference)
@receiver(post_save, sender=AerialGeoreference)
@receiver(post_delete, sender=AerialGeoreference)
def refresh_stats_on_georeference_change(sender, instance, **kwargs):
    """Refresh the image's collection stats when a georeference changes.

    Only the author's contributor rows can change, so only those refresh —
    plus the previous image's collection and author when an update moved it.
    On cascade deletes triggered by an Image delete the image row may already
    be gone; the Image post_delete handler covers the refresh in that case.
    """
    collection_ids = {_collection_id_of_image(instance.image_id)}
    user_ids = {instance.georeferenced_by_id}
    previous = getattr(instance, "_previous_image_author", None)
    if previous:
        previous_image_id, previous_user_id = previous
        if previous_image_id != instance.image_id:
            collection_ids.add(_collection_id_of_image(previous_image_id))
        user_ids.add(previous_user_id)
    _refresh_stats_on_commit(collection_ids, contributor_ids=user_ids)


@receiver(pre_save, sender=Image)
def capture_previous_collection(sender, instance, **kwargs):
    """Remember the collection an image is moving away from, so its stats
    can be refreshed too."""
    instance._previous_collection_id = None
    update_fields = kwargs.get("update_fields")
    if instance.pk and (update_fields is None or "collection" in update_fields):
        instance._previous_collection_id = _collection_id_of_image(instance.pk)


@receiver(post_save, sender=Image)
def refresh_stats_on_image_save(sender, instance, **kwargs):
    update_fields = kwargs.get("update_fields")
    include_sitewide = True
    if update_fields is not None:
        fields = set(update_fields)
        if not (REGION_STATS_RELEVANT_IMAGE_FIELDS & fields):
            return
        # A save restricted to the image's own region moves it between
        # regions without changing any sitewide count
        include_sitewide = bool(STATS_RELEVANT_IMAGE_FIELDS & fields)
    previous = getattr(instance, "_previous_collection_id", None)
    ids = {instance.collection_id}
    if previous and previous != instance.collection_id:
        ids.add(previous)
    _refresh_stats_on_commit(ids, include_sitewide=include_sitewide)


@receiver(post_delete, sender=Image)
def refresh_stats_on_image_delete(sender, instance, **kwargs):
    _refresh_stats_on_commit([instance.collection_id])


@receiver(pre_save, sender=Collection)
def capture_collection_region_inputs(sender, instance, **kwargs):
    """Remember whether a collection's images are changing region.

    Both fields matter: `region` directly, and `source` because moving a
    collection to a source with a different region moves every image that
    doesn't set a region of its own.
    """
    instance._region_stats_stale = False
    update_fields = kwargs.get("update_fields")
    if instance.pk and (
        update_fields is None or {"region", "source"} & set(update_fields)
    ):
        previous = (
            Collection.objects.filter(pk=instance.pk)
            .values_list("region_id", "source_id")
            .first()
        )
        instance._region_stats_stale = previous is not None and previous != (
            instance.region_id,
            instance.source_id,
        )


@receiver(post_save, sender=Collection)
def refresh_stats_on_collection_save(sender, instance, created, **kwargs):
    """Seed a new collection's (zeroed) stats row, and recompute its region
    rows whenever the region its images inherit changes."""
    if created:
        _refresh_stats_on_commit([instance.pk])
    elif getattr(instance, "_region_stats_stale", False):
        _refresh_stats_on_commit([instance.pk], include_sitewide=False)


@receiver(pre_save, sender=Source)
def capture_source_region_change(sender, instance, **kwargs):
    """Remember whether a source's region is changing.

    Gated on an actual change rather than on the save itself: the admin saves
    with update_fields=None, and a Source save is already expensive (it
    rewrites the search vector of every image beneath it).
    """
    instance._region_changed = False
    update_fields = kwargs.get("update_fields")
    if instance.pk and (update_fields is None or "region" in update_fields):
        previous = (
            Source.objects.filter(pk=instance.pk)
            .values_list("region_id", flat=True)
            .first()
        )
        instance._region_changed = previous != instance.region_id


@receiver(post_save, sender=Source)
def refresh_region_stats_on_source_save(sender, instance, **kwargs):
    """Recompute region rows for the collections inheriting this source's
    region.

    Only collections without a region of their own are affected: resolution
    stops at the collection, so a collection that sets a region never reaches
    its source's.
    """
    if not getattr(instance, "_region_changed", False):
        return
    _refresh_stats_on_commit(
        instance.collections.filter(region__isnull=True).values_list("id", flat=True),
        include_sitewide=False,
    )


@receiver(pre_save, sender=Region)
def capture_region_rollup_change(sender, instance, **kwargs):
    """Remember whether a region is new or is being repointed at a different
    Wikidata item — the two things that change how images roll up."""
    instance._rollup_changed = instance.pk is None
    update_fields = kwargs.get("update_fields")
    if instance.pk and (update_fields is None or "wikidata_item" in update_fields):
        previous = (
            Region.objects.filter(pk=instance.pk)
            .values_list("wikidata_item_id", flat=True)
            .first()
        )
        instance._rollup_changed = previous != instance.wikidata_item_id


@receiver(post_save, sender=Region)
def reconcile_region_stats_on_region_save(sender, instance, **kwargs):
    """Recompute every region row when a region's place in the hierarchy
    changes.

    Creating "Virginia" as a Region makes every image already inside it start
    rolling up into it, and no image, collection, or source row changed to
    signal that — so a full reconcile is the only correct response. Region
    saves are rare and admin-only, so the cost is proportionate.

    Deletes need no handler: the rows CASCADE with the region, and PROTECT on
    the three region FKs means a region still in use cannot be deleted.
    """
    if not getattr(instance, "_rollup_changed", False):
        return

    def _reconcile():
        for task in (
            reconcile_collection_region_stats,
            reconcile_collection_contributor_stats,
        ):
            try:
                task.delay()
            except Exception:
                logger.warning(
                    f"Failed to queue {task.name} after saving region {instance.pk}",
                    exc_info=True,
                )

    transaction.on_commit(_reconcile)


@receiver(post_save, sender=Image)
def queue_image_processing(sender, instance, **kwargs):
    """
    Queue image processing (thumbnail generation and/or transforms) on every save.

    The task itself checks current state and only does work that's needed,
    so it's safe to queue on every save. Saves of rows without a permalink
    (e.g. the placeholder insert during API import, before the S3 copy) are
    skipped — there is nothing to download yet, and the save that later sets
    the permalink queues the task.
    """
    from .tasks import process_image

    if not instance.permalink:
        return

    def _queue():
        try:
            process_image.apply_async(args=[instance.id])
        except Exception as e:
            logger.error(
                f"Failed to queue image processing for Image {instance.id}: {e}"
            )

    transaction.on_commit(_queue)
