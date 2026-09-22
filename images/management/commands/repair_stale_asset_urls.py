from django.core.management.base import BaseCommand

from images.models import Image
from images.tasks import process_image


class Command(BaseCommand):
    help = (
        "Find images whose derived-asset URLs (thumbnail, transformed, IIIF) "
        "point at a generation directory other than the current "
        "asset_generation — dangling after cleanup_old_image_assets sweeps "
        "old generations — then clear the stale fields and queue "
        "process_image to regenerate them."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--collection-id",
            type=int,
            help="Only inspect images from a specific collection ID",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report affected images without modifying or queueing anything",
        )

    def handle(self, *args, **options):
        queryset = Image.objects.filter(asset_generation__gt=0)
        if options["collection_id"]:
            queryset = queryset.filter(collection_id=options["collection_id"])

        affected = []
        for (
            image_id,
            generation,
            thumbnail,
            transformed,
            iiif_url,
        ) in queryset.values_list(
            "id",
            "asset_generation",
            "thumbnail",
            "transformed_permalink",
            "iiif_url",
        ).iterator():
            prefix = f"/images/{image_id}/{generation}/"
            stale_fields = []
            if thumbnail and not thumbnail.endswith(f"{prefix}thumbnail.webp"):
                stale_fields.append("thumbnail")
            if transformed and not transformed.endswith(f"{prefix}transformed.webp"):
                stale_fields.append("transformed_permalink")
            if iiif_url and not iiif_url.endswith(f"{prefix}tiles"):
                stale_fields.append("iiif_url")
            if stale_fields:
                affected.append(image_id)
                self.stdout.write(
                    f"Image {image_id} (generation {generation}): "
                    f"stale {', '.join(stale_fields)}"
                )

        if not affected:
            self.stdout.write(self.style.SUCCESS("No stale asset URLs found."))
            return

        if options["dry_run"]:
            self.stdout.write(
                self.style.WARNING(
                    f"Dry run: {len(affected)} images have stale asset URLs; "
                    "nothing modified."
                )
            )
            return

        # Clear the derived fields with a queryset update (no post_save
        # signal), then queue process_image explicitly — it claims a fresh
        # generation and regenerates thumbnail, transform, and tiles.
        for image_id in affected:
            Image.objects.filter(pk=image_id).update(
                thumbnail=None,
                transformed_permalink=None,
                iiif_url=None,
                tile_status="",
                tile_error="",
            )
            process_image.delay(image_id)

        self.stdout.write(
            self.style.SUCCESS(
                f"Cleared stale asset URLs and queued regeneration for "
                f"{len(affected)} images."
            )
        )
