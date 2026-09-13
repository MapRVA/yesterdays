"""Request parsing and input limits shared by the community write endpoints.

Every helper raises :class:`InvalidInput` on bad input. Views catch that once
and turn it into a generic ``400`` — the message is safe to show a client
because it only ever describes the caller's own input, never our internals.

Two Python-specific hazards are handled here rather than being left to the
model layer:

* ``json.loads`` accepts ``NaN``, ``Infinity`` and ``-Infinity``, which are not
  valid JSON. A ``NaN`` latitude reaches PostGIS happily and poisons every
  spatial query that later touches the row, so the parser rejects them.
* ``Model.objects.create()`` does not run field validators. ``direction``'s
  ``MinValueValidator``/``MaxValueValidator`` therefore never fire on the API
  path, so the bounds are checked explicitly.
"""

import json
import math

from django.conf import settings
from django.contrib.gis.geos import GEOSGeometry, Polygon
from django.contrib.gis.geos.error import GEOSException

__all__ = [
    "InvalidInput",
    "parse_json_body",
    "validate_text",
    "validate_choice",
    "validate_latitude",
    "validate_longitude",
    "validate_direction",
    "validate_polygon_geojson",
    "build_polygon",
    "WGS84_SRID",
]


def _reject_json_constant(constant):
    """Refuse the NaN/Infinity literals Python's JSON parser accepts.

    They are not valid JSON, and a non-finite coordinate that reaches PostGIS
    poisons every spatial query that later touches the row.
    """
    raise ValueError(f"invalid JSON constant: {constant}")


# GeoJSON positions are [longitude, latitude], optionally with elevation.
LONGITUDE_MIN, LONGITUDE_MAX = -180.0, 180.0
LATITUDE_MIN, LATITUDE_MAX = -90.0, 90.0

# Everything we store is WGS84. Stated explicitly so a client cannot smuggle in
# geometry expressed in another coordinate system.
WGS84_SRID = 4326


class InvalidInput(Exception):
    """Client input that must produce a 400 without any database write."""


def parse_json_body(request, *, max_bytes=None):
    """Parse a JSON request body under a byte ceiling.

    The length check runs against ``request.body`` before decoding, so an
    oversized payload never reaches the JSON parser.
    """
    if max_bytes is None:
        max_bytes = settings.COMMUNITY_WRITE_MAX_BODY_BYTES

    body = request.body
    if len(body) > max_bytes:
        raise InvalidInput("Request body is too large")

    try:
        data = json.loads(body, parse_constant=_reject_json_constant)
    except (ValueError, UnicodeDecodeError):
        raise InvalidInput("Invalid JSON in request body")

    if not isinstance(data, dict):
        raise InvalidInput("Request body must be a JSON object")

    return data


def validate_text(data, key, *, max_length, required=False, strip=True):
    """Read a string field and enforce its configured maximum length."""
    value = data.get(key, "")

    if value is None:
        value = ""
    if not isinstance(value, str):
        raise InvalidInput(f"'{key}' must be text")
    if strip:
        value = value.strip()

    if required and not value:
        raise InvalidInput(f"'{key}' is required")
    if len(value) > max_length:
        raise InvalidInput(f"'{key}' must be {max_length} characters or fewer")

    return value


def validate_choice(data, key, choices, *, required=True):
    """Read a field constrained to a known set of values."""
    value = data.get(key)

    if value is None:
        if required:
            raise InvalidInput(f"Missing required field: {key}")
        return None
    if value not in choices:
        raise InvalidInput(f"Invalid value for '{key}'")

    return value


def _finite_float(data, key):
    if key not in data:
        raise InvalidInput(f"Missing required field: {key}")

    value = data[key]
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise InvalidInput(f"'{key}' must be a number")

    try:
        value = float(value)
    except (TypeError, ValueError):
        raise InvalidInput(f"'{key}' must be a number")

    # Belt and braces: parse_json_body rejects bare NaN/Infinity literals, but
    # a quoted "nan" survives it and float() is happy to convert one.
    if not math.isfinite(value):
        raise InvalidInput(f"'{key}' must be a finite number")

    return value


def validate_latitude(data, key="latitude"):
    value = _finite_float(data, key)
    if not (LATITUDE_MIN <= value <= LATITUDE_MAX):
        raise InvalidInput("Latitude must be between -90 and 90")
    return value


def validate_longitude(data, key="longitude"):
    value = _finite_float(data, key)
    if not (LONGITUDE_MIN <= value <= LONGITUDE_MAX):
        raise InvalidInput("Longitude must be between -180 and 180")
    return value


def validate_direction(data, key="direction"):
    """Read an optional compass bearing in whole degrees.

    ``None`` and the empty string mean "not supplied". ``0`` does not: due
    north is a real bearing, so it must survive the falsiness check that the
    previous ``if data.get("direction")`` idiom silently dropped.
    """
    if key not in data:
        return None

    value = data[key]
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise InvalidInput("Direction must be a whole number of degrees")

    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise InvalidInput("Direction must be a whole number of degrees")
        value = int(value)
    elif isinstance(value, int):
        pass
    elif isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError:
            raise InvalidInput("Direction must be a whole number of degrees")
    else:
        raise InvalidInput("Direction must be a whole number of degrees")

    if not (0 <= value <= 359):
        raise InvalidInput("Direction must be between 0 and 359 degrees")

    return value


