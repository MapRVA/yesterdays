import json
import logging

from django.db import DatabaseError, connection
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods
from django_ratelimit.decorators import ratelimit
from psycopg import sql

from regions.context_processors import get_current_region

from ...models import Image

logger = logging.getLogger(__name__)

# Query security setting shared by the text-input search modes.
MAX_TEXT_QUERY_LENGTH = 500

# Try to import PostgreSQL search functions
try:
    import django.contrib.postgres.search

    HAS_POSTGRES_SEARCH = True
except ImportError:
    HAS_POSTGRES_SEARCH = False


@ratelimit(key="ip", rate="1000/h", method=["GET", "POST"])  # 16/min average
@ratelimit(key="ip", rate="100/5m", method=["GET", "POST"])  # 20/min burst
@require_http_methods(["GET", "POST"])
def text_search(request):
    """API endpoint for text search using PostgreSQL trigram word similarity.

    Supports format=html parameter to return rendered HTML cards instead of JSON.
    """
    if not HAS_POSTGRES_SEARCH:
        return JsonResponse(
            {
                "success": False,
                "error": "Text search not available. PostgreSQL search dependencies not installed.",
            },
            status=503,
        )

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
        distance_threshold = float(request.GET.get("threshold", 0.7))

        # Convert years here
        start_year_str = request.GET.get("start_year")
        end_year_str = request.GET.get("end_year")
        start_year = int(start_year_str) if start_year_str else None
        end_year = int(end_year_str) if end_year_str else None
    except ValueError:
        return JsonResponse(
            {"success": False, "error": "Invalid numeric parameters"}, status=400
        )

    offset = (page - 1) * limit
    georeferenced_only = (
        request.GET.get("georeferenced_only", "false").lower() == "true"
    )
    non_georeferenced_only = (
        request.GET.get("non_georeferenced_only", "false").lower() == "true"
    )

    # Subject filtering parameters
    with_subjects_str = request.GET.get("with_subjects")
    without_subjects_str = request.GET.get("without_subjects")
    no_subjects = request.GET.get("no_subjects", "false").lower() == "true"

    if not query and not any([with_subjects_str, without_subjects_str, no_subjects]):
        return JsonResponse(
            {
                "success": False,
                "error": "A search query or subject filter is required.",
            },
            status=400,
        )

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

    # --- Start of Query Logic ---
    try:
        # Build SQL WHERE conditions for all filters
        sql_where_conditions = [
            "i.is_searchable = true",
        ]
        sql_params = {
            "query": query,
            "threshold": distance_threshold,
            "limit": limit,
            "offset": offset,
        }

        # Georeferenced filtering (mirrors Image.is_georeferenced: aerial images
        # are judged by polygon georefs, others by point georefs)
        if georeferenced_only:
            sql_where_conditions.append(
                "((i.aerial = false AND EXISTS (SELECT 1 FROM images_georeference g WHERE g.image_id = i.id)) "
                "OR (i.aerial = true AND EXISTS (SELECT 1 FROM images_aerialgeoreference ag WHERE ag.image_id = i.id)))"
            )
        elif non_georeferenced_only:
            sql_where_conditions.append(
                "NOT ((i.aerial = false AND EXISTS (SELECT 1 FROM images_georeference g WHERE g.image_id = i.id)) "
                "OR (i.aerial = true AND EXISTS (SELECT 1 FROM images_aerialgeoreference ag WHERE ag.image_id = i.id)))"
            )

        # Year filtering
        if start_year:
            sql_where_conditions.append(
                "(i.start_decdate >= %(start_year)s OR i.fuzzy_start_decdate >= %(start_year)s)"
            )
            sql_params["start_year"] = start_year

        if end_year:
            sql_where_conditions.append(
                "(i.end_decdate <= %(end_year)s OR i.fuzzy_end_decdate <= %(end_year)s)"
            )
            sql_params["end_year"] = end_year

        # Subject filtering
        if no_subjects:
            sql_where_conditions.append(
                "NOT EXISTS (SELECT 1 FROM images_subjectmapping sm WHERE sm.image_id = i.id)"
            )
        else:
            if with_subject_ids:
                for idx, subject_id in enumerate(with_subject_ids):
                    param_name = f"with_subject_{idx}"
                    sql_where_conditions.append(
                        f"EXISTS (SELECT 1 FROM images_subjectmapping sm WHERE sm.image_id = i.id AND sm.subject_id = %({param_name})s)"
                    )
                    sql_params[param_name] = subject_id

            if without_subject_ids:
                sql_where_conditions.append(
                    "i.id NOT IN (SELECT image_id FROM images_subjectmapping WHERE subject_id = ANY(%(without_subject_ids)s))"
                )
                sql_params["without_subject_ids"] = without_subject_ids

        # Scope to the selected region (and the regions inside it), the same
        # resolution ImageQuerySet.in_region performs. where_clause feeds both
        # the count query and the page query, so both stay in sync.
        if region is not None:
            sql_where_conditions.append(
                Image.EFFECTIVE_REGION_SQL.format(alias="i.") + " = ANY(%(region_ids)s)"
            )
            sql_params["region_ids"] = region.self_and_descendant_ids()

        where_clause = " AND ".join(sql_where_conditions)

        # 2. Use Raw SQL for the complex trigram query for performance and control
        with connection.cursor() as cursor:
            # First, get total count of results that meet the threshold
            count_sql = sql.SQL("""
                SELECT COUNT(i.id)
                FROM images_image i
                LEFT JOIN LATERAL (
                    SELECT MIN(%(query)s <<-> c.text) as best_comment_distance
                    FROM images_comment c
                    WHERE c.image_id = i.id
                ) comment_match ON true
                LEFT JOIN LATERAL (
                    SELECT MIN(%(query)s <<-> g.confidence_notes) as best_geo_distance
                    FROM images_georeference g
                    WHERE g.image_id = i.id
                    AND g.confidence_notes != ''
                ) geo_match ON true
                LEFT JOIN LATERAL (
                    SELECT MIN(%(query)s <<-> ag.confidence_notes) as best_aerial_distance
                    FROM images_aerialgeoreference ag
                    WHERE ag.image_id = i.id
                    AND ag.confidence_notes != ''
                ) aerial_match ON true
                WHERE
                    {where_clause}
                    AND LEAST(
                        COALESCE(%(query)s <<-> i.title, 1.0),
                        COALESCE(%(query)s <<-> i.description, 1.0),
                        COALESCE(comment_match.best_comment_distance, 1.0),
                        COALESCE(geo_match.best_geo_distance, 1.0),
                        COALESCE(aerial_match.best_aerial_distance, 1.0)
                    ) < %(threshold)s
            """).format(where_clause=sql.SQL(where_clause))

            cursor.execute(count_sql, sql_params)
            total_count = cursor.fetchone()[0]

            # Now, get the paginated results
            page_query = sql.SQL("""
                SELECT
                    i.id, i.title, i.permalink, i.original_date, i.edtf_date,
                    LEAST(
                        COALESCE(%(query)s <<-> i.title, 1.0),
                        COALESCE(%(query)s <<-> i.description, 1.0),
                        COALESCE(comment_match.best_comment_distance, 1.0),
                        COALESCE(geo_match.best_geo_distance, 1.0),
                        COALESCE(aerial_match.best_aerial_distance, 1.0)
                    ) as distance
                FROM images_image i
                LEFT JOIN LATERAL (
                    SELECT MIN(%(query)s <<-> c.text) as best_comment_distance
                    FROM images_comment c
                    WHERE c.image_id = i.id
                ) comment_match ON true
                LEFT JOIN LATERAL (
                    SELECT MIN(%(query)s <<-> g.confidence_notes) as best_geo_distance
                    FROM images_georeference g
                    WHERE g.image_id = i.id
                    AND g.confidence_notes != ''
                ) geo_match ON true
                LEFT JOIN LATERAL (
                    SELECT MIN(%(query)s <<-> ag.confidence_notes) as best_aerial_distance
                    FROM images_aerialgeoreference ag
                    WHERE ag.image_id = i.id
                    AND ag.confidence_notes != ''
                ) aerial_match ON true
                WHERE
                    {where_clause}
                    AND LEAST(
                        COALESCE(%(query)s <<-> i.title, 1.0),
                        COALESCE(%(query)s <<-> i.description, 1.0),
                        COALESCE(comment_match.best_comment_distance, 1.0),
                        COALESCE(geo_match.best_geo_distance, 1.0),
                        COALESCE(aerial_match.best_aerial_distance, 1.0)
                    ) < %(threshold)s
                ORDER BY distance ASC, i.id ASC
                LIMIT %(limit)s OFFSET %(offset)s
            """).format(where_clause=sql.SQL(where_clause))
            cursor.execute(page_query, sql_params)
            rows = cursor.fetchall()

        # 3. Format the results
        search_results = []
        image_ids = [row[0] for row in rows]
        images_by_id = {
            img.id: img
            for img in Image.objects.filter(id__in=image_ids).select_related(
                "collection__source"
            )
        }

        for row in rows:
            # Unpack row data
            image_id, title, permalink, original_date, edtf_date, distance = row
            image = images_by_id.get(image_id)

            if not image:
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
                    "similarity": similarity,
                    "collection": {
                        "name": image.collection.name,
                        "slug": image.collection.slug,
                    },
                    "source": {
                        "name": image.collection.source.name,
                        "slug": image.collection.source.slug,
                    },
                    "detail_url": f"/{image.id}/",
                    "georeferenced": image.is_georeferenced,
                    "will_not_georef": image.will_not_georef,
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
                "search_type": "word_distance",
            }
        )

    except DatabaseError as e:
        logger.error(f"Database error in text search: {e}", exc_info=True)
        return JsonResponse(
            {"success": False, "error": "Search query failed. Please try again."},
            status=500,
        )
    except Exception:
        logger.exception("Unexpected error in text search")
        return JsonResponse(
            {"success": False, "error": "An unexpected error occurred."}, status=500
        )
