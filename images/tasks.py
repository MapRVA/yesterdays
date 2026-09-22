import logging
import uuid
from datetime import timedelta
from io import BytesIO

import requests
from celery import shared_task
from django.conf import settings
from django.db import connection, transaction
from django.db.models import F
from django.utils import timezone
from iiif_prezi3 import (
    Annotation,
    AnnotationBody,
    AnnotationPage,
    Canvas,
    Manifest,
    ServiceV3,
)
from PIL import Image as PILImage

from yesterdays.iiif import generate_and_upload_iiif_tiles

from .models import (
    CollectionEmbeddingStats,
    CollectionRegionStats,
    CollectionStats,
    DuplicateImagePair,
    Image,
    ImportSlot,
)
from .utils import R2Uploader, R2UploaderError, to_rgb

logger = logging.getLogger(__name__)

# How long a zeroed CollectionRegionStats row must sit untouched before the
# reconcile below drops it. An internal safety margin against deleting a row a
# concurrent refresh is about to rewrite, not a tunable.
EMPTY_REGION_STATS_TTL = timedelta(days=1)


@shared_task(bind=True, max_retries=3, default_retry_delay=60, ignore_result=True)
def process_image(self, image_id: int, quality: int = 85, force: bool = False):
    """
    Ensure an image has the correct transformed/plain assets on R2.

    This task is queued on every Image save. It checks the current state and
    does only the work needed:
    - If the image has a transform: apply it, upload full-size + thumbnail
    - If no transform but no thumbnail: generate a plain thumbnail
    - If everything is already correct: do nothing
    - If forced: regenerate all derived assets in a new generation

    A stale-write guard re-checks the DB before writing, so concurrent tasks
    from rapid saves won't clobber each other.
    """
    try:
        image = Image.objects.get(pk=image_id)
    except Image.DoesNotExist:
        return

    # Snapshot current state — used for stale-write guard later
    task_rotation = image.rotation
    task_mirror = image.mirror

    # Determine what work is needed
    needs_transform = image.has_transform and not image.transformed_permalink
    needs_thumbnail = not image.thumbnail
    needs_transform_cleanup = not image.has_transform and image.transformed_permalink
    # Legacy images predate versioned assets — their thumbnail/permalink point
    # at unversioned URLs, so re-emit them into a claimed generation directory.
    needs_migration = image.asset_generation == 0

    if (
        not force
        and not needs_transform
        and not needs_thumbnail
        and not needs_transform_cleanup
        and not needs_migration
    ):
        if image.tile_status != "complete":
            generate_iiif_tiles.delay(image_id)
        return

    # Bump asset_generation and claim a new generation directory for every
    # asset we're about to write. The CDN caches asset URLs for a year, so
    # reusing the same path would serve stale content even after R2 is
    # updated. select_for_update serializes concurrent process_image runs so
    # each one observes a distinct value.
    with transaction.atomic():
        Image.objects.select_for_update().only("id").get(pk=image_id)
        Image.objects.filter(pk=image_id).update(
            asset_generation=F("asset_generation") + 1,
        )
        generation = Image.objects.values_list("asset_generation", flat=True).get(
            pk=image_id
        )

    base_key = f"images/{image_id}/{generation}"

    try:
        r2_uploader = R2Uploader()

        pil_image = download_image(image.permalink)
        if pil_image is None:
            raise Exception(f"Failed to download image from {image.permalink}")

        if image.has_transform:
            transformed = transform_image(pil_image, image.rotation, image.mirror)

            transformed_bytes = BytesIO()
            transformed.save(transformed_bytes, "WEBP", quality=quality)
            transformed_bytes.seek(0)

            transformed_url = r2_uploader.upload_file_content(
                transformed_bytes.read(),
                f"{base_key}/transformed.webp",
                content_type="image/webp",
                overwrite=True,
            )

            thumb_source = transformed
        else:
            transformed_url = None
            thumb_source = pil_image

        thumb = create_thumbnail(thumb_source)
        thumb_bytes = BytesIO()
        thumb.save(thumb_bytes, "WEBP", quality=quality)
        thumb_bytes.seek(0)

        thumb_url = r2_uploader.upload_file_content(
            thumb_bytes.read(),
            f"{base_key}/thumbnail.webp",
            content_type="image/webp",
            overwrite=True,
        )

        if _transform_changed(image_id, task_rotation, task_mirror):
            return

        # Only persist if the generation this task claimed is still current.
        # A concurrent process_image run that claimed a newer generation
        # supersedes this one: cleanup_old_image_assets keeps only the
        # current generation's R2 directory, so persisting a superseded
        # generation's URLs would leave the DB pointing at objects that are
        # about to be deleted.
        # iiif_url is left alone: generate_iiif_tiles overwrites it in the same
        # statement that marks tiles complete, so the viewer switches the
        # moment the new tiles are on R2. Nulling it here would instead blank
        # the deep-zoom viewer for the whole tiling run — minutes, on a large
        # scan — while the previous generation's tiles sit there perfectly
        # serviceable. Clearing tile_status is what marks tiles as stale.
        updated = Image.objects.filter(pk=image_id, asset_generation=generation).update(
            transformed_permalink=transformed_url,
            thumbnail=thumb_url,
            tile_status="",
            tile_error="",
        )
        if not updated:
            logger.info(
                "Asset generation for image %d advanced past %d while "
                "processing; discarding superseded assets",
                image_id,
                generation,
            )
            return

    except R2UploaderError as e:
        raise self.retry(exc=e)
    except Exception:
        logger.exception("Failed to process image %d", image_id)
        return

    generate_iiif_tiles.delay(image_id)