def _validate_ring(ring, *, budget):
    """Structurally validate one linear ring and return its vertex count."""
    if not isinstance(ring, (list, tuple)):
        raise InvalidInput("Polygon rings must be lists of positions")

    count = len(ring)
    if count < 4:
        raise InvalidInput("Each polygon ring needs at least four positions")
    if count > settings.POLYGON_MAX_VERTICES_PER_RING:
        raise InvalidInput(
            f"Each polygon ring may have at most "
            f"{settings.POLYGON_MAX_VERTICES_PER_RING} positions"
        )
    if count > budget:
        raise InvalidInput(
            f"Polygon may have at most {settings.POLYGON_MAX_TOTAL_VERTICES} "
            "positions in total"
        )

    for position in ring:
        if not isinstance(position, (list, tuple)) or len(position) < 2:
            raise InvalidInput("Polygon positions must be [longitude, latitude]")

        longitude, latitude = position[0], position[1]
        for value in (longitude, latitude):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise InvalidInput("Polygon coordinates must be numbers")
            if not math.isfinite(value):
                raise InvalidInput("Polygon coordinates must be finite")

        if not (LONGITUDE_MIN <= longitude <= LONGITUDE_MAX):
            raise InvalidInput("Polygon longitudes must be between -180 and 180")
        if not (LATITUDE_MIN <= latitude <= LATITUDE_MAX):
            raise InvalidInput("Polygon latitudes must be between -90 and 90")

    first, last = ring[0], ring[-1]
    if first[0] != last[0] or first[1] != last[1]:
        raise InvalidInput("Polygon rings must be closed")

    return count


def validate_polygon_geojson(value):
    """Check a GeoJSON polygon against every structural limit.

    Runs entirely on the decoded JSON, before any GEOS parsing or database
    work, so a hostile payload is rejected at its cheapest point. A
    ``MultiPolygon`` wrapping exactly one polygon is accepted and unwrapped,
    matching what the drawing UI submits.

    Returns the coordinate list of a single polygon; topology, emptiness and
    area are checked by the caller once GEOS has built the geometry.
    """
    if not isinstance(value, dict):
        raise InvalidInput("Invalid polygon geometry")

    geom_type = value.get("type")
    if geom_type not in ("Polygon", "MultiPolygon"):
        raise InvalidInput("Polygon must be a GeoJSON Polygon or MultiPolygon")

    coordinates = value.get("coordinates")
    if not isinstance(coordinates, (list, tuple)) or not coordinates:
        raise InvalidInput("Polygon has no coordinates")

    if geom_type == "MultiPolygon":
        if len(coordinates) != 1:
            raise InvalidInput("Please draw exactly one polygon")
        coordinates = coordinates[0]
        if not isinstance(coordinates, (list, tuple)) or not coordinates:
            raise InvalidInput("Polygon has no coordinates")

    if len(coordinates) > settings.POLYGON_MAX_RINGS:
        raise InvalidInput(
            f"Polygon may have at most {settings.POLYGON_MAX_RINGS} rings"
        )

    budget = settings.POLYGON_MAX_TOTAL_VERTICES
    for ring in coordinates:
        budget -= _validate_ring(ring, budget=budget)

    return list(coordinates)


def build_polygon(value):
    """Turn a client-supplied GeoJSON polygon into a validated GEOS polygon.

    Structural limits are applied first (see
    :func:`validate_polygon_geojson`), then GEOS builds the geometry and the
    remaining semantic checks run: it must be a non-empty, topologically valid
    polygon in WGS84 whose area is under the configured ceiling.
    """
    coordinates = validate_polygon_geojson(value)

    try:
        polygon = GEOSGeometry(
            json.dumps({"type": "Polygon", "coordinates": coordinates}),
            srid=WGS84_SRID,
        )
    except (GEOSException, ValueError, TypeError):
        raise InvalidInput("Invalid polygon geometry")

    if not isinstance(polygon, Polygon):
        raise InvalidInput("Polygon must be a GeoJSON Polygon or MultiPolygon")

    # GEOSGeometry honours an SRID embedded in the input over the srid kwarg,
    # so assert it rather than trusting the constructor.
    polygon.srid = WGS84_SRID

    if polygon.empty:
        raise InvalidInput("Polygon is empty")

    try:
        if not polygon.valid:
            raise InvalidInput(f"Polygon is not a valid shape: {polygon.valid_reason}")
        area = polygon.area
    except GEOSException:
        raise InvalidInput("Invalid polygon geometry")

    if area <= 0:
        raise InvalidInput("Polygon encloses no area")
    if area > settings.POLYGON_MAX_AREA_SQ_DEGREES:
        raise InvalidInput("Polygon covers too large an area")

    return polygon
