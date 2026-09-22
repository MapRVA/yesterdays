"""
Pre Collection importer

Allows the user to select a Pre Collection, and import any reviewed images
into a Collection.

Usage:
    uv run manage.py load_pre_collection
    uv run manage.py load_pre_collection --hotlink
"""

from django.core.management.base import BaseCommand
from tqdm import tqdm

from images.models import Collection, Image, PreCollection
from images.utils import R2Uploader, R2UploaderError


class Command(BaseCommand):
    help = "Import reviewed images from a PreCollection into a Collection."

    def add_arguments(self, parser):
        parser.add_argument(
            "--hotlink",
            action="store_true",
            help="Hotlink images instead of uploading to R2",
        )

    def handle(self, *args, **options):
        self.stdout.write("PreCollection Import Tool")
        self.stdout.write("=" * 40)

        # Step 1: Select PreCollection
        pre_collection = self.select_pre_collection()
        if not pre_collection:
            self.stdout.write("No PreCollection selected. Exiting.")
            return

        # Step 2: Check if there are reviewed images to import
        reviewed_images = pre_collection.images.filter(keep=True, imported=False)
        if not reviewed_images.exists():
            self.stdout.write(
                f"No unimported reviewed images found in '{pre_collection.name}'."
            )
            return

        # Step 3: Create or get Collection
        collection = self.create_collection_from_pre_collection(pre_collection)
        if not collection:
            self.stdout.write("Collection creation cancelled. Exiting.")
            return

        # Step 4: Import the images
        upload_to_r2 = not options["hotlink"]
        imported_count = self.import_pre_images_to_collection(
            pre_collection, collection, upload_to_r2=upload_to_r2
        )

        if imported_count > 0:
            self.stdout.write(
                self.style.SUCCESS(
                    f"\nSuccessfully imported {imported_count} images from PreCollection to Collection!"
                )
            )
            self.stdout.write(
                f"  PreCollection: {pre_collection.source.name} - {pre_collection.name}"
            )
            self.stdout.write(
                f"  Collection: {collection.source.name} - {collection.name}"
            )
        else:
            self.stdout.write("\nNo images were imported.")

    def list_pre_collections(self):
        """List all available PreCollections with their details"""
        pre_collections = PreCollection.objects.all().order_by("source__name", "name")

        if not pre_collections.exists():
            self.stdout.write("No PreCollections found.")
            return []

        self.stdout.write("\nAvailable PreCollections:")
        self.stdout.write("=" * 60)

        collection_data = []
        for i, pre_collection in enumerate(pre_collections, 1):
            reviewed_count = pre_collection.images.filter(keep=True).count()
            not_imported_count = pre_collection.images.filter(
                keep=True, imported=False
            ).count()
            total_count = pre_collection.images.count()

            self.stdout.write(
                f"{i:2d}. {pre_collection.source.name} - {pre_collection.name}"
            )
            self.stdout.write(f"    Total images: {total_count}")
            self.stdout.write(f"    Reviewed (keep=True): {reviewed_count}")
            self.stdout.write(f"    Not yet imported: {not_imported_count}")
            self.stdout.write(
                f"    Complete: {'Yes' if pre_collection.complete else 'No'}"
            )
            self.stdout.write("")

            collection_data.append(
                {
                    "index": i,
                    "pre_collection": pre_collection,
                    "reviewed_count": reviewed_count,
                    "not_imported_count": not_imported_count,
                    "total_count": total_count,
                }
            )

        return collection_data

    def select_pre_collection(self):
        """Allow user to select a PreCollection interactively"""
        collection_data = self.list_pre_collections()

        if not collection_data:
            return None

        while True:
            try:
                choice = int(
                    input(
                        f"Select a PreCollection (1-{len(collection_data)}) or 0 to exit: "
                    )
                )

                if choice == 0:
                    return None
                elif 1 <= choice <= len(collection_data):
                    selected = collection_data[choice - 1]
                    pre_collection = selected["pre_collection"]

                    self.stdout.write(
                        f"\nSelected: {pre_collection.source.name} - {pre_collection.name}"
                    )
                    self.stdout.write(f"Description: {pre_collection.description}")
                    self.stdout.write(f"URL: {pre_collection.url}")
                    self.stdout.write(
                        f"Images to import: {selected['not_imported_count']}"
                    )

                    confirm = input("\nConfirm this selection? [y/N] ").lower()
                    if confirm in ("y", "yes"):
                        return pre_collection
                else:
                    self.stdout.write(
                        f"Please enter a number between 1 and {len(collection_data)}"
                    )

            except ValueError, EOFError, KeyboardInterrupt:
                self.stdout.write("\nInvalid input. Please enter a number.")

    def create_collection_from_pre_collection(self, pre_collection):
        """Create or get a Collection based on the PreCollection"""
        existing_collection = Collection.objects.filter(
            source=pre_collection.source, name=pre_collection.name
        ).first()

        if existing_collection:
            self.stdout.write(f"Using existing Collection: {existing_collection.name}")
            return existing_collection

        self.stdout.write("\nCreating new Collection:")
        self.stdout.write(f"  Source: {pre_collection.source.name}")
        self.stdout.write(f"  Name: {pre_collection.name}")
        self.stdout.write(f"  URL: {pre_collection.url}")
        self.stdout.write(f"  Description: {pre_collection.description}")

        confirm = input("\nCreate this Collection? [y/N] ").lower()
        if confirm in ("y", "yes"):
            collection = Collection.objects.create(
                source=pre_collection.source,
                name=pre_collection.name,
                url=pre_collection.url,
                description=pre_collection.description,
                public=True,
            )
            self.stdout.write(
                self.style.SUCCESS(f"Created Collection: {collection.name}")
            )
            return collection
        else:
            return None

    def import_pre_images_to_collection(
        self, pre_collection, collection, upload_to_r2=True
    ):
        """Import reviewed PreImages into the Collection"""
        pre_images_to_import = pre_collection.images.filter(keep=True, imported=False)

        if not pre_images_to_import.exists():
            self.stdout.write("No reviewed images found to import.")
            return 0

        self.stdout.write(
            f"Found {pre_images_to_import.count()} reviewed images to import."
        )

        if upload_to_r2:
            try:
                r2_uploader = R2Uploader()
                self.stdout.write(
                    self.style.SUCCESS("R2 uploader initialized successfully.")
                )
            except R2UploaderError as e:
                self.stdout.write(
                    self.style.ERROR(f"R2 uploader initialization failed: {e}")
                )
                self.stdout.write(
                    "Images will be imported with original permalinks (hotlinked)."
                )
                upload_to_r2 = False

        imported_count = 0
        skipped_count = 0

        with tqdm(pre_images_to_import, desc="Importing images") as pbar:
            for pre_image in pbar:
                try:
                    existing_image = None
                    if pre_image.ref:
                        existing_image = Image.objects.filter(
                            collection=collection, ref=pre_image.ref
                        ).first()
                    else:
                        existing_image = Image.objects.filter(
                            collection=collection, title=pre_image.title
                        ).first()

                    if existing_image:
                        tqdm.write(f"      -> Image already exists: {pre_image.title}")
                        skipped_count += 1
                        pre_image.imported = True
                        pre_image.save(update_fields=["imported"])
                        continue

                    permalink = pre_image.permalink

                    if upload_to_r2:
                        try:
                            permalink = r2_uploader.upload_url(
                                pre_image.permalink,
                                in_tqdm=True,
                                raise_on_err=False,
                            )
                            if permalink is None:
                                tqdm.write(
                                    f"      x Failed to upload {pre_image.title}, using original URL"
                                )
                                permalink = pre_image.permalink
                        except Exception as e:
                            tqdm.write(
                                f"      x R2 upload error for {pre_image.title}: {e}"
                            )
                            permalink = pre_image.permalink

                    image = Image.objects.create(
                        collection=collection,
                        title=pre_image.title,
                        permalink=permalink,
                        description=pre_image.description or "",
                        license=pre_image.license,
                        creator=pre_image.creator,
                        ref=pre_image.ref,
                        original_date=pre_image.original_date,
                        edtf_date=pre_image.edtf_date,
                        source_point=pre_image.source_point,
                        original_url=pre_image.permalink,
                    )

                    pre_image.imported = True
                    pre_image.save(update_fields=["imported"])

                    imported_count += 1
                    tqdm.write(f"      -> Imported: {image.title} (ID: {image.id})")

                except Exception as e:
                    tqdm.write(f"      x Error importing {pre_image.title}: {e}")
                    continue

        self.stdout.write(self.style.SUCCESS("\nImport complete!"))
        self.stdout.write(f"  Imported: {imported_count} images")
        self.stdout.write(f"  Skipped (already exist): {skipped_count} images")

        return imported_count
