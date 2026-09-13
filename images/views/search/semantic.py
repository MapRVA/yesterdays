import json
import logging

from django.conf import settings
from django.contrib import messages
from django.db import DatabaseError, connection, transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods
from django_ratelimit.decorators import ratelimit
from psycopg import sql

from regions.context_processors import get_current_region

from ... import clip_client
from ...models import Image

logger = logging.getLogger(__name__)

# Fixed at the model + index level (ViT-L/14@336px → 768D, see migration 0036)
CLIP_EMBEDDING_DIMENSION = 768

# Query security setting shared by the text-input search modes.
MAX_TEXT_QUERY_LENGTH = 500


def _get_text_embedding(text):
    """Encode text into a CLIP embedding via the CLIP service."""
    return clip_client.get_text_embedding(text)


@ratelimit(key="ip", rate="1000/h", method=["GET", "POST"])  # 16/min average
@ratelimit(key="ip", rate="100/5m", method=["GET", "POST"])  # 20/min burst
@require_http_methods(["GET", "POST"])
def semantic_search(request):
    """API endpoint for semantic search using CLIP embeddings.

    Supports format=html parameter to return rendered HTML cards instead of JSON.
    """
    # Check if HTML format is requested
    return_html = request.GET.get("format") == "html"

    # Get search query
    if request.method == "POST":
        try:
            data = json.loads(request.body)
            query = data.get("query", "").strip()
        except json.JSONDecodeError:
            return JsonResponse(
                {"success": False, "error": "Invalid JSON in request body"}, status=400
            )
    else:  # GET request
        query = request.GET.get("q", "").strip()

    if not query:
        return JsonResponse(
            {
                "success": False,
                "error": "Query parameter 'q' (GET) or 'query' (POST) is required",
            },
            status=400,
        )

    # Validate query length
    if len(query) > MAX_TEXT_QUERY_LENGTH:
        return JsonResponse(
            {
                "success": False,
                "error": f"Query too long. Maximum {MAX_TEXT_QUERY_LENGTH} characters.",
            },
            status=400,
        )

    # Get search and pagination parameters
    try:
        limit = min(int(request.GET.get("pagelimit", 20)), 100)
        page = max(int(request.GET.get("page", 1)), 1)
    except ValueError:
        return JsonResponse(
            {"success": False, "error": "Invalid pagination parameters"}, status=400
        )
    offset = (page - 1) * limit

    include_no_embedding = (
        request.GET.get("include_no_embedding", "false").lower() == "true"
    )
    georeferenced_only = (
        request.GET.get("georeferenced_only", "false").lower() == "true"
    )
    non_georeferenced_only = (
        request.GET.get("non_georeferenced_only", "false").lower() == "true"
    )

    # Year filtering parameters
    start_year = request.GET.get("start_year")
    end_year = request.GET.get("end_year")

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
    with_subjects_str = request.GET.get("with_subjects")
    without_subjects_str = request.GET.get("without_subjects")
    no_subjects = request.GET.get("no_subjects", "false").lower() == "true"

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
        # Generate query embedding
        try:
            query_embedding = _get_text_embedding(query)
        except Exception as e:
            logger.warning(f"Text embedding generation failed: {type(e).__name__}")
            return JsonResponse(
                {"success": False, "error": "Could not process search query"},
                status=400,
            )

        if len(query_embedding) != CLIP_EMBEDDING_DIMENSION:
            return JsonResponse(
                {
                    "success": False,
                    "error": f"Model dimension mismatch. Database contains {CLIP_EMBEDDING_DIMENSION}D embeddings, but current model produces {len(query_embedding)}D embeddings. Please regenerate embeddings with the current model.",
                },
                status=400,
            )

        # Use raw SQL for vector similarity search
        # Note: This requires pgvector extension to be installed

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
                "query": query,
                "results": search_results,
                "count": total_count,
                "page": page,
                "limit": limit,
            }
        )

    except DatabaseError as e:
        logger.error(f"Database error in semantic search: {e}", exc_info=True)
        return JsonResponse(
            {"success": False, "error": "Database query failed. Please try again."},
            status=500,
        )
    except Exception:
        logger.exception("Unexpected error in semantic search")
        return JsonResponse(
            {"success": False, "error": "An unexpected error occurred."},
            status=500,
        )