def _transform_changed(image_id, expected_rotation, expected_mirror):
    """Check if the transform has changed since the task started."""
    current = Image.objects.only("rotation", "mirror").get(pk=image_id)
    if current.rotation != expected_rotation or current.mirror != expected_mirror:
        logger.info(
            "Transform changed for image %d while task was running, skipping write",
            image_id,
        )
        return True
    return False


@shared_task(ignore_result=True)
def process_images_batch(
    collection_id: int | None = None,
    image_ids: list[int] | None = None,
    quality: int = 85,
):
    """
    Queue image processing for multiple images.

    Args:
        collection_id: Optional collection to filter by
        image_ids: Optional specific image IDs to process
        quality: WEBP quality
    """
    queryset = Image.objects.all()

    if collection_id:
        queryset = queryset.filter(collection_id=collection_id)
    if image_ids:
        queryset = queryset.filter(id__in=image_ids)

    count = 0
    for image_id in queryset.values_list("id", flat=True):
        process_image.delay(image_id, quality=quality)
        count += 1

    return {"queued": count}


def transform_image(img: PILImage.Image, rotation: int, mirror: str) -> PILImage.Image:
    """Apply mirror and rotation transforms to a PIL Image.

    Order of operations: mirror first, then rotate (matching EXIF convention).
    """
    if mirror == "h":
        img = img.transpose(PILImage.FLIP_LEFT_RIGHT)
    elif mirror == "v":
        img = img.transpose(PILImage.FLIP_TOP_BOTTOM)

    if rotation == 90:
        img = img.transpose(PILImage.ROTATE_270)  # PIL rotates counter-clockwise
    elif rotation == 180:
        img = img.transpose(PILImage.ROTATE_180)
    elif rotation == 270:
        img = img.transpose(PILImage.ROTATE_90)

    return img


def download_image(url: str, timeout: int = 30) -> PILImage.Image | None:
    """Download an image from URL and return PIL Image."""
    try:
        response = requests.get(url, timeout=timeout, stream=True)
        response.raise_for_status()

        content_type = response.headers.get("content-type", "").lower()
        if not content_type.startswith("image/"):
            return None

        image_data = BytesIO(response.content)
        return to_rgb(PILImage.open(image_data))
    except Exception:
        return None


def create_thumbnail(img: PILImage.Image, max_dimension: int = 500) -> PILImage.Image:
    """Create a thumbnail with max_dimension longest side, preserving aspect ratio."""
    width, height = img.size

    if max(width, height) <= max_dimension:
        return img.copy()

    if width > height:
        scale_factor = max_dimension / width
    else:
        scale_factor = max_dimension / height

    new_width = int(width * scale_factor)
    new_height = int(height * scale_factor)

    return img.resize((new_width, new_height), PILImage.LANCZOS)


