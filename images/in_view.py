"""
In-view queries: which photographs have a given coordinate in view.

The site can already answer "what does this look like?"; this module answers
the inverse — "what did this spot look like?" — by walking the GiST index on
``images_georeference.point`` outward from a coordinate.

* PostGIS's KNN operator ``<->`` in ``ORDER BY ... LIMIT n`` makes the planner
  do an index-ordered scan: it walks the index outward from the origin,
  applies the ``WHERE`` filters row by row, and stops as soon as it has ``n``
  rows. That is "scan until we have found enough results", so no radius is
  needed to bound the work. A radius is offered anyway, as an extra bound and
  because it is the only case where a total count means anything.
* The distance is measured against the ``(point::geography)`` expression index
  created in migration 0066, so ``<->`` returns true metres rather than
  degrees. Ordering by degrees on a 4326 geometry is latitude-distorted: at
  Richmond's ~37.5°N a degree of longitude is only 0.79x a degree of latitude,
  so a point 1 km east would sort ahead of one 1 km north.

Only the latest georeference of each image counts, matching
``Image.get_georeference()`` and the map tiles — a location that has since
been corrected must not resurface. That restriction is expressed as a
``NOT EXISTS`` filter rather than a ``DISTINCT ON``, so the query emits at
most one row per image: deduplication is free, and ``LIMIT limit + 1`` is
exactly enough rows to fill a page and know whether another one follows.

The searched coordinate has to additionally fall inside a cone of
``IN_VIEW_SEARCH_DIRECTION_SWEEP_DEGREES`` centered on the georeference's
``direction``. Georeferences with no recorded direction are kept.

Polygonal georeferences are excluded for now.
"""

import math

from django.conf import settings
from django.db import connection
from psycopg import sql

from .validation import InvalidInput

__all__ = ["in_view_rows", "in_view_count", "parse_radius", "format_distance"]


# The origin coordinate. Always bound as parameters, never interpolated.
_ORIGIN = sql.SQL("ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography")

# Great-circle distance in metres from the origin to a georeference. Written
# out again in ORDER BY rather than referenced by its output alias: the
# planner only recognises the KNN form when the operator itself appears there.
_DISTANCE = sql.SQL("g.point::geography <-> {origin}").format(origin=_ORIGIN)

# Keep only each image's most recent georeference. Comparing the row
# ``(georeferenced_at, id)`` rather than the timestamp alone makes "latest" a
# total order, so two georeferences submitted in the same instant cannot both
# survive and emit the image twice. This is the same intent as the
# ``DISTINCT ON (image_id) ORDER BY image_id, -georeferenced_at`` idiom used
# by the GeoJSON viewsets and the validation queue.
_LATEST_PER_IMAGE = sql.SQL(
    "NOT EXISTS ("
    "SELECT 1 FROM images_georeference g2"
    " WHERE g2.image_id = g.image_id"
    " AND (g2.georeferenced_at, g2.id) > (g.georeferenced_at, g.id))"
)

# Does the camera point at the origin? ST_Azimuth gives the true geodetic
# bearing from the georeference back to the searched coordinate, in radians
# clockwise from north; the +540/mod 360/-180 dance folds the difference
# against `direction` into [-180, 180] so that, say, 5 degrees and 355 degrees
# read as 10 degrees apart rather than 350.
#
# Two kinds of row bypass the test rather than failing it: a georeference with
# no recorded direction (most of the collection), and one sitting exactly on
# the searched coordinate, where ST_Azimuth is undefined and returns NULL.
_FACING_ORIGIN = sql.SQL(
    "(g.direction IS NULL"
    " OR ST_Azimuth(g.point::geography, {origin}) IS NULL"
    " OR abs(mod((degrees(ST_Azimuth(g.point::geography, {origin}))"
    " - g.direction + 540)::numeric, 360) - 180)"
    " <= %(direction_half_sweep)s)"
).format(origin=_ORIGIN)

_WITHIN_RADIUS = sql.SQL("ST_DWithin(g.point::geography, {origin}, %(radius)s)").format(
    origin=_ORIGIN
)

_FROM = sql.SQL(
    "FROM images_georeference g JOIN images_image i ON i.id = g.image_id WHERE {where}"
)


def _where(radius_m, extra_conditions):
    """Build the shared WHERE clause. ``extra_conditions`` alias images as ``i``."""
    conditions = [
        sql.SQL("i.is_searchable"),
        sql.SQL("NOT i.aerial"),
        _LATEST_PER_IMAGE,
        _FACING_ORIGIN,
    ]
    if radius_m is not None:
        conditions.append(_WITHIN_RADIUS)
    conditions.extend(extra_conditions)
    return sql.SQL(" AND ").join(conditions)


