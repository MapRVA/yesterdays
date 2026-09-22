from django.core.management.base import BaseCommand

from images.models import Image
from images.tasks import generate_iiif_tiles
from images.utils import R2Uploader


class Command(BaseCommand):
    help = "Queue IIIF tile generation for images that don't have tiles yet."

    def add_arguments(self, parser):
        parser.add_argument(
            "--collection",
            type=int,
            help="Only process images in this collection ID",
        )
        parser.add_argument(
            "--image",
            type=int,
            help="Only process this specific image ID",
        )
        parser.add_argument(
            "--limit",
            type=int,
            help="Maximum number of images to queue",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-generate tiles even for images that already have them",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be queued without actually queuing",
        )
        parser.add_argument(
            "--migrate",
            action="store_true",
            help="Download originals from current permalink and upload to R2 bucket, then update permalink",
        )

    def _get_queryset(self, options):
        qs = Image.objects.all()

        if options["image"]:
            qs = qs.filter(pk=options["image"])
        elif options["collection"]:
            qs = qs.filter(collection_id=options["collection"])

        qs = qs.order_by("id")

        if options["limit"]:
            qs = qs[: options["limit"]]

        return qs

    def handle(self, **options):
        if options["migrate"]:
            self._handle_migrate(options)
            return

        qs = self._get_queryset(options)

        if not options["force"]:
            qs = qs.exclude(tile_status="complete")

        image_ids = list(qs.values_list("id", flat=True))

        if options["dry_run"]:
            self.stdout.write(
                f"Would queue {len(image_ids)} image(s) for tile generation."
            )
            return

        for image_id in image_ids:
            generate_iiif_tiles.delay(image_id)

        self.stdout.write(
            self.style.SUCCESS(f"Queued {len(image_ids)} image(s) for tile generation.")
        )

    def _handle_migrate(self, options):
        uploader = R2Uploader()
        qs = self._get_queryset(options)

        # Skip images whose permalink already points to the R2 bucket
        already_migrated = 0
        to_migrate = []
        for image in qs.only("id", "permalink"):
            if image.permalink and image.permalink.startswith(uploader.public_url_base):
                already_migrated += 1
            else:
                to_migrate.append(image)

        self.stdout.write(
            f"Found {len(to_migrate)} image(s) to migrate "
            f"({already_migrated} already in bucket)."
        )

        if options["dry_run"] or not to_migrate:
            return

        migrated = 0
        failed = 0
        for image in to_migrate:
            new_url = uploader.upload_original(image.id, image.permalink)
            if new_url:
                image.permalink = new_url
                image.save(update_fields=["permalink"])
                migrated += 1
                self.stdout.write(f"  Migrated image {image.id}")
            else:
                failed += 1
                self.stderr.write(f"  Failed to migrate image {image.id}")

        self.stdout.write(
            self.style.SUCCESS(
                f"Migration complete: {migrated} migrated, {failed} failed. "
                f"Asset/tile generation queued for migrated images."
            )
        )