def _build_image_manifest(image, iiif_base, width, height, generation):
    """Build a static IIIF Presentation v3 manifest for a single Image."""
    manifest_id = (
        f"{settings.R2_PUBLIC_URL_BASE}/images/{image.id}/{generation}/manifest.json"
    )

    manifest = Manifest(
        id=manifest_id,
        label={"en": [image.title or f"Image {image.id}"]},
    )

    canvas_id = f"{manifest_id}#canvas"
    canvas = Canvas(
        id=canvas_id,
        label={"en": [image.title or f"Image {image.id}"]},
        height=height,
        width=width,
    )

    body = AnnotationBody(
        id=f"{iiif_base}/full/max/0/default.jpg",
        type="Image",
        format="image/jpeg",
        height=height,
        width=width,
    )
    service = ServiceV3(id=iiif_base, type="ImageService3", profile="level0")
    body.service = [service]

    anno = Annotation(
        id=f"{canvas_id}/anno",
        motivation="painting",
        body=body,
        target=canvas_id,
    )
    anno_page = AnnotationPage(id=f"{canvas_id}/page")
    anno_page.add_item(anno)
    canvas.add_item(anno_page)
    manifest.add_item(canvas)

    return manifest.json(indent=2)


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    ignore_result=True,
    time_limit=1800,
    soft_time_limit=1500,
)
def generate_iiif_tiles(self, image_id):
    """Generate IIIF tiles and a static manifest for an Image."""
    try:
        image = Image.objects.get(pk=image_id)
    except Image.DoesNotExist:
        return

    # Snapshot the source URL — used for a stale-write guard after tiling.
    source_url = image.display_permalink

    # process_image is the canonical bumper of asset_generation; we just read
    # whatever it set up and write tiles into the matching generation dir.
    generation = image.asset_generation
    if generation == 0:
        logger.info(
            "generate_iiif_tiles called for image %d with no asset generation; "
            "queueing process_image to bootstrap assets",
            image_id,
        )
        process_image.delay(image_id)
        return

    Image.objects.filter(pk=image_id).update(
        tile_status="processing",
        tile_error="",
    )

    try:
        r2_prefix = f"images/{image.id}/{generation}/tiles"
        width, height = generate_and_upload_iiif_tiles(
            source_url=source_url,
            r2_tiles_prefix=r2_prefix,
        )

        # Build and upload static IIIF manifest
        uploader = R2Uploader()
        iiif_base = uploader.get_public_url(r2_prefix)
        manifest_json = _build_image_manifest(
            image, iiif_base, width, height, generation
        )
        manifest_key = f"images/{image.id}/{generation}/manifest.json"
        uploader.upload_file_content(
            manifest_json.encode("utf-8"),
            manifest_key,
            content_type='application/ld+json;profile="http://iiif.io/api/presentation/3/context.json"',
            overwrite=True,
        )

        # Stale-write guard: if the display image changed while we were
        # tiling, discard results so the next process_image cycle re-queues
        # tile generation from the correct source.
        current = Image.objects.get(pk=image_id)
        if current.display_permalink != source_url:
            logger.info(
                "Display image changed for image %d while tiling, "
                "discarding stale tiles",
                image_id,
            )
            Image.objects.filter(pk=image_id).update(
                tile_status="",
                tile_error="",
            )
            return

        # Same generation guard as process_image: if a newer generation was
        # claimed while we were tiling, our tiles live in a directory that
        # cleanup_old_image_assets will delete — don't point the DB at them.
        # The newer run re-queues tiling for its own generation.
        updated = Image.objects.filter(pk=image_id, asset_generation=generation).update(
            tile_status="complete",
            tile_error="",
            iiif_url=iiif_base,
            width=width,
            height=height,
        )
        if not updated:
            logger.info(
                "Asset generation for image %d advanced past %d while "
                "tiling; discarding superseded tiles",
                image_id,
                generation,
            )
            return
        logger.info(
            "IIIF tiles generated for image %d (%dx%d)",
            image_id,
            width,
            height,
        )

        # Queue a delayed sweep of prior generation directories. The delay
        # gives any in-flight viewers time to finish loading assets from the
        # previous generation before they disappear.
        cleanup_old_image_assets.apply_async(args=[image_id], countdown=300)

    except Exception as exc:
        logger.exception("Failed to generate IIIF tiles for image %d", image_id)
        Image.objects.filter(pk=image_id).update(
            tile_status="failed",
            tile_error=str(exc),
        )
        raise self.retry(exc=exc)