@ratelimit(key="ip", rate="1000/h", method=["GET", "POST"])  # 16/min average
@ratelimit(key="ip", rate="100/5m", method=["GET", "POST"])  # 20/min burst
def find_similar_images(request, image_id):
    """
    Find and display images with embeddings most similar to a given image.

    Supports filtering via query parameters from filter_cards.html:
    - georeference_status: comma-separated values (georeferenced, pending, will_not_georef)
    - start_year, end_year: year range filtering
    - with_subjects: comma-separated subject IDs (images must have ALL)
    - without_subjects: comma-separated subject IDs (images must not have ANY)
    - no_subjects: if 'true', only images with no subjects

    For AJAX requests (X-Requested-With: XMLHttpRequest), returns just the image
    cards HTML partial for "Load More" functionality.
    """
    # Get the target image and its embedding
    target_image = get_object_or_404(Image, id=image_id)
    if not target_image.embedding:
        messages.error(
            request,
            "The selected image does not have an embedding, so similar images cannot be found.",
        )
        return redirect("images:image_detail", image_id=image_id)

    # Check if this is an AJAX request for "Load More"
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    # Get filter parameters from URL (matching filter_cards.html)
    georeference_status = (
        request.GET.get("georeference_status", "").split(",")
        if request.GET.get("georeference_status")
        else []
    )
    start_year = request.GET.get("start_year")
    end_year = request.GET.get("end_year")
    with_subjects = (
        request.GET.get("with_subjects", "").split(",")
        if request.GET.get("with_subjects")
        else []
    )
    without_subjects = (
        request.GET.get("without_subjects", "").split(",")
        if request.GET.get("without_subjects")
        else []
    )
    no_subjects = request.GET.get("no_subjects") == "true"

    # Pagination parameters - use offset-based pagination for "Load More"
    per_page = 24
    try:
        offset = int(request.GET.get("offset", 0))
        if offset < 0:
            offset = 0
    except (ValueError, TypeError):
        offset = 0

    try:
        with transaction.atomic(), connection.cursor() as cursor:
            # Raise pgvector's HNSW search depth from its default of 40.
            # Scoped to this transaction via SET LOCAL.
            cursor.execute("SET LOCAL hnsw.ef_search = %s", [settings.HNSW_EF_SEARCH])

            # Build WHERE conditions
            where_conditions = ["embedding IS NOT NULL", "id != %s"]
            where_params = [target_image.id]

            # Georeference status filtering (matching filter_cards.html behavior)
            if georeference_status:
                status_conditions = []
                if "georeferenced" in georeference_status:
                    status_conditions.append(
                        "(EXISTS (SELECT 1 FROM images_georeference g WHERE g.image_id = images_image.id) "
                        "OR (aerial = true AND EXISTS (SELECT 1 FROM images_aerialgeoreference ag WHERE ag.image_id = images_image.id)))"
                    )
                if "pending" in georeference_status:
                    status_conditions.append(
                        "((NOT EXISTS (SELECT 1 FROM images_georeference g WHERE g.image_id = images_image.id) "
                        "AND aerial = false AND will_not_georef = false) "
                        "OR (NOT EXISTS (SELECT 1 FROM images_aerialgeoreference ag WHERE ag.image_id = images_image.id) "
                        "AND aerial = true AND will_not_georef = false))"
                    )
                if "will_not_georef" in georeference_status:
                    status_conditions.append("will_not_georef = true")

                if status_conditions:
                    where_conditions.append(f"({' OR '.join(status_conditions)})")

            # Year range conditions
            if start_year:
                try:
                    start_year_int = int(start_year)
                    where_conditions.append(
                        "(fuzzy_start_decdate >= %s OR start_decdate >= %s)"
                    )
                    where_params.extend([start_year_int, start_year_int])
                except ValueError:
                    pass
            if end_year:
                try:
                    end_year_int = int(end_year)
                    where_conditions.append(
                        "(fuzzy_end_decdate <= %s OR end_decdate <= %s)"
                    )
                    where_params.extend([end_year_int, end_year_int])
                except ValueError:
                    pass

            # Subject filtering
            if no_subjects:
                where_conditions.append(
                    "NOT EXISTS (SELECT 1 FROM images_subjectmapping sm WHERE sm.image_id = images_image.id)"
                )
            elif with_subjects:
                # Images must have ALL specified subjects
                for subject_id in with_subjects:
                    if subject_id:
                        where_conditions.append(
                            "EXISTS (SELECT 1 FROM images_subjectmapping sm WHERE sm.image_id = images_image.id AND sm.subject_id = %s)"
                        )
                        where_params.append(subject_id)
            elif without_subjects:
                # Images must not have ANY of the specified subjects
                valid_ids = [sid for sid in without_subjects if sid]
                if valid_ids:
                    placeholders = ", ".join(["%s"] * len(valid_ids))
                    where_conditions.append(
                        f"NOT EXISTS (SELECT 1 FROM images_subjectmapping sm WHERE sm.image_id = images_image.id AND sm.subject_id IN ({placeholders}))"
                    )
                    where_params.extend(valid_ids)

            where_clause = " AND ".join(where_conditions)

            # Get total count for pagination
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

            # SQL-level pagination - only fetch the IDs we need for this page
            query_sql = sql.SQL("""
                SELECT
                    id,
                    (embedding::vector(768) <=> %s::vector(768)) as distance
                FROM images_image
                WHERE {where_clause}
                AND is_searchable = true
                ORDER BY embedding::vector(768) <=> %s::vector(768), id ASC
                LIMIT %s OFFSET %s
            """).format(where_clause=sql.SQL(where_clause))
            cursor.execute(
                query_sql,
                [target_image.embedding]
                + where_params
                + [target_image.embedding, per_page, offset],
            )
            page_results = cursor.fetchall()

        # Get the IDs for this page only
        current_page_ids = [row[0] for row in page_results]

        # Get the full Image objects for the current page
        images_on_page = Image.objects.filter(id__in=current_page_ids).select_related(
            "collection__source"
        )

        # Create a dictionary to map IDs to image objects for correct ordering
        images_by_id = {img.id: img for img in images_on_page}

        # Re-order the fetched image objects based on the result order
        ordered_images = [
            images_by_id[img_id]
            for img_id in current_page_ids
            if img_id in images_by_id
        ]

        # Calculate if there are more images to load
        next_offset = offset + per_page
        has_more = next_offset < total_count

        # For AJAX requests, return just the image cards partial
        if is_ajax:
            return render(
                request,
                "images/partials/similar_images_items.html",
                {
                    "images": ordered_images,
                    "has_more": has_more,
                    "georeference_url": "/georeference/",
                    "show_collection_link": True,
                    "badges": True,
                    "buttons": True,
                },
            )

        # For regular requests, return the full page
        context = {
            "target_image": target_image,
            "images": ordered_images,
            "total_similar_count": total_count,
            "has_more": has_more,
            "per_page": per_page,
        }

        return render(request, "images/similar_images.html", context)

    except Exception as e:
        messages.error(
            request, f"An error occurred while finding similar images: {str(e)}"
        )
        return redirect("images:image_detail", image_id=image_id)
