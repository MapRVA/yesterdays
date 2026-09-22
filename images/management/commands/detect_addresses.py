"""
Django management command to detect addresses in image titles and geocode them.

Usage:
    python manage.py detect_addresses
"""

import re
import time

from django.contrib.gis.geos import Point
from django.core.management.base import BaseCommand
from geopy.exc import GeocoderServiceError, GeocoderTimedOut, GeocoderUnavailable
from geopy.extra.rate_limiter import RateLimiter
from geopy.geocoders import Nominatim
from tqdm import tqdm

from images.models import Collection, Image, SiteSettings

# Regex pattern for street addresses with house numbers
# Handles patterns like:
#   "314 N. 36th St."
#   "103 - 105 - 107 N. 18th St." (extracts first number: 103)
#   "115-17-19 N. Lombary St." (extracts first complete number: 115)
#   "2013 Monument Ave."
#   "1708 Pump House Dr."
#   "428 N. Boulevard" (Boulevard as street name, no suffix)

# Street type suffixes (used in regex and for detection)
STREET_TYPES = (
    r"(?:St|Street)\.?",
    r"(?:Ave|Avenue)\.?",
    r"(?:Blvd)\.?",  # Note: "Boulevard" handled separately as it can be a street name
    r"(?:Dr|Drive)\.?",
    r"(?:Rd|Road)\.?",
    r"(?:Ct|Court)\.?",
    r"(?:Pl|Place)\.?",
    r"(?:Ln|Lane)\.?",
    r"Way",
    r"(?:Pkwy|Parkway)\.?",
    r"(?:Cir|Circle)\.?",
    r"(?:Ter|Terrace)\.?",
    r"(?:Hwy|Highway)\.?",
    r"Slip",  # e.g., Shockoe Slip
)

