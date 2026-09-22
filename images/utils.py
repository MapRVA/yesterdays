"""
Utility functions for the images app.
Includes markdown rendering and HTML sanitization.
"""

import hashlib
import io
import logging
import mimetypes
import os
import ssl
import time
from xml.etree import ElementTree as etree

import boto3
import markdown
import nh3
import requests
from botocore.exceptions import ClientError
from django.db.models import F, Sum
from django.db.models.functions import Coalesce
from django.urls import reverse
from markdown.extensions import Extension
from markdown.inlinepatterns import InlineProcessor
from PIL import Image as PILImage
from tqdm import tqdm

# Allowed HTML tags for sanitized content
ALLOWED_TAGS = {"p", "br", "strong", "em", "u", "ul", "ol", "li", "blockquote", "a"}

# Allowed attributes per tag
ALLOWED_ATTRIBUTES = {
    "a": {"href", "title"},
}

# PIL modes that store more than 8 bits per channel. A direct .convert("RGB")
# on these clamps any value above 255 to white instead of rescaling.
_HIGH_DEPTH_MODES = ("I", "I;16", "I;16B", "I;16L", "I;16N", "F")


def to_rgb(img: PILImage.Image) -> PILImage.Image:
    """Convert a PIL image to 8-bit RGB, downscaling high-bit-depth sources.

    Pillow's direct .convert("RGB") on 16-bit modes (e.g. a 16-bit grayscale
    TIFF scan, mode "I;16") clamps every value above 255 to white instead of
    rescaling, producing a blank thumbnail. Map the 16-bit range down to 8 bits
    (high byte) first; ordinary 8-bit images pass straight through unchanged.
    """
    if img.mode in _HIGH_DEPTH_MODES:
        img = img.convert("I").point(lambda v: v * (1 / 256)).convert("L")
    return img.convert("RGB")


class ImageReferenceProcessor(InlineProcessor):
    """
    Processor for converting #1234 image references to links.
    Pattern: #DIGITS (e.g., #1234)
    Works at start of lines, in the middle of text, and after whitespace.
    """

    def __init__(self, pattern, markdown_instance):
        super().__init__(pattern, markdown_instance)

    def handleMatch(self, m, data):
        """Handle matches of image reference pattern."""
        image_id = m.group(1)

        # Create an anchor element
        el = etree.Element("a")
        el.text = f"#{image_id}"

        # Generate URL to image detail page
        try:
            el.set(
                "href", reverse("images:image_detail", kwargs={"image_id": image_id})
            )
            el.set("title", f"Image #{image_id}")
        except Exception:
            # If URL generation fails, just return the text as-is
            el.text = f"#{image_id}"

        return el, m.start(0), m.end(0)


class ImageReferenceExtension(Extension):
    """
    Extension to convert #DIGITS to links to image detail pages.
    """

    def extendMarkdown(self, md):
        """Register the image reference processor with markdown."""
        pattern = r"#(\d+)"
        processor = ImageReferenceProcessor(pattern, md)
        md.inlinePatterns.register(processor, "image_reference", 190)


class NoHeadersExtension(Extension):
    """
    Extension to disable heading parsing.
    This allows #1234 to be treated as content, not as heading syntax.
    """

    def extendMarkdown(self, md):
        """Remove the heading processors to allow # in content."""
        # Deregister hash-style heading processor (#, ##, etc.)
        md.parser.blockprocessors.deregister("hashheader")
        # Deregister setext-style heading processor (underline style)
        md.parser.blockprocessors.deregister("setextheader")


def render_markdown(text):
    """
    Convert markdown text to HTML.

    Disables heading syntax to allow #1234 image references.

    Args:
        text (str): Markdown text to render

    Returns:
        str: HTML string (not yet sanitized)
    """
    if not text:
        return ""

    # Convert markdown to HTML with custom extensions
    html = markdown.markdown(
        text,
        extensions=[
            NoHeadersExtension(),
            ImageReferenceExtension(),
        ],
    )

    return html


def sanitize_html(html_string):
    """
    Sanitize HTML by removing potentially dangerous elements and attributes.
    Uses nh3 for safe HTML filtering.

    Args:
        html_string (str): HTML to sanitize

    Returns:
        str: Sanitized HTML
    """
    if not html_string:
        return ""

    # Sanitize using nh3 with our allowed tags and attributes
    sanitized = nh3.clean(
        html_string,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
    )

    return sanitized


