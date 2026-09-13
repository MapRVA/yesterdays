import logging
from io import BytesIO

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import DatabaseError, connection, transaction
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods
from django_ratelimit.decorators import ratelimit
from PIL import Image as PILImage
from psycopg import sql

from regions.context_processors import get_current_region

from ... import clip_client
from ...models import Image

logger = logging.getLogger(__name__)

# Image security settings.
MAX_IMAGE_PIXELS = 89_000_000  # ~89 megapixels
PILImage.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
MAX_DIMENSION = 10000
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP", "GIF"}


def _sanitize_image(uploaded_file, max_pixels=MAX_IMAGE_PIXELS):
    """Validate and re-encode an uploaded image as metadata-free PNG."""
    try:
        image = PILImage.open(uploaded_file)

        if image.format not in ALLOWED_FORMATS:
            raise ValueError(
                f"Image format '{image.format}' not supported. "
                f"Allowed formats: {', '.join(sorted(ALLOWED_FORMATS))}"
            )

        image.load()

        if image.width > MAX_DIMENSION or image.height > MAX_DIMENSION:
            raise ValueError(
                f"Image dimensions ({image.width}x{image.height}) exceed "
                f"maximum {MAX_DIMENSION}px per side"
            )

        total_pixels = image.width * image.height
        if total_pixels > max_pixels:
            raise ValueError(
                f"Image has too many pixels ({total_pixels:,}). Maximum: {max_pixels:,}"
            )

        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")

        output = BytesIO()
        image.save(output, format="PNG")
        output.seek(0)
        return output
    except ValueError:
        raise
    except PILImage.DecompressionBombError:
        logger.warning("Decompression bomb detected")
        raise ValueError("Image rejected: decompression bomb detected")
    except Exception as e:
        logger.warning("Image sanitization failed: %s: %s", type(e).__name__, e)
        raise ValueError("Invalid or corrupted image file")


def _get_image_embedding(sanitized_image):
    """Encode an already-sanitized PNG through the CLIP service."""
    return clip_client.get_image_embedding(sanitized_image.read())



