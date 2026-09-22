import logging
import os
import tempfile
import time

import pyvips
import requests

from images.utils import R2Uploader

logger = logging.getLogger(__name__)

CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".json": "application/ld+json",
}


def _download_with_resume(
    source_url, dest_path, max_attempts=5, timeout=120, chunk_size=1024 * 1024
):
    """Download *source_url* to *dest_path*, resuming on transient failures.

    Retries broken streams (ChunkedEncodingError, ConnectionError, Timeout) up
    to ``max_attempts`` times. Each retry issues a ``Range`` request from the
    byte offset already on disk, so only the missing tail is re-fetched. Falls
    back to a full re-download if the server doesn't honor Range (200 OK
    instead of 206 Partial Content).
    """
    retryable = (
        requests.ConnectionError,
        requests.Timeout,
        requests.exceptions.ChunkedEncodingError,
    )
    for attempt in range(max_attempts):
        written = os.path.getsize(dest_path) if os.path.exists(dest_path) else 0
        headers = {"Range": f"bytes={written}-"} if written > 0 else {}
        try:
            resp = requests.get(
                source_url, timeout=timeout, stream=True, headers=headers
            )
            resp.raise_for_status()
            if headers and resp.status_code != 206:
                # Server ignored Range — restart from scratch.
                written = 0
                mode = "wb"
            else:
                mode = "ab" if written > 0 else "wb"
            with open(dest_path, mode) as f:
                for chunk in resp.iter_content(chunk_size=chunk_size):
                    f.write(chunk)
            return
        except retryable as e:
            if attempt + 1 >= max_attempts:
                raise
            delay = min(2**attempt, 30)
            logger.warning(
                "IIIF download failed at byte %d (attempt %d/%d, retrying in %ds): %s",
                written,
                attempt + 1,
                max_attempts,
                delay,
                e,
            )
            time.sleep(delay)


def generate_and_upload_iiif_tiles(source_url, r2_tiles_prefix):
    """Generate IIIF 3.0 tiles from an image URL and upload them to R2.

    Downloads the image from *source_url*, produces a full tile pyramid with
    pyvips ``dzsave(layout="iiif3")``, and uploads every generated file to R2
    under *r2_tiles_prefix* (e.g. ``"images/42/tiles"``).

    Returns ``(width, height)`` of the source image.
    """
    uploader = R2Uploader()

    # pyvips dzsave appends the output directory's basename to the ``id``
    # value written into info.json.  We want info.json to contain
    # ``{public_base}/{r2_tiles_prefix}`` so we pass the *parent* of the
    # prefix as ``id`` and name the output directory after the last path
    # component.
    r2_parent = "/".join(r2_tiles_prefix.split("/")[:-1])
    dir_basename = r2_tiles_prefix.split("/")[-1]
    iiif_id_url = uploader.get_public_url(r2_parent)

    with tempfile.TemporaryDirectory() as tmpdir:
        # Download the source image (resumes across transient network failures).
        source_path = os.path.join(tmpdir, "source")
        _download_with_resume(source_url, source_path)

        # Generate IIIF tiles
        tile_dir = os.path.join(tmpdir, dir_basename)
        vimg = pyvips.Image.new_from_file(source_path, access="sequential")

        vimg.dzsave(
            tile_dir,
            layout="iiif3",
            tile_size=512,
            overlap=0,
            suffix=".jpg[Q=85]",
            id=iiif_id_url,
        )

        # Upload the tile tree to R2
        for root, _dirs, files in os.walk(tile_dir):
            for fname in files:
                local_path = os.path.join(root, fname)
                rel_path = os.path.relpath(local_path, tile_dir)
                key = f"{r2_tiles_prefix}/{rel_path}"
                file_ext = os.path.splitext(fname)[1].lower()
                content_type = CONTENT_TYPES.get(file_ext, "application/octet-stream")

                with open(local_path, "rb") as f:
                    uploader.upload_file_content(
                        f.read(),
                        key,
                        content_type=content_type,
                        overwrite=True,
                    )

        width, height = vimg.width, vimg.height

        # When the whole image fits in one tile, dzsave names that tile by
        # its pixel size (full/499,401/...), but IIIF 3 clients such as
        # OpenSeadragon request the canonical size "max" for a full-region,
        # full-size tile. A level0 (static) service must answer canonical
        # URLs, so upload the same JPEG under both keys.
        single_tile = os.path.join(
            tile_dir, "full", f"{width},{height}", "0", "default.jpg"
        )
        if os.path.exists(single_tile):
            with open(single_tile, "rb") as f:
                uploader.upload_file_content(
                    f.read(),
                    f"{r2_tiles_prefix}/full/max/0/default.jpg",
                    content_type="image/jpeg",
                    overwrite=True,
                )

    logger.info(
        "IIIF tiles uploaded to %s (%dx%d)",
        r2_tiles_prefix,
        width,
        height,
    )
    return width, height