@shared_task(ignore_result=True)
def cleanup_old_image_assets(image_id):
    """Delete R2 objects under previous generation directories for an image.

    Reads the authoritative ``asset_generation`` from the DB at run time so
    rapid successive regenerations can't race into deleting the valid current
    generation — any invocation keeps whatever is current at the moment it
    runs. Top-level files (e.g. ``images/{id}/original.jpg``) are preserved;
    only keys under a numeric ``{N}/`` subdirectory other than the current
    generation are deleted.
    """
    try:
        image = Image.objects.only("id", "asset_generation").get(pk=image_id)
    except Image.DoesNotExist:
        return

    keep_generation = image.asset_generation
    if keep_generation == 0:
        return

    prefix = f"images/{image_id}/"
    keep_subprefix = f"{prefix}{keep_generation}/"

    r2 = R2Uploader()
    to_delete = []
    for key in r2.iter_keys(prefix):
        if key.startswith(keep_subprefix):
            continue
        first_segment, _, _ = key[len(prefix) :].partition("/")
        if first_segment.isdigit():
            to_delete.append(key)

    if not to_delete:
        return

    r2.delete_files(to_delete)
    logger.info(
        "Cleaned up %d stale asset objects for image %d (keeping generation %d)",
        len(to_delete),
        image_id,
        keep_generation,
    )


@shared_task
def cleanup_stale_import_slots():
    """Delete import slots older than 24 hours and their temporary S3 files."""
    from datetime import timedelta

    from django.utils import timezone

    cutoff = timezone.now() - timedelta(hours=24)
    stale_slots = ImportSlot.objects.filter(created_at__lt=cutoff)
    count = stale_slots.count()

    if count == 0:
        return "No stale import slots found."

    try:
        r2 = R2Uploader()
        for slot in stale_slots:
            r2.delete_file(slot.s3_key)
    except R2UploaderError:
        logger.warning(
            "Failed to clean up some S3 files for stale import slots", exc_info=True
        )

    stale_slots.delete()
    return f"Cleaned up {count} stale import slot(s)."


@shared_task(ignore_result=True)
def reconcile_collection_stats():
    """Recompute every CollectionStats row from scratch.

    Signal handlers keep the stats current in real time; this periodic
    reconcile self-heals any drift from write paths that bypass signals
    (bulk updates, raw SQL).
    """
    CollectionStats.refresh_for()


@shared_task(ignore_result=True)
def reconcile_collection_region_stats():
    """Recompute every CollectionRegionStats row from scratch.

    Same role as reconcile_collection_stats for the per-region table, plus the
    garbage collection of rows for pairs that no longer have any images: the
    refresh zeroes those rather than deleting them, so that every write goes
    through the freshness guard.
    """
    CollectionRegionStats.refresh_for()
    # The refresh above re-stamped every pair that still has images, so a row
    # left at zero this long is definitively dead — no in-flight refresh can
    # still be about to write it.
    CollectionRegionStats.objects.filter(
        total_images=0, updated_at__lt=timezone.now() - EMPTY_REGION_STATS_TTL
    ).delete()


@shared_task(ignore_result=True)
def refresh_collection_embedding_stats():
    """Recompute every collection's mean CLIP embedding.

    These means power the style-neutral subject similarity query
    (subjects/similarity.py). The frequent refresh_next task below keeps them
    current between runs; this daily full refresh is the reconcile backstop
    for changes the count-mismatch check can't see (e.g. embeddings
    regenerated in place), and does the initial seeding after deploy.
    """
    CollectionEmbeddingStats.refresh_for()