def _params(latitude, longitude, radius_m, extra_params):
    params = dict(extra_params or {})
    params["lat"] = latitude
    params["lon"] = longitude
    # Half either side of the recorded direction, so the setting reads as the
    # total width of the cone.
    params["direction_half_sweep"] = settings.IN_VIEW_SEARCH_DIRECTION_SWEEP_DEGREES / 2
    if radius_m is not None:
        params["radius"] = radius_m
    return params


def parse_radius(data, key="radius"):
    """Read an optional search radius in metres.

    Missing and empty mean "no radius", which is the normal case: the KNN scan
    does not need one. A radius larger than ``IN_VIEW_SEARCH_MAX_RADIUS_M`` is
    clamped rather than rejected, so callers should echo the returned value
    back rather than the one they were given.
    """
    if key not in data:
        return None

    value = data[key]
    if value is None or value == "":
        return None

    try:
        radius = float(value)
    except TypeError, ValueError:
        raise InvalidInput(f"'{key}' must be a number of metres")

    if not math.isfinite(radius) or radius <= 0:
        raise InvalidInput(f"'{key}' must be a positive number of metres")

    return min(radius, settings.IN_VIEW_SEARCH_MAX_RADIUS_M)


def in_view_rows(
    *,
    latitude,
    longitude,
    limit,
    offset=0,
    radius_m=None,
    extra_conditions=(),
    extra_params=None,
):
    """Latest-per-image point georeferences nearest to a coordinate.

    Returns ``(rows, has_more)``. Each row is one image that looks *at* the
    coordinate, at the location of its most recent georeference, ordered by
    true great-circle distance.
    ``has_more`` comes from fetching one row past the page rather than from a
    ``COUNT``, the same trick the semantic search HTML path uses.

    ``extra_conditions`` are ``psycopg.sql`` fragments filtering the image
    table under the alias ``i``, with their values in ``extra_params``.

    Raises ``InvalidInput`` if the requested window reaches past
    ``IN_VIEW_SEARCH_MAX_RESULTS``. That ceiling is the safety bound standing
    in for a mandatory radius: without it, a coordinate in the middle of the
    ocean plus a deep page would walk a large slice of the index.
    """
    max_results = settings.IN_VIEW_SEARCH_MAX_RESULTS
    if offset + limit > max_results:
        raise InvalidInput(
            f"In-view search returns at most {max_results} results; "
            "narrow the search instead of paging further."
        )

    query = sql.SQL(
        "SELECT g.image_id,"
        " g.id AS georeference_id,"
        " g.direction,"
        " g.confidence,"
        " ST_Y(g.point) AS latitude,"
        " ST_X(g.point) AS longitude,"
        " {distance} AS distance_m"
        " {from_clause}"
        " ORDER BY {distance}, g.image_id"
        " LIMIT %(fetch_limit)s OFFSET %(offset)s"
    ).format(
        distance=_DISTANCE,
        from_clause=_FROM.format(where=_where(radius_m, extra_conditions)),
    )

    params = _params(latitude, longitude, radius_m, extra_params)
    params["fetch_limit"] = limit + 1
    params["offset"] = offset

    with connection.cursor() as cursor:
        cursor.execute(query, params)
        fetched = cursor.fetchall()

    has_more = len(fetched) > limit
    rows = [
        {
            "image_id": row[0],
            "georeference_id": row[1],
            "direction": row[2],
            "confidence": row[3],
            "latitude": row[4],
            "longitude": row[5],
            "distance_m": row[6],
        }
        for row in fetched[:limit]
    ]
    return rows, has_more


def in_view_count(
    *,
    latitude,
    longitude,
    radius_m,
    extra_conditions=(),
    extra_params=None,
):
    """Number of images pointing at a coordinate from within ``radius_m`` of it.

    Only meaningful with a radius, which is why one is required here: without
    a bound, *every* georeferenced image is technically a result and a total
    would be a misleading way of saying "all of them". Bounded, the count uses
    the same ``ST_DWithin`` predicate and the same index as the page query.
    """
    query = sql.SQL("SELECT COUNT(*) {from_clause}").format(
        from_clause=_FROM.format(where=_where(radius_m, extra_conditions)),
    )

    with connection.cursor() as cursor:
        cursor.execute(query, _params(latitude, longitude, radius_m, extra_params))
        return cursor.fetchone()[0]


def format_distance(metres):
    """Render a distance for display: "42 m" up close, "1.2 km" further out."""
    if metres is None:
        return ""
    if metres < 1000:
        return f"{round(metres)} m"
    return f"{metres / 1000:.1f} km"