@ratelimit(key="ip", rate="100/h", method=["POST"])  # Stricter for image processing
@ratelimit(key="ip", rate="20/5m", method=["POST"])  # Lower burst
@login_required
@require_http_methods(["POST"])
def reverse_image_search(request):
    """API endpoint for reverse image search using CLIP embeddings - requires authentication.

    Supports format=html parameter to return rendered HTML cards instead of JSON.
    """
    MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10MB

    # Check if HTML format is requested (check both GET and POST for multipart forms)
    return_html = (
        request.GET.get("format") == "html" or request.POST.get("format") == "html"
    )

    # Check if an image was uploaded
    if "image" not in request.FILES:
        return JsonResponse(
            {"success": False, "error": "No image file provided"}, status=400
        )

    uploaded_file = request.FILES["image"]

    # Check file size
    if uploaded_file.size > MAX_UPLOAD_SIZE:
        return JsonResponse({"success": False, "error": "File too large"}, status=400)

    # Validate file type
    if not uploaded_file.content_type.startswith("image/"):
        return JsonResponse(
            {"success": False, "error": "Uploaded file must be an image"}, status=400
        )

    # Get search and pagination parameters
    try:
        limit = min(int(request.POST.get("pagelimit", 20)), 100)
        page = max(int(request.POST.get("page", 1)), 1)
    except ValueError:
        return JsonResponse(
            {"success": False, "error": "Invalid pagination parameters"}, status=400
        )
    offset = (page - 1) * limit

    georeferenced_only = (
        request.POST.get("georeferenced_only", "false").lower() == "true"
    )
    non_georeferenced_only = (
        request.POST.get("non_georeferenced_only", "false").lower() == "true"
    )

    # Year filtering parameters
    start_year = request.POST.get("start_year")
    end_year = request.POST.get("end_year")

    # Validate year parameters
    if start_year:
        try:
            start_year = int(start_year)
        except ValueError:
            return JsonResponse(
                {
                    "success": False,
                    "error": "Invalid start_year parameter. Must be an integer.",
                },
                status=400,
            )

    if end_year:
        try:
            end_year = int(end_year)
        except ValueError:
            return JsonResponse(
                {
                    "success": False,
                    "error": "Invalid end_year parameter. Must be an integer.",
                },
                status=400,
            )

    # Subject filtering parameters
    with_subjects_str = request.POST.get("with_subjects")
    without_subjects_str = request.POST.get("without_subjects")
    no_subjects = request.POST.get("no_subjects", "false").lower() == "true"

    with_subject_ids = []
    if with_subjects_str:
        try:
            with_subject_ids = [
                int(s_id) for s_id in with_subjects_str.split(",") if s_id.strip()
            ]
        except ValueError:
            return JsonResponse(
                {
                    "success": False,
                    "error": "Invalid with_subjects parameter. Must be comma-separated integers.",
                },
                status=400,
            )

    without_subject_ids = []
    if without_subjects_str:
        try:
            without_subject_ids = [
                int(s_id) for s_id in without_subjects_str.split(",") if s_id.strip()
            ]
        except ValueError:
            return JsonResponse(
                {
                    "success": False,
                    "error": "Invalid without_subjects parameter. Must be comma-separated integers.",
                },
                status=400,
            )

    region = get_current_region(request)

    try:
        # Sanitize and validate the image (defense-in-depth security)
        try:
            sanitized_image = _sanitize_image(uploaded_file)
        except ValueError as e:
            # ValueError contains our custom error messages from sanitization
            return JsonResponse(
                {"success": False, "error": str(e)},
                status=400,
            )
        except Exception as e:
            logger.warning(f"Image sanitization error: {type(e).__name__}")
            return JsonResponse(
                {"success": False, "error": "Failed to process uploaded image"},
                status=400,
            )

        # Generate embedding from sanitized image
        try:
            query_embedding = _get_image_embedding(sanitized_image)
        except Exception as e:
            logger.warning(f"Image embedding generation failed: {type(e).__name__}")
            return JsonResponse(
                {"success": False, "error": "Could not generate image embedding"},
                status=400,
            )

        query_dimension = len(query_embedding)

        # Check dimension compatibility with database
        sample_embedding = None
        expected_dimension = None

        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT embedding
                FROM images_image
                WHERE embedding IS NOT NULL AND is_searchable = true
                LIMIT 1
            """)
            result = cursor.fetchone()
            if result:
                sample_embedding = result[0]
                expected_dimension = len(sample_embedding)

        # Check dimension compatibility
        if expected_dimension and query_dimension != expected_dimension:
            return JsonResponse(
                {
                    "success": False,
                    "error": f"Model dimension mismatch. Database contains {expected_dimension}D embeddings, but current model produces {query_dimension}D embeddings.",
                },
                status=400,
            )

        # Use raw SQL for vector similarity search
        with transaction.atomic(), connection.cursor() as cursor:
            # Raise pgvector's HNSW search depth from its default of 40.
            # Scoped to this transaction via SET LOCAL.
            cursor.execute("SET LOCAL hnsw.ef_search = %s", [settings.HNSW_EF_SEARCH])

            # Build dynamic WHERE conditions and separate parameters
            where_conditions = ["embedding IS NOT NULL"]
            where_params = []

            # Add georeferenced filter (mirrors Image.is_georeferenced: aerial
            # images are judged by polygon georefs, others by point georefs)
            if georeferenced_only:
                where_conditions.append(
                    "((aerial = false AND EXISTS (SELECT 1 FROM images_georeference g WHERE g.image_id = images_image.id)) "
                    "OR (aerial = true AND EXISTS (SELECT 1 FROM images_aerialgeoreference ag WHERE ag.image_id = images_image.id)))"
                )
            elif non_georeferenced_only:
                where_conditions.append(
                    "NOT ((aerial = false AND EXISTS (SELECT 1 FROM images_georeference g WHERE g.image_id = images_image.id)) "
                    "OR (aerial = true AND EXISTS (SELECT 1 FROM images_aerialgeoreference ag WHERE ag.image_id = images_image.id)))"
                )

            # Add year filtering conditions
            if start_year is not None:
                where_conditions.append(
                    "(start_decdate >= %s OR fuzzy_start_decdate >= %s)"
                )
                where_params.extend([start_year, start_year])

            if end_year is not None:
                where_conditions.append(
                    "(end_decdate <= %s OR fuzzy_end_decdate <= %s)"
                )
                where_params.extend([end_year, end_year])

            # Add subject filtering conditions
            if no_subjects:
                where_conditions.append(
                    "NOT EXISTS (SELECT 1 FROM images_subjectmapping sm WHERE sm.image_id = images_image.id)"
                )
            else:
                if with_subject_ids:
                    for subject_id in with_subject_ids:
                        where_conditions.append(
                            "EXISTS (SELECT 1 FROM images_subjectmapping sm WHERE sm.image_id = images_image.id AND sm.subject_id = %s)"
                        )
                        where_params.append(subject_id)

                if without_subject_ids:
                    where_conditions.append(
                        "images_image.id NOT IN (SELECT image_id FROM images_subjectmapping WHERE subject_id = ANY(%s))"
                    )
                    where_params.append(without_subject_ids)

            # Scope to the selected region (and the regions inside it), the
            # same resolution ImageQuerySet.in_region performs. Appended last
            # because where_params is positional: the page query splices it
            # between the two embedding parameters. Like every other filter
            # here this post-filters the HNSW candidate pool, so a small
            # region can return fewer than `limit` rows even when more
            # in-region matches exist.
            if region is not None:
                where_conditions.append(
                    Image.EFFECTIVE_REGION_SQL.format(alias="images_image.")
                    + " = ANY(%s)"
                )
                where_params.append(region.self_and_descendant_ids())

            # Combine all WHERE conditions
            where_clause = " AND ".join(where_conditions)

            # The HTML response doesn't show a total count for semantic or
            # reverse-image search (see search.js: stats line omits count when
            # mode is semantic/reverse). Skip the COUNT query for HTML and
            # detect has_more by fetching one extra row.
            skip_count = return_html
            fetch_limit = limit + 1 if skip_count else limit

            if not skip_count:
                count_sql = sql.SQL("""
                    SELECT COUNT(id)
                    FROM images_image
                    WHERE {where_clause}
                    AND is_searchable = true
                """).format(where_clause=sql.SQL(where_clause))
                cursor.execute(count_sql, where_params)
                total_count = cursor.fetchone()[0]
                # HNSW can only rank ef_search candidates per query, so deeper
                # results aren't reachable even if more matching rows exist.
                total_count = min(total_count, settings.HNSW_EF_SEARCH)
            else:
                total_count = None

            # Raw SQL query for cosine similarity
            query_sql = sql.SQL("""
                SELECT
                    id,
                    title,
                    permalink,
                    original_date,
                    edtf_date,
                    start_decdate,
                    end_decdate,
                    (embedding::vector(768) <=> %s::vector(768)) as distance
                FROM images_image
                WHERE {where_clause}
                AND is_searchable = true
                ORDER BY embedding::vector(768) <=> %s::vector(768), id ASC
                LIMIT %s
                OFFSET %s
            """).format(where_clause=sql.SQL(where_clause))

            # Pass embedding as parameter - pgvector accepts array format
            query_params = (
                [query_embedding]
                + where_params
                + [query_embedding, fetch_limit, offset]
            )

            cursor.execute(query_sql, query_params)
            results = cursor.fetchall()

            if skip_count:
                has_more_from_fetch = len(results) > limit
                results = results[:limit]

        # Format results - Fetch all images in one query to avoid N+1 problem
        search_results = []
        image_ids = [row[0] for row in results]
        images_dict = {
            img.id: img
            for img in Image.objects.select_related("collection__source").filter(
                id__in=image_ids
            )
        }

        for row in results:
            (
                image_id,
                title,
                permalink,
                original_date,
                edtf_date,
                start_decdate,
                end_decdate,
                distance,
            ) = row

            # Get the full image object for additional data
            image = images_dict.get(image_id)
            if not image:
                # Skip if image was deleted between query and retrieval
                continue

            similarity = 1.0 - float(distance)
            similarity_score = round(similarity * 100)

            if return_html:
                # For HTML format, store image object and similarity score
                search_results.append(
                    {
                        "image": image,
                        "similarity_score": similarity_score,
                    }
                )
            else:
                # For JSON format, build full result dict
                result = {
                    "id": image_id,
                    "title": title,
                    "permalink": image.display_permalink,
                    "thumbnail": image.thumbnail
                    if image.thumbnail
                    else image.display_permalink,
                    "original_date": str(original_date) if original_date else None,
                    "edtf_date": str(edtf_date) if edtf_date else None,
                    "distance": float(distance),
                    "similarity": similarity,
                    "collection": {
                        "name": image.collection.name,
                        "slug": image.collection.slug,
                    },
                    "source": {
                        "name": image.collection.source.name,
                        "slug": image.collection.source.slug,
                    },
                    "detail_url": f"/{image_id}/",
                    "georeferenced": image.is_georeferenced,
                    "will_not_georef": image.will_not_georef,
                }

                # Add georeference data if available
                if image.is_georeferenced:
                    georeference = image.get_georeference()
                    if georeference:
                        result["georeference"] = {
                            "latitude": georeference.point.y,
                            "longitude": georeference.point.x,
                            "direction": georeference.direction,
                            "confidence": georeference.confidence,
                        }

                search_results.append(result)

        # Calculate if there are more results
        if total_count is None:
            # COUNT was skipped; we fetched limit+1 to detect more rows
            has_more = has_more_from_fetch
        else:
            has_more = (page * limit) < total_count

        if return_html:
            # Return rendered HTML partial
            return render(
                request,
                "images/partials/search_results_items.html",
                {
                    "results": search_results,
                    "has_more": has_more,
                    "total_count": total_count,
                    "page": page,
                },
            )

        return JsonResponse(
            {
                "success": True,
                "query": "reverse_image_search",
                "results": search_results,
                "count": total_count,
                "page": page,
                "limit": limit,
                "search_type": "reverse_image",
            }
        )

    except DatabaseError as e:
        logger.error(f"Database error in reverse image search: {e}", exc_info=True)
        return JsonResponse(
            {"success": False, "error": "Database query failed. Please try again."},
            status=500,
        )
    except Exception:
        logger.exception("Unexpected error in reverse image search")
        return JsonResponse(
            {"success": False, "error": "An unexpected error occurred."},
            status=500,
        )
