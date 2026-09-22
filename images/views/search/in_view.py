import logging

from django.db import DatabaseError
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods
from django_ratelimit.decorators import ratelimit
from psycopg import sql

from ...in_view import format_distance, in_view_rows, parse_radius
from ...models import Image
from ...validation import InvalidInput, validate_latitude, validate_longitude

logger = logging.getLogger(__name__)


def _in_view_filters(request):
    """Parse the page's advanced filters into named-parameter SQL fragments.

    Mirrors the year and subject filters the other search views accept, in the
    shape ``images.in_view`` wants: fragments over the image table aliased
    ``i``, with their values in a dict. Raises ``ValueError`` on bad input, the
    way ``int()`` does, so the caller has a single place to turn it into a 400.
    """
    conditions = []
    params = {}

    # Year bounds use the same expressions as text_search, so switching modes
    # on the search page doesn't silently change what a year range means.
    start_year = request.GET.get("start_year")
    if start_year:
        params["start_year"] = int(start_year)
        conditions.append(
            sql.SQL(
                "(i.start_decdate >= %(start_year)s"
                " OR i.fuzzy_start_decdate >= %(start_year)s)"
            )
        )

    end_year = request.GET.get("end_year")
    if end_year:
        params["end_year"] = int(end_year)
        conditions.append(
            sql.SQL(
                "(i.end_decdate <= %(end_year)s OR i.fuzzy_end_decdate <= %(end_year)s)"
            )
        )

    if request.GET.get("no_subjects", "false").lower() == "true":
        conditions.append(
            sql.SQL(
                "NOT EXISTS (SELECT 1 FROM images_subjectmapping sm"
                " WHERE sm.image_id = i.id)"
            )
        )
        return conditions, params

    with_subjects = request.GET.get("with_subjects")
    if with_subjects:
        ids = [int(s) for s in with_subjects.split(",") if s.strip()]
        if ids:
            # ALL of them: count the distinct matches rather than emitting one
            # EXISTS per subject, so the fragment stays a single fixed string.
            params["with_subjects"] = ids
            params["with_subjects_count"] = len(set(ids))
            conditions.append(
                sql.SQL(
                    "(SELECT COUNT(DISTINCT sm.subject_id)"
                    " FROM images_subjectmapping sm"
                    " WHERE sm.image_id = i.id"
                    " AND sm.subject_id = ANY(%(with_subjects)s))"
                    " = %(with_subjects_count)s"
                )
            )

    without_subjects = request.GET.get("without_subjects")
    if without_subjects:
        ids = [int(s) for s in without_subjects.split(",") if s.strip()]
        if ids:
            params["without_subjects"] = ids
            conditions.append(
                sql.SQL(
                    "NOT EXISTS (SELECT 1 FROM images_subjectmapping sm"
                    " WHERE sm.image_id = i.id"
                    " AND sm.subject_id = ANY(%(without_subjects)s))"
                )
            )

    return conditions, params


@ratelimit(key="ip", rate="1000/h", method=["GET"])  # 16/min average
@ratelimit(key="ip", rate="100/5m", method=["GET"])  # 20/min burst
@require_http_methods(["GET"])
def in_view_search(request):
    """API endpoint for in-view search: images that have a point in view.

    Supports format=html parameter to return rendered HTML cards instead of
    JSON, like its sibling search endpoints.

    "Near" is necessary but not sufficient — see ``images.in_view`` for the
    directional sweep that decides whether a photograph actually faces the
    coordinate.

    Unlike every other search view here, results are deliberately NOT scoped
    to the navbar region, following the precedent set by the from-above point
    filter: a coordinate is its own scope, and a photograph taken across a
    region boundary from the clicked spot is still a photograph of that spot.
    """
    return_html = request.GET.get("format") == "html"

    try:
        latitude = validate_latitude(request.GET, "lat")
        longitude = validate_longitude(request.GET, "lon")
        radius_m = parse_radius(request.GET, "radius")
    except InvalidInput as e:
        return JsonResponse({"success": False, "error": str(e)}, status=400)

    try:
        limit = min(max(int(request.GET.get("pagelimit", 20)), 1), 100)
        page = max(int(request.GET.get("page", 1)), 1)
        extra_conditions, extra_params = _in_view_filters(request)
    except ValueError:
        return JsonResponse(
            {"success": False, "error": "Invalid numeric parameters"}, status=400
        )
    offset = (page - 1) * limit

    # Every result here is georeferenced by construction, so the page's
    # "not georeferenced only" option can only ever match nothing.
    if request.GET.get("non_georeferenced_only", "false").lower() == "true":
        rows, has_more = [], False
    else:
        try:
            rows, has_more = in_view_rows(
                latitude=latitude,
                longitude=longitude,
                limit=limit,
                offset=offset,
                radius_m=radius_m,
                extra_conditions=extra_conditions,
                extra_params=extra_params,
            )
        except InvalidInput as e:
            return JsonResponse({"success": False, "error": str(e)}, status=400)
        except DatabaseError as e:
            logger.error("Database error in in-view search: %s", e, exc_info=True)
            return JsonResponse(
                {"success": False, "error": "Database query failed. Please try again."},
                status=500,
            )

    images_by_id = {
        image.id: image
        for image in Image.objects.select_related("collection__source").filter(
            id__in=[row["image_id"] for row in rows]
        )
    }

    search_results = []
    # Send the same geometry as the cards so the picker map cannot drift from
    # the result page it is displaying.
    result_points = []
    for row in rows:
        image = images_by_id.get(row["image_id"])
        if not image:
            # Skip if the image was deleted between query and retrieval.
            continue

        result_points.append(
            {
                "id": image.id,
                "lat": row["latitude"],
                "lng": row["longitude"],
                "direction": row["direction"],
            }
        )

        if return_html:
            search_results.append(
                {
                    "image": image,
                    "distance_label": format_distance(row["distance_m"]),
                    # The card uses an explicit None to distinguish a distance
                    # badge from its similarity badge.
                    "similarity_score": None,
                }
            )
        else:
            search_results.append(
                {
                    "id": image.id,
                    "title": image.title,
                    "permalink": image.display_permalink,
                    "thumbnail": image.thumbnail or image.display_permalink,
                    "original_date": str(image.original_date)
                    if image.original_date
                    else None,
                    "date_display": image.date_display,
                    "distance_m": round(row["distance_m"], 1),
                    "collection": {
                        "name": image.collection.name,
                        "slug": image.collection.slug,
                    },
                    "source": {
                        "name": image.collection.source.name,
                        "slug": image.collection.source.slug,
                    },
                    "detail_url": f"/{image.id}/",
                    "georeferenced": True,
                    "georeference": {
                        "latitude": row["latitude"],
                        "longitude": row["longitude"],
                        "direction": row["direction"],
                        "confidence": row["confidence"],
                    },
                }
            )

    if return_html:
        return render(
            request,
            "images/partials/search_results_items.html",
            {
                "results": search_results,
                "result_points": result_points,
                "has_more": has_more,
                # No total: without a radius every georeferenced image is a
                # result, so a count would be a misleading way of saying "all".
                "total_count": None,
                "page": page,
            },
        )

    return JsonResponse(
        {
            "success": True,
            "latitude": latitude,
            "longitude": longitude,
            "radius": radius_m,
            "results": search_results,
            "has_more": has_more,
            "page": page,
            "limit": limit,
        }
    )
