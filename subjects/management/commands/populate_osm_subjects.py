import time

import requests
from django.core.management.base import BaseCommand
from tqdm import tqdm

from subjects.models import Subject
from subjects.tasks import (
    PostpassResultTruncated,
    create_request_session,
    fetch_osm_features,
    get_postpass_timeout,
    get_postpass_url,
    sync_osm_elements,
)


class Command(BaseCommand):
    help = "Populate OSM elements for subjects that have Wikidata items attached"

    def add_arguments(self, parser):
        parser.add_argument(
            "--wait",
            type=int,
            default=10,
            help="Seconds to wait between API requests (default: 10)",
        )
        parser.add_argument(
            "--postpass-url",
            type=str,
            default=None,
            help="URL of the Postpass API instance (default: from settings)",
        )
        parser.add_argument(
            "--timeout",
            type=int,
            default=None,
            help="Timeout for API requests in seconds (default: from settings)",
        )
        parser.add_argument(
            "--refresh",
            action="store_true",
            help="Re-query all OSM elements for subjects with Wikidata items. Deletes OsmElements that are no longer found in OSM.",
        )

    def handle(self, *args, **options):
        wait_time = options["wait"]
        postpass_url = options["postpass_url"] or get_postpass_url()
        timeout = options["timeout"] or get_postpass_timeout()
        refresh = options["refresh"]

        if refresh:
            # Get all subjects with Wikidata items (regardless of OSM element status)
            subjects = Subject.objects.filter(wikidata_item__isnull=False)
            tqdm.write(self.style.SUCCESS("Running in refresh mode"))
        else:
            # Get all subjects with Wikidata items but no OSM elements
            subjects = Subject.objects.filter(
                wikidata_item__isnull=False, osm_elements__isnull=True
            )

        if not subjects.exists():
            tqdm.write(self.style.SUCCESS("No subjects found that need OSM elements"))
            return

        subject_list = list(subjects)
        subject_count = len(subject_list)
        tqdm.write(f"Found {subject_count} subjects to process")

        session = create_request_session()
        processed = 0
        skipped = 0
        deleted = 0

        progress = tqdm(subject_list, desc="Processing subjects", unit="subject")
        for i, subject in enumerate(progress):
            wikidata_id = subject.wikidata_item.wikidata_id
            progress.set_description(f"Processing {subject.title[:30]}")

            try:
                features = fetch_osm_features(
                    session, wikidata_id, postpass_url=postpass_url, timeout=timeout
                )
                created_count, updated_count, deleted_count = sync_osm_elements(
                    subject, features
                )
                deleted += deleted_count

                if not features:
                    tqdm.write(
                        self.style.WARNING(
                            f"  No OSM elements found for {subject.title} ({wikidata_id})"
                        )
                    )
                    if deleted_count:
                        tqdm.write(
                            self.style.SUCCESS(
                                f"  Deleted {deleted_count} OSM element(s) (no longer found in OSM)"
                            )
                        )
                    else:
                        skipped += 1
                else:
                    if deleted_count:
                        tqdm.write(
                            self.style.SUCCESS(
                                f"  Deleted {deleted_count} stale OSM element(s) for {subject.title}"
                            )
                        )
                    if created_count > 0 or updated_count > 0:
                        tqdm.write(
                            self.style.SUCCESS(
                                f"  {subject.title}: created {created_count}, updated {updated_count} OSM element(s)"
                            )
                        )
                    processed += 1

                # Wait before next request to be respectful to the API
                if i < subject_count - 1:
                    time.sleep(wait_time)

            except PostpassResultTruncated as e:
                tqdm.write(self.style.WARNING(f"  Skipping {subject.title}: {e}"))
                skipped += 1
            except requests.Timeout:
                tqdm.write(
                    self.style.WARNING(
                        f"  Request timed out for {subject.title}, skipping"
                    )
                )
                skipped += 1
            except requests.RequestException as e:
                tqdm.write(
                    self.style.WARNING(
                        f"  HTTP error for {subject.title}: {str(e)}, skipping"
                    )
                )
                skipped += 1
            except Exception as e:
                tqdm.write(
                    self.style.ERROR(f"  Error processing {subject.title}: {str(e)}")
                )
                skipped += 1

        progress.close()
        session.close()

        if refresh:
            tqdm.write(
                self.style.SUCCESS(
                    f"\nComplete! Processed: {processed}, Deleted: {deleted}, Skipped: {skipped}"
                )
            )
        else:
            tqdm.write(
                self.style.SUCCESS(
                    f"\nComplete! Processed: {processed}, Skipped: {skipped}"
                )
            )