@shared_task(ignore_result=True)
def refresh_next_collection_embedding_stats():
    """Refresh the most stale collection's mean embedding, if any.

    A collection is stale when its stored embedding_count differs from its
    current number of eligible images (including collections with no stats
    row yet). At most one collection is recomputed per run, so a bulk import
    costs one cheap counting scan per interval — never a recompute per image —
    and the most-drifted collection is fixed first.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT c.id
            FROM images_collection c
            LEFT JOIN images_collectionembeddingstats s ON s.collection_id = c.id
            LEFT JOIN images_image img ON img.collection_id = c.id
                AND {CollectionEmbeddingStats.ELIGIBLE_SQL.format(alias="img.")}
            GROUP BY c.id, s.embedding_count
            HAVING COUNT(img.id) IS DISTINCT FROM COALESCE(s.embedding_count, 0)
            ORDER BY ABS(COUNT(img.id) - COALESCE(s.embedding_count, 0)) DESC, c.id
            LIMIT 1
            """
        )
        row = cursor.fetchone()
    if row:
        CollectionEmbeddingStats.refresh_for([row[0]])


@shared_task(ignore_result=True)
def refresh_duplicate_image_pairs():
    """
    Rebuild the candidate visual-duplicate pair table (nightly).

    For every searchable, non-duplicate image, find its nearest neighbors by
    CLIP-embedding cosine distance via the partial HNSW index, then keep the
    globally closest settings.DUPLICATE_PAIRS_COUNT pairs. Binding each image's
    own embedding back as a %s::vector(768) parameter guarantees the index is
    used, exactly as semantic search does. The whole table is then replaced
    atomically.
    """
    neighbor_sql = """
        SELECT id, (embedding::vector(768) <=> %s::vector(768)) AS distance
        FROM images_image
        WHERE is_searchable = true
          AND embedding IS NOT NULL
          AND duplicate_of_id IS NULL
          AND id <> %s
        ORDER BY embedding::vector(768) <=> %s::vector(768)
        LIMIT %s
    """
    k = settings.DUPLICATE_PAIRS_NEIGHBORS
    batch_size = settings.DUPLICATE_PAIRS_BATCH_SIZE
    pairs = {}
    with connection.cursor() as cursor:
        cursor.execute("SET hnsw.ef_search = %s", [settings.DUPLICATE_PAIRS_EF_SEARCH])
        try:
            cursor.execute(
                "SELECT id FROM images_image "
                "WHERE is_searchable = true AND embedding IS NOT NULL "
                "AND duplicate_of_id IS NULL"
            )
            image_ids = [row[0] for row in cursor.fetchall()]
            for start in range(0, len(image_ids), batch_size):
                chunk = image_ids[start : start + batch_size]
                cursor.execute(
                    "SELECT id, embedding FROM images_image WHERE id = ANY(%s)",
                    [chunk],
                )
                # fetchall() materializes the chunk, freeing the cursor for the
                # per-image neighbor queries issued inside the loop.
                for image_id, embedding in cursor.fetchall():
                    cursor.execute(neighbor_sql, [embedding, image_id, embedding, k])
                    for neighbor_id, distance in cursor.fetchall():
                        key = (
                            (image_id, neighbor_id)
                            if image_id < neighbor_id
                            else (neighbor_id, image_id)
                        )
                        if key not in pairs or distance < pairs[key]:
                            pairs[key] = distance
        finally:
            cursor.execute("RESET hnsw.ef_search")

    closest = sorted(pairs.items(), key=lambda item: item[1])[
        : settings.DUPLICATE_PAIRS_COUNT
    ]
    # Preserve the uuid of any pair that persists across the rebuild so its
    # review url stays stable; new pairs fall back to a fresh uuid.
    existing_uuids = {
        (a_id, b_id): pair_uuid
        for a_id, b_id, pair_uuid in DuplicateImagePair.objects.values_list(
            "image_a_id", "image_b_id", "uuid"
        )
    }
    computed_at = timezone.now()
    with transaction.atomic():
        DuplicateImagePair.objects.all().delete()
        DuplicateImagePair.objects.bulk_create(
            [
                DuplicateImagePair(
                    uuid=existing_uuids.get((a_id, b_id)) or uuid.uuid4(),
                    image_a_id=a_id,
                    image_b_id=b_id,
                    distance=distance,
                    computed_at=computed_at,
                )
                for (a_id, b_id), distance in closest
            ]
        )
    logger.info(
        "refresh_duplicate_image_pairs: scanned %d images, stored %d pairs",
        len(image_ids),
        len(closest),
    )