# Pattern for standard addresses with street type suffix
ADDRESS_WITH_SUFFIX_PATTERN = re.compile(
    r"""
    (\d+(?:\s*1/2)?)                  # House number with optional fraction (captured - first in sequence)
    (?:\s*-\s*[\d]+(?:\s*1/2)?)*      # Optional following house numbers (e.g., " - 204 - 206")
    \s+
    ((?:No|So|[NSEW])(?=\.|\s|$)\.?\s*)?  # Optional cardinal direction (N. S. E. W. or No. So.) - must be followed by dot, space, or end
    ((?![Bb][Ll][Oo][Cc][Kk]\s)(?:St\.?\s+)?[\w]+(?:\s+[\w]+)*?) # Street name: not "Block" alone (case-insensitive), optional "St." prefix, then words (non-greedy)
    \s+
    (                                 # Street type suffix
        """
    + "|".join(STREET_TYPES)
    + r"""
    )
    (?:\s|$|[.,])                     # Must be followed by whitespace, end, or punctuation
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Pattern for addresses where the street name IS the type (e.g., "428 N. Boulevard")
# or street names without standard suffixes (e.g., "St. James", "St. Paul")
ADDRESS_STREET_AS_NAME_PATTERN = re.compile(
    r"""
    (\d+(?:\s*1/2)?)                  # House number with optional fraction (captured - first in sequence)
    (?:\s*-\s*[\d]+(?:\s*1/2)?)*      # Optional following house numbers
    \s+
    ((?:No|So|[NSEW])(?=\.|\s|$)\.?\s*)?  # Optional cardinal direction (N. S. E. W. or No. So.) - must be followed by dot, space, or end
    (Boulevard|Plaza|Circle|Park|St\.?\s+\w+)  # Street name: type/place OR "St. [Name]" pattern
    (?:\s|$|[.,])                     # Must be followed by whitespace, end, or punctuation
    """,
    re.IGNORECASE | re.VERBOSE,
)


class Command(BaseCommand):
    help = "Detect addresses in image titles and geocode them using Nominatim"

    def add_arguments(self, parser):
        parser.add_argument(
            "--collection-id",
            type=int,
            help="Collection ID to process (skips interactive selection)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be detected without saving to database",
        )

    def handle(self, *args, **options):
        # Get collection - either from argument or interactive selection
        if options["collection_id"]:
            try:
                collection = Collection.objects.get(id=options["collection_id"])
            except Collection.DoesNotExist:
                self.stderr.write(
                    self.style.ERROR(
                        f"Collection with ID {options['collection_id']} not found"
                    )
                )
                return
        else:
            collection = self.select_collection_interactive()
            if collection is None:
                return

        self.stdout.write(
            self.style.SUCCESS(f"\nSelected collection: {collection.name}")
        )

        # Get images from this collection that:
        # - Don't already have detected_address
        # - Don't have source_point (higher priority hint)
        # - Are not already georeferenced (highest priority hint)
        images = (
            Image.objects.filter(
                collection=collection,
                detected_address__isnull=True,
                source_point__isnull=True,
            )
            .exclude(title__isnull=True)
            .exclude(title="")
            .exclude(georeferences__isnull=False)
            .select_related(
                "region",
                "collection__region",
                "collection__source__region",
            )
        )

        image_count = images.count()
        self.stdout.write(f"Found {image_count} images without detected addresses\n")

        if image_count == 0:
            self.stdout.write(self.style.WARNING("No images to process."))
            return

        dry_run = options["dry_run"]
        if dry_run:
            self.stdout.write(
                self.style.WARNING("DRY RUN - no changes will be saved\n")
            )

        site_settings = SiteSettings.load()

        # Initialize geocoder with rate limiting (2 seconds between calls)
        # Use a longer timeout to reduce transient failures
        geolocator = Nominatim(user_agent="YesterdaysMapRVA", timeout=10)
        # Disable RateLimiter's internal retries - we handle retries ourselves
        geocode = RateLimiter(
            geolocator.geocode,
            min_delay_seconds=2,
            max_retries=0,  # No internal retries, we handle this in geocode_address()
            swallow_exceptions=False,
        )

        # Process images
        matched_count = 0
        no_match_count = 0
        geocoded_count = 0
        geocode_failed_count = 0
        cache_hit_count = 0

        # Region is part of the key: the same street address can legitimately
        # resolve differently in two cities.
        address_cache = {}

        # Use tqdm for progress bar
        progress_bar = tqdm(
            images,
            total=image_count,
            desc="Processing images",
            unit="img",
        )

        for image in progress_bar:
            address = self.extract_address(image.title)

            if address:
                matched_count += 1
                progress_bar.write(
                    f"  {self.style.SUCCESS('MATCH')} [ID {image.id}]: "
                    f"{image.title!r} -> {address!r}"
                )

                if not dry_run:
                    region = image.effective_region
                    viewbox = self.get_search_viewbox(region, site_settings)
                    cache_key = (region.pk if region is not None else None, address)

                    # Check cache first
                    if cache_key in address_cache:
                        cached_coords = address_cache[cache_key]
                        if cached_coords:
                            lat, lon = cached_coords
                            point = Point(lon, lat, srid=4326)
                            image.detected_address = point
                            image.save(update_fields=["detected_address"])
                            cache_hit_count += 1
                            progress_bar.write(
                                f"    -> {self.style.SUCCESS('CACHED')}: "
                                f"({lat:.6f}, {lon:.6f})"
                            )
                        else:
                            # Cached as failed
                            geocode_failed_count += 1
                            progress_bar.write(
                                f"    -> {self.style.WARNING('GEOCODE FAILED')} (cached)"
                            )
                    else:
                        # Geocode the address
                        location = self.geocode_address(
                            geocode,
                            address,
                            progress_bar,
                            viewbox,
                            region.long_name if region is not None else None,
                        )

                        if location:
                            # Cache the result
                            address_cache[cache_key] = (
                                location.latitude,
                                location.longitude,
                            )
                            # Save to database
                            point = Point(
                                location.longitude, location.latitude, srid=4326
                            )
                            image.detected_address = point
                            image.save(update_fields=["detected_address"])
                            geocoded_count += 1
                            progress_bar.write(
                                f"    -> {self.style.SUCCESS('GEOCODED')}: "
                                f"({location.latitude:.6f}, {location.longitude:.6f})"
                            )
                        else:
                            # Cache the failure
                            address_cache[cache_key] = None
                            geocode_failed_count += 1
                            progress_bar.write(
                                f"    -> {self.style.WARNING('GEOCODE FAILED')}"
                            )
            else:
                no_match_count += 1
                if options["verbosity"] >= 2:
                    progress_bar.write(
                        f"  {self.style.WARNING('NO MATCH')}: {image.title!r}"
                    )

        # Summary
        self.stdout.write("\n" + "-" * 60)
        self.stdout.write(f"Addresses matched: {matched_count}")
        self.stdout.write(f"No address found: {no_match_count}")
        if not dry_run:
            self.stdout.write(f"Successfully geocoded: {geocoded_count}")
            self.stdout.write(f"From cache: {cache_hit_count}")
            self.stdout.write(f"Geocoding failed: {geocode_failed_count}")
        self.stdout.write(f"Total images: {image_count}")

    def select_collection_interactive(self):
        """Display collections and let user select one interactively."""
        collections = Collection.objects.select_related("source").order_by(
            "source__name", "name"
        )

        if not collections.exists():
            self.stderr.write(self.style.ERROR("No collections found in database."))
            return None

        self.stdout.write("\nAvailable collections:\n")
        self.stdout.write("-" * 60)

        collection_list = list(collections)
        current_source = None

        for idx, collection in enumerate(collection_list, 1):
            # Print source header when it changes
            if collection.source != current_source:
                current_source = collection.source
                self.stdout.write(
                    f"\n  {self.style.MIGRATE_HEADING(current_source.name)}"
                )

            # Count images that need processing (same criteria as handle())
            image_count = collection.images.count()
            needs_processing = (
                collection.images.filter(
                    detected_address__isnull=True,
                    source_point__isnull=True,
                )
                .exclude(title__isnull=True)
                .exclude(title="")
                .exclude(georeferences__isnull=False)
                .count()
            )

            self.stdout.write(
                f"    [{idx:3}] {collection.name} "
                f"({needs_processing}/{image_count} images to process)"
            )

        self.stdout.write("\n" + "-" * 60)

        # Get user selection
        while True:
            try:
                selection = input(
                    "\nEnter collection number (or 'q' to quit): "
                ).strip()

                if selection.lower() == "q":
                    self.stdout.write("Cancelled.")
                    return None

                idx = int(selection)
                if 1 <= idx <= len(collection_list):
                    return collection_list[idx - 1]
                else:
                    self.stderr.write(
                        self.style.ERROR(
                            f"Please enter a number between 1 and {len(collection_list)}"
                        )
                    )
            except ValueError:
                self.stderr.write(
                    self.style.ERROR("Please enter a valid number or 'q' to quit")
                )
            except KeyboardInterrupt, EOFError:
                self.stdout.write("\nCancelled.")
                return None

    def extract_address(self, title):
        """
        Extract a street address from an image title.

        Returns a normalized address string suitable for geocoding,
        or None if no valid address pattern is found.
        """
        if not title:
            return None

        # Try standard address pattern first (e.g., "314 N. 36th St.")
        match = ADDRESS_WITH_SUFFIX_PATTERN.search(title)
        if match:
            house_number = match.group(1)
            direction = match.group(2)  # May be None
            street_name = match.group(3)
            street_type = match.group(4)

            # Clean up street name (remove extra whitespace)
            street_name = " ".join(street_name.split())

            # Build the address string
            parts = [house_number]
            if direction:
                # Normalize direction (e.g., "N." -> "N", "No." -> "N", "So." -> "S")
                dir_normalized = direction.strip().rstrip(".")
                if dir_normalized.lower() == "no":
                    dir_normalized = "N"
                elif dir_normalized.lower() == "so":
                    dir_normalized = "S"
                parts.append(dir_normalized)
            parts.append(street_name)
            parts.append(street_type.rstrip("."))

            return " ".join(parts)

        # Try street-as-name pattern (e.g., "428 N. Boulevard")
        match = ADDRESS_STREET_AS_NAME_PATTERN.search(title)
        if match:
            house_number = match.group(1)
            direction = match.group(2)  # May be None
            street_name = match.group(3)

            # Build the address string
            parts = [house_number]
            if direction:
                # Normalize direction (e.g., "N." -> "N", "No." -> "N", "So." -> "S")
                dir_normalized = direction.strip().rstrip(".")
                if dir_normalized.lower() == "no":
                    dir_normalized = "N"
                elif dir_normalized.lower() == "so":
                    dir_normalized = "S"
                parts.append(dir_normalized)
            parts.append(street_name)

            return " ".join(parts)

        return None

    @staticmethod
    def get_search_viewbox(region, site_settings):
        """Return geopy's southwest/northeast viewbox for a region."""
        bbox = region.search_bbox if region is not None else None
        if bbox is None:
            bbox = [
                site_settings.default_search_bbox_west,
                site_settings.default_search_bbox_south,
                site_settings.default_search_bbox_east,
                site_settings.default_search_bbox_north,
            ]
        west, south, east, north = bbox
        return ((south, west), (north, east))

    def geocode_address(
        self,
        geocode,
        address,
        progress_bar,
        viewbox,
        region_name=None,
        max_retries=3,
    ):
        """
        Geocode an address using Nominatim, restricted to the effective search bbox.

        Args:
            geocode: Rate-limited geocode function
            address: Address string to geocode
            progress_bar: tqdm progress bar for output
            viewbox: ((south_lat, west_lon), (north_lat, east_lon)) tuple
            region_name: Region name appended to disambiguate the address
            max_retries: Number of times to retry on transient failures

        Returns:
            Location object with latitude/longitude, or None if not found
        """
        full_address = f"{address}, {region_name}" if region_name else address

        for attempt in range(max_retries):
            if attempt > 0:
                progress_bar.write(
                    f"    [Nominatim] Retry {attempt}/{max_retries - 1}..."
                )
                # Wait before retry (respecting rate limit)
                time.sleep(2)

            progress_bar.write(f"    [Nominatim] Geocoding: {full_address!r}")

            try:
                location = geocode(
                    full_address,
                    viewbox=viewbox,
                    bounded=True,  # Restrict results to viewbox
                    exactly_one=True,
                )
                return location

            except GeocoderTimedOut:
                progress_bar.write(
                    self.style.WARNING(f"    [Nominatim] Timeout for: {full_address!r}")
                )
                if attempt == max_retries - 1:
                    return None

            except GeocoderUnavailable as e:
                progress_bar.write(
                    self.style.WARNING(f"    [Nominatim] Unavailable: {e}")
                )
                if attempt == max_retries - 1:
                    return None

            except GeocoderServiceError as e:
                progress_bar.write(
                    self.style.ERROR(f"    [Nominatim] Service error: {e}")
                )
                return None  # Don't retry on service errors

        return None