def render_markdown_safe(text):
    """
    Render markdown to HTML and sanitize the result.
    Safe wrapper combining render_markdown and sanitize_html.

    Args:
        text (str): Markdown text to render

    Returns:
        str: Sanitized HTML
    """
    if not text:
        return ""

    # First convert markdown to HTML
    html = render_markdown(text)

    # Then sanitize the result
    sanitized = sanitize_html(html)

    return sanitized


# Plain-language bearings for a Georeference.direction (0-359, 0 = north).
# Sixteen points would be more precise than a georeference usually is; eight
# is the resolution a sentence can carry ("facing north-east").
COMPASS_POINTS = (
    "north",
    "north-east",
    "east",
    "south-east",
    "south",
    "south-west",
    "west",
    "north-west",
)


def compass_label(degrees):
    """Name the bearing ``degrees`` points at: "north-east" for 47.

    None in, None out, so callers can render the direction clause only
    when there is a direction to render.
    """
    if degrees is None:
        return None
    # Modulo after rounding, not before: 359 rounds up to the 8th octant,
    # which is north again rather than an off-the-end index.
    return COMPASS_POINTS[round(degrees / 45) % 8]


def _public_collection_stats(region=None):
    """Stats rows for publicly visible collections.

    Sitewide these are CollectionStats rows; for a region they are that
    region's CollectionRegionStats rows, which already roll every image up
    its P131 chain, so filtering on ``region`` alone covers the region and
    everything inside it. Both tables count non-public collections and
    sources too, so visibility is applied here, at read time.
    """
    from .models import CollectionRegionStats, CollectionStats

    if region is None:
        rows = CollectionStats.objects.all()
    else:
        rows = CollectionRegionStats.objects.filter(region=region)
    return rows.filter(collection__public=True, collection__source__public=True)


def get_confidence_breakdown(region=None):
    """
    Get per-confidence image counts, avoiding double-counting from-above images.

    Reads the denormalized CollectionStats table (maintained eagerly by
    signals), or CollectionRegionStats when ``region`` is given. Each image is
    counted exactly once under its most-recent georeference's confidence
    level (or ``not_georeferenced`` if it has none).

    Returns:
        dict with keys ``not_georeferenced``, ``low``, ``medium``, ``high``,
        each mapping to an integer count.
    """
    agg = _public_collection_stats(region).aggregate(
        total=Coalesce(Sum(F("total_images") - F("will_not_georef_images")), 0),
        low=Coalesce(Sum("georeferenced_low"), 0),
        medium=Coalesce(Sum("georeferenced_medium"), 0),
        high=Coalesce(Sum("georeferenced_high"), 0),
    )
    georeferenced = agg["low"] + agg["medium"] + agg["high"]
    return {
        "not_georeferenced": agg["total"] - georeferenced,
        "low": agg["low"],
        "medium": agg["medium"],
        "high": agg["high"],
    }


def get_overall_stats(region=None):
    """
    Get overall site statistics for images.

    Calculates totals for sources, collections, images, and georeferenced images.
    Excludes duplicates and images marked as will_not_georef from totals.
    Includes both point georeferences and aerial georeferences.

    With ``region``, every figure is scoped to that region and the regions
    inside it: images by their effective region, and sources/collections to
    those that hold at least one such image.

    Returns:
        dict: Statistics containing:
            - total_sources: Count of public sources
            - total_collections: Count of public collections
            - total_images: Count of eligible images
            - total_georeferenced: Count of georeferenced images
            - georeferenced_percentage: Percentage georeferenced
    """
    from .models import Collection, Source

    rows = _public_collection_stats(region)
    agg = rows.aggregate(
        total=Coalesce(Sum(F("total_images") - F("will_not_georef_images")), 0),
        georeferenced=Coalesce(
            Sum(
                F("georeferenced_low")
                + F("georeferenced_medium")
                + F("georeferenced_high")
            ),
            0,
        ),
    )
    total_images = agg["total"]
    georeferenced_count = agg["georeferenced"]

    if region is None:
        total_sources = Source.objects.filter(public=True).count()
        total_collections = Collection.objects.filter(
            public=True, source__public=True
        ).count()
    else:
        # A (collection, region) row lingers at zero for a while after its
        # last image leaves, so count only rows that still hold images.
        populated = rows.filter(total_images__gt=0)
        total_sources = populated.values("collection__source").distinct().count()
        total_collections = populated.values("collection").distinct().count()

    georeferenced_percentage = (
        round((georeferenced_count / total_images * 100), 1) if total_images > 0 else 0
    )

    return {
        "total_sources": total_sources,
        "total_collections": total_collections,
        "total_images": total_images,
        "total_georeferenced": georeferenced_count,
        "georeferenced_percentage": georeferenced_percentage,
    }


class R2UploaderError(Exception):
    """Custom exception for R2 uploader errors"""

    pass


class R2Uploader:
    """Upload files to Cloudflare R2 storage"""

    def __init__(self):
        """Initialize R2 client with environment variables"""
        self.endpoint_url = os.getenv("IMPORT_R2_ENDPOINT_URL")
        self.access_key_id = os.getenv("IMPORT_R2_ACCESS_KEY_ID")
        self.secret_access_key = os.getenv("IMPORT_R2_SECRET_ACCESS_KEY")
        self.region = os.getenv("IMPORT_R2_REGION", "auto")
        self.bucket_name = os.getenv("IMPORT_R2_BUCKET_NAME")
        self.public_url_base = os.getenv("IMPORT_R2_PUBLIC_URL_BASE")

        # Validate required environment variables
        self._validate_config()

        # Initialize S3 client for R2 (retry on transient SSL init errors)
        logger = logging.getLogger(__name__)
        for attempt in range(3):
            try:
                self.s3_client = boto3.client(
                    service_name="s3",
                    endpoint_url=self.endpoint_url,
                    aws_access_key_id=self.access_key_id,
                    aws_secret_access_key=self.secret_access_key,
                    region_name=self.region,
                )
                break
            except ssl.SSLError:
                if attempt < 2:
                    logger.warning(
                        "SSLError creating S3 client, retrying (attempt %d/3)",
                        attempt + 1,
                    )
                    time.sleep(0.1 * (attempt + 1))
                else:
                    raise

        # Set default public URL base if not provided
        if not self.public_url_base:
            # Extract account ID from endpoint URL
            # https://<accountid>.r2.cloudflarestorage.com -> https://<bucket>.<accountid>.r2.cloudflarestorage.com
            if self.endpoint_url and self.bucket_name:
                account_part = self.endpoint_url.replace("https://", "").replace(
                    ".r2.cloudflarestorage.com", ""
                )
                self.public_url_base = f"https://{self.bucket_name}.{account_part}.r2.cloudflarestorage.com"

    def _validate_config(self):
        """Validate that all required environment variables are set"""
        required_vars = [
            ("IMPORT_R2_ENDPOINT_URL", self.endpoint_url),
            ("IMPORT_R2_ACCESS_KEY_ID", self.access_key_id),
            ("IMPORT_R2_SECRET_ACCESS_KEY", self.secret_access_key),
            ("IMPORT_R2_BUCKET_NAME", self.bucket_name),
        ]

        missing_vars = [
            var_name for var_name, var_value in required_vars if not var_value
        ]

        if missing_vars:
            raise R2UploaderError(
                f"Missing required environment variables: {', '.join(missing_vars)}\n"
                f"Please set all IMPORT_R2_* environment variables before using the R2 uploader."
            )

    def file_exists(self, key):
        """Check if a file already exists in the bucket"""
        try:
            self.s3_client.head_object(Bucket=self.bucket_name, Key=key)
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                return False
            else:
                raise R2UploaderError(f"Error checking file existence: {e}")

    def upload_url(
        self,
        source_url,
        overwrite=False,
        timeout=30,
        in_tqdm=False,
        raise_on_err=True,
        cache_control="public, max-age=31536000",
    ):
        """
        Download a file from URL and upload to R2 bucket

        Args:
            source_url (str): URL to download the file from
            key (str): S3 key (path) where to store the file in the bucket
            overwrite (bool): Whether to overwrite existing files
            timeout (int): Timeout for downloading the source file
            in_tqdm (bool): If True, use tqdm to print messages
            raise_on_err (bool): If True, raise an Exception when there is a download error
            cache_control (str): Cache-Control header to set on the uploaded object

        Returns:
            str: Public URL of the uploaded file

        Raises:
            R2UploaderError: If upload fails
        """
        key = self.generate_key_from_url(source_url)

        # Check if file already exists
        if not overwrite and self.file_exists(key):
            print(f"  File already exists in R2: {key}")
            return self.get_public_url(key)

        try:
            # Download the file from the source URL
            if tqdm:
                tqdm.write(f"  Downloading from: {source_url}")
            else:
                print(f"  Downloading from: {source_url}")
            headers = {"User-Agent": "Yesterdays/1.0 (https://maprva.org)"}
            response = requests.get(
                source_url, timeout=timeout, stream=True, headers=headers
            )
            response.raise_for_status()

            # Get file content
            file_content = response.content

            # Determine content type
            content_type = response.headers.get("content-type")
            if not content_type:
                # Guess content type from URL
                content_type, _ = mimetypes.guess_type(source_url)
                if not content_type:
                    content_type = "application/octet-stream"

            # Upload to R2
            if tqdm:
                tqdm.write(f"  Uploading to R2: {key}")
            else:
                print(f"  Uploading to R2: {key}")
            self.s3_client.upload_fileobj(
                io.BytesIO(file_content),
                self.bucket_name,
                key,
                ExtraArgs={
                    "ContentType": content_type,
                    "CacheControl": cache_control,
                },
            )

            public_url = self.get_public_url(key)
            if tqdm:
                tqdm.write(f"  ✓ Uploaded to R2: {public_url}")
            else:
                print(f"  ✓ Uploaded to R2: {public_url}")
            return public_url

        except requests.RequestException as e:
            if raise_on_err:
                raise
            else:
                if tqdm:
                    tqdm.write(f"Failed to download from {source_url}: {e}")
                else:
                    print(f"Failed to download from {source_url}: {e}")
        except ClientError as e:
            raise R2UploaderError(f"Failed to upload to R2: {e}")
        except Exception as e:
            raise R2UploaderError(f"Unexpected error during upload: {e}")

    def upload_file_content(
        self,
        file_content,
        key,
        content_type=None,
        overwrite=False,
        cache_control="public, max-age=31536000",
    ):
        """
        Upload file content directly to R2 bucket

        Args:
            file_content (bytes): Raw file content to upload
            key (str): S3 key (path) where to store the file in the bucket
            content_type (str): MIME type of the file (optional)
            overwrite (bool): Whether to overwrite existing files
            cache_control (str): Cache-Control header to set on the uploaded object

        Returns:
            str: Public URL of the uploaded file

        Raises:
            R2UploaderError: If upload fails
        """
        # Check if file already exists
        if not overwrite and self.file_exists(key):
            print(f"  File already exists in R2: {key}")
            return self.get_public_url(key)

        try:
            # Set default content type
            if not content_type:
                content_type, _ = mimetypes.guess_type(key)
                if not content_type:
                    content_type = "application/octet-stream"

            # Upload to R2
            print(f"  Uploading to R2: {key}")
            self.s3_client.upload_fileobj(
                io.BytesIO(file_content),
                self.bucket_name,
                key,
                ExtraArgs={
                    "ContentType": content_type,
                    "CacheControl": cache_control,
                },
            )

            public_url = self.get_public_url(key)
            print(f"  ✓ Uploaded to R2: {public_url}")
            return public_url

        except ClientError as e:
            raise R2UploaderError(f"Failed to upload to R2: {e}")
        except Exception as e:
            raise R2UploaderError(f"Unexpected error during upload: {e}")

    def delete_file(self, key):
        """
        Delete a file from the R2 bucket

        Args:
            key (str): S3 key of the file to delete

        Returns:
            bool: True if deletion was successful

        Raises:
            R2UploaderError: If deletion fails
        """
        try:
            self.s3_client.delete_object(Bucket=self.bucket_name, Key=key)
            print(f"  ✓ Deleted from R2: {key}")
            return True
        except ClientError as e:
            raise R2UploaderError(f"Failed to delete from R2: {e}")

    def iter_keys(self, prefix):
        """Yield every object key in the bucket that begins with *prefix*."""
        paginator = self.s3_client.get_paginator("list_objects_v2")
        try:
            for page in paginator.paginate(Bucket=self.bucket_name, Prefix=prefix):
                for obj in page.get("Contents", []) or []:
                    yield obj["Key"]
        except ClientError as e:
            raise R2UploaderError(f"Failed to list R2 objects: {e}")

    def delete_files(self, keys):
        """Delete many objects in batches of 1000 (the S3 DeleteObjects limit)."""
        batch = []
        for key in keys:
            batch.append({"Key": key})
            if len(batch) == 1000:
                self._delete_batch(batch)
                batch = []
        if batch:
            self._delete_batch(batch)

    def _delete_batch(self, batch):
        try:
            self.s3_client.delete_objects(
                Bucket=self.bucket_name,
                Delete={"Objects": batch, "Quiet": True},
            )
        except ClientError as e:
            raise R2UploaderError(f"Failed to delete R2 objects: {e}")

    def upload_original(
        self, image_id, source_url, timeout=30, in_tqdm=False, max_retries=3
    ):
        """
        Download a file from URL and upload to R2 at images/<ID>/original.<ext>.

        Args:
            image_id (int): Image ID for the R2 key path
            source_url (str): URL to download the file from
            timeout (int): Timeout for downloading the source file
            in_tqdm (bool): If True, use tqdm to print messages
            max_retries (int): Number of attempts for the source download on
                transient failures (5xx, connection/timeout errors)

        Returns:
            str: Public URL of the uploaded file, or None on download failure

        Raises:
            R2UploaderError: If upload fails
        """
        key = f"images/{image_id}/original"

        # Check if file already exists
        if self.file_exists(key):
            return self.get_public_url(key)

        _print = tqdm.write if in_tqdm else print

        response = None
        for attempt in range(max_retries):
            try:
                _print(f"  Downloading from: {source_url}")
                response = requests.get(source_url, timeout=timeout, stream=True)
                response.raise_for_status()
                break
            except requests.RequestException as e:
                status = e.response.status_code if e.response is not None else None
                is_retryable = isinstance(
                    e, (requests.ConnectionError, requests.Timeout)
                ) or (status is not None and 500 <= status < 600)
                if attempt + 1 < max_retries and is_retryable:
                    delay = 2**attempt
                    _print(
                        f"  Download failed (attempt {attempt + 1}/{max_retries}, "
                        f"retrying in {delay}s): {e}"
                    )
                    time.sleep(delay)
                    continue
                _print(f"Failed to download from {source_url}: {e}")
                return None

        try:
            file_content = response.content

            # Determine content type and extension
            content_type = response.headers.get("content-type")
            if not content_type:
                content_type, _ = mimetypes.guess_type(source_url)
                if not content_type:
                    content_type = "application/octet-stream"

            ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ""
            # mimetypes returns .jpe for image/jpeg on some systems
            if ext in (".jpe", ".jpeg"):
                ext = ".jpg"
            key = f"images/{image_id}/original{ext}"

            _print(f"  Uploading to R2: {key}")
            self.s3_client.upload_fileobj(
                io.BytesIO(file_content),
                self.bucket_name,
                key,
                ExtraArgs={
                    "ContentType": content_type,
                    "CacheControl": "public, max-age=31536000",
                },
            )

            public_url = self.get_public_url(key)
            _print(f"  ✓ Uploaded to R2: {public_url}")
            return public_url

        except ClientError as e:
            raise R2UploaderError(f"Failed to upload to R2: {e}")

    def generate_presigned_put_url(self, key, content_type, expiration=300):
        """Generate a presigned PUT URL for direct browser uploads."""
        return self.s3_client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self.bucket_name,
                "Key": key,
                "ContentType": content_type,
            },
            ExpiresIn=expiration,
        )

    def get_public_url(self, key):
        """
        Get the public URL for a file in the bucket

        Args:
            key (str): S3 key of the file

        Returns:
            str: Public URL of the file
        """
        if self.public_url_base:
            return f"{self.public_url_base}/{key}"
        else:
            # Fallback to constructing URL from endpoint
            return f"{self.endpoint_url}/{self.bucket_name}/{key}"

    def head_object(self, key):
        """Return metadata for an S3 object, or None if it doesn't exist."""
        try:
            return self.s3_client.head_object(Bucket=self.bucket_name, Key=key)
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                return None
            raise

    def copy_object(self, source_key, dest_key):
        """Copy an object within the same bucket.

        Returns:
            str: Public URL of the destination object
        """
        self.s3_client.copy_object(
            Bucket=self.bucket_name,
            CopySource={"Bucket": self.bucket_name, "Key": source_key},
            Key=dest_key,
        )
        return self.get_public_url(dest_key)

    def generate_key_from_url(self, source_url) -> str:
        """
        Generate a unique key for storing a file based on its source URL

        Args:
            source_url (str): Original URL of the file
            prefix (str): Prefix for the key (folder structure)

        Returns:
            str: Generated key for the file
        """
        return str(hashlib.md5(source_url.encode()).hexdigest()[:20])
