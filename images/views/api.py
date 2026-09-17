import hashlib
import json

from django.contrib.gis.geos import Point
from django.db import connection
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse

from regions.models import Region

from ..models import Image, TileVersion
from .core import get_min_scale_for_zoom


def geojson_endpoint(request):
    """Return GeoJSON FeatureCollection of georeferenced images.

    Reads from the public_georeferences_mvt materialized view for performance,
    joining to live tables only for permalink and subject data.
    """

    # Apply filters based on GET parameters
    image_id = request.GET.get("image")
    collection_id = request.GET.get("collection")
    source_id = request.GET.get("source")
    subject_id = request.GET.get("subject")

    where_conditions = []
    where_params = []

    if image_id:
        where_conditions.append("mv.image_id = %s")
        where_params.append(image_id)
    if collection_id:
        where_conditions.append("i.collection_id = %s")
        where_params.append(collection_id)
    if source_id:
        where_conditions.append(
            "i.collection_id IN (SELECT id FROM images_collection WHERE source_id = %s)"
        )
        where_params.append(source_id)
    if subject_id:
        where_conditions.append(
            "mv.image_id IN (SELECT image_id FROM images_subjectmapping WHERE subject_id = %s)"
        )
        where_params.append(subject_id)

    where_clause = " AND ".join(where_conditions)
    if where_clause:
        where_clause = f"WHERE {where_clause}"

    sql = f"""
        SELECT
            mv.image_id,
            ST_X(mv.point) as lon,
            ST_Y(mv.point) as lat,
            COALESCE(i.transformed_permalink, i.permalink) as permalink,
            mv.original_date,
            mv.edtf_date,
            mv.start_decdate,
            mv.fuzzy_start_decdate,
            mv.end_decdate,
            mv.fuzzy_end_decdate,
            mv.scale,
            mv.direction,
            (
                SELECT array_agg(wi.wikidata_id)
                FROM images_subjectmapping sm
                JOIN subjects_subject ss ON sm.subject_id = ss.id
                JOIN subjects_wikidataitem wi ON ss.wikidata_item_id = wi.id
                WHERE sm.image_id = mv.image_id
            ) as subjects
        FROM public_georeferences_mvt mv
        JOIN images_image i ON mv.image_id = i.id
        {where_clause}
    """

    features = []
    with connection.cursor() as cursor:
        cursor.execute(sql, where_params)
        columns = [col.name for col in cursor.description]

        for row in cursor.fetchall():
            data = dict(zip(columns, row))

            img_entry = request.build_absolute_uri(
                reverse("images:image_detail", kwargs={"image_id": data["image_id"]})
            )

            properties = {
                "img_url": data["permalink"],
                "img_entry": img_entry,
                "original_date": data["original_date"] or None,
                "edtf_date": data["edtf_date"] or None,
                "start_decdate": data["start_decdate"],
                "fuzzy_start_decdate": data["fuzzy_start_decdate"],
                "end_decdate": data["end_decdate"],
                "fuzzy_end_decdate": data["fuzzy_end_decdate"],
            }

            if data["direction"] is not None:
                properties["direction"] = data["direction"]

            if data["scale"] and data["scale"] != 0:
                properties["scale"] = data["scale"]

            if data["subjects"]:
                properties["subjects"] = data["subjects"]

            features.append(
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Point",
                        "coordinates": [data["lon"], data["lat"]],
                    },
                    "properties": properties,
                }
            )

    geojson = {"type": "FeatureCollection", "features": features}

    return JsonResponse(geojson)


def _build_aerial_georeference_feature(image, aerial_georeference, request):
    """
    Helper function to build a GeoJSON feature for an aerial georeference.

    Args:
        image: Image model instance
        aerial_georeference: AerialGeoreference model instance
        request: Django request object (for building absolute URLs)

    Returns:
        GeoJSON feature dict
    """
    # Build the image entry URL (absolute URL to image detail page)
    img_entry = request.build_absolute_uri(
        reverse("images:image_detail", kwargs={"image_id": image.id})
    )

    # Build properties
    properties = {
        "id": image.id,
        "img_url": image.display_permalink,
        "img_entry": img_entry,
        "original_date": str(image.original_date) if image.original_date else None,
        "edtf_date": str(image.edtf_date) if image.edtf_date else None,
        "start_decdate": image.start_decdate,
        "fuzzy_start_decdate": image.fuzzy_start_decdate,
        "end_decdate": image.end_decdate,
        "fuzzy_end_decdate": image.fuzzy_end_decdate,
        "confidence": aerial_georeference.confidence,
        "georeferenced_by": aerial_georeference.georeferenced_by.username
        if aerial_georeference.georeferenced_by
        else None,
    }

    # Only include scale if it's not None
    if image.scale is not None:
        properties["scale"] = image.scale

    # Add subjects as Wikidata IDs if they exist
    subject_wikidata_ids = [
        mapping.subject.wikidata_item.wikidata_id
        for mapping in image.subject_mappings.all()
        if mapping.subject.wikidata_item
    ]
    if subject_wikidata_ids:
        properties["subjects"] = subject_wikidata_ids

    return {
        "type": "Feature",
        "geometry": json.loads(aerial_georeference.polygon.geojson),
        "properties": properties,
    }


def aerial_geojson_endpoint(request):
    """Return GeoJSON FeatureCollection of aerial georeferences (most recent per image)"""
    # Start with all aerial images that have aerial georeferences from public collections/sources
    images = (
        Image.objects.select_related("collection__source")
        .prefetch_related("aerial_georeferences")
        .filter(
            aerial=True,  # Must be marked as aerial
            aerial_georeferences__isnull=False,  # Must have aerial georeferences
            collection__public=True,  # Collection must be public
            collection__source__public=True,  # Source must be public
            duplicate_of__isnull=True,  # Exclude duplicate images
        )
        .distinct()
    )

    # Apply filters based on GET parameters
    image_id = request.GET.get("image")
    collection_id = request.GET.get("collection")
    source_id = request.GET.get("source")
    subject_id = request.GET.get("subject")

    if image_id:
        images = images.filter(id=image_id)
    if collection_id:
        images = images.filter(collection_id=collection_id)
    if source_id:
        images = images.filter(collection__source_id=source_id)
    if subject_id:
        images = images.filter(subject_mappings__subject_id=subject_id)

    # Build GeoJSON features
    features = []
    for image in images:
        # Get the most recent aerial georeference (like get_georeference for regular ones)
        aerial_georeference = image.get_aerial_georeference()
        if not aerial_georeference:  # Skip if no aerial georeference found
            continue

        feature = _build_aerial_georeference_feature(
            image, aerial_georeference, request
        )
        features.append(feature)

    # Build final GeoJSON
    geojson = {"type": "FeatureCollection", "features": features}
    return JsonResponse(geojson)


def polygonal_georeferences_at_point(request):
    """
    API endpoint that returns all aerial georeferences that overlap a given point.

    Query parameters:
    - lat: Latitude (required)
    - lon: Longitude (required)

    Returns: GeoJSON FeatureCollection of aerial georeferences containing the point
    """

    # Get lat/lon from query parameters
    try:
        lat = float(request.GET.get("lat"))
        lon = float(request.GET.get("lon"))
    except (TypeError, ValueError):
        return JsonResponse(
            {"error": "Invalid or missing lat/lon parameters"}, status=400
        )

    # Validate coordinates are within reasonable bounds
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return JsonResponse({"error": "Coordinates out of valid range"}, status=400)

    # Create a Point from the coordinates (note: Point uses lon, lat order)
    point = Point(lon, lat)

    # Query for aerial georeferences that contain this point
    # We need to use the polygon field's contains lookup

    # Get all aerial images with public collections/sources

    images = (
        Image.objects.filter(
            aerial=True,  # Must be marked as aerial
            aerial_georeferences__isnull=False,  # Must have aerial georeferences
            collection__public=True,  # Collection must be public
            collection__source__public=True,  # Source must be public
            duplicate_of__isnull=True,  # Exclude duplicate images
        )
        .select_related("collection__source")
        .prefetch_related("aerial_georeferences")
        .distinct()
    )

    # Build GeoJSON features - only include if most recent georeference contains the point
    features = []
    for image in images:
        # Get the most recent aerial georeference for this image
        aerial_georeference = image.get_aerial_georeference()
        if not aerial_georeference:
            continue

        # Check if this georeference's polygon contains the point
        try:
            if not aerial_georeference.polygon.contains(point):
                continue
        except Exception:
            continue

        # Build feature using helper
        feature = _build_aerial_georeference_feature(
            image, aerial_georeference, request
        )
        # Add validation count (specific to this endpoint)
        feature["properties"]["validation_count"] = aerial_georeference.validation_count
        features.append(feature)

    # Build final GeoJSON
    geojson = {
        "type": "FeatureCollection",
        "features": features,
        "query": {
            "lat": lat,
            "lon": lon,
        },
        "count": len(features),
    }
    return JsonResponse(geojson)


def get_tile_version() -> str:
    """Get current tile data version from the database."""
    return str(TileVersion.get_version())


def bump_tile_version():
    """Increment tile version, invalidating all cached tiles."""
    return str(TileVersion.bump())


def vector_tiles_endpoint(request, z, x, y, v=None):
    """Return MVT vector tiles of georeferenced images.

    Tiles are cached at the CDN edge, not on the server. Versioned URLs (v) are
    the invalidation mechanism: when the version changes, all tile URLs change,
    so stale cached tiles are simply never requested again. Unversioned URLs
    can never be invalidated, so they must not be edge-cached.
    """

    # Collect filter parameters
    enable_scale_filter = (
        request.GET.get("enable_scale_filter", "false").lower() == "true"
    )
    image_id = request.GET.get("image")
    collection_id = request.GET.get("collection")
    source_id = request.GET.get("source")
    subject_id = request.GET.get("subject")
    album_id = request.GET.get("album")
    georeferenced_by = request.GET.get("georeferenced_by")

    region_qid = request.GET.get("region")
    region = (
        get_object_or_404(Region, wikidata_item__wikidata_id=region_qid)
        if region_qid
        else None
    )
    is_filtered = any(
        [
            image_id, collection_id, source_id, subject_id,
            album_id, georeferenced_by, region,
        ]
    )

    mvt_data = _generate_tile(
        z,
        x,
        y,
        enable_scale_filter,
        image_id,
        collection_id,
        source_id,
        subject_id,
        album_id,
        georeferenced_by,
        region=region,
    )

    return _make_tile_response(mvt_data, is_filtered, versioned=v is not None)


def _make_tile_response(
    mvt_data: bytes, is_filtered: bool, versioned: bool
) -> HttpResponse:
    """Create response with appropriate cache headers."""
    response = HttpResponse(mvt_data, content_type="application/x-protobuf")

    if is_filtered:
        # Don't cache filtered tiles in browser, short edge cache
        response["Cache-Control"] = "public, max-age=0, s-maxage=300"
    elif versioned:
        # Versioned URLs mean stale tiles are never requested again, so cache aggressively
        response["Cache-Control"] = "public, max-age=86400, s-maxage=604800"
    else:
        # Unversioned URLs never rotate, so an edge-cached tile would stay stale
        # until its TTL expired, with no way to invalidate it
        response["Cache-Control"] = "public, max-age=0, s-maxage=0"

    response["ETag"] = f'"{hashlib.md5(mvt_data).hexdigest()}"'

    return response


def _generate_tile(
    z,
    x,
    y,
    enable_scale_filter,
    image_id,
    collection_id,
    source_id,
    subject_id,
    album_id,
    georeferenced_by=None,
    region=None,
) -> bytes:
    """Generate MVT tile from database."""

    # Build WHERE conditions for filtering
    where_conditions = []
    where_params = []

    if enable_scale_filter:
        min_scale = get_min_scale_for_zoom(z)
        if min_scale is not None:
            where_conditions.append("(scale >= %s OR scale = 0)")
            where_params.append(min_scale)

    if image_id:
        where_conditions.append("image_id = %s")
        where_params.append(image_id)
    if collection_id:
        where_conditions.append(
            "image_id IN (SELECT id FROM images_image WHERE collection_id = %s)"
        )
        where_params.append(collection_id)
    if source_id:
        where_conditions.append(
            "image_id IN (SELECT i.id FROM images_image i JOIN images_collection c ON i.collection_id = c.id WHERE c.source_id = %s)"
        )
        where_params.append(source_id)
    if region is not None:
        effective_region_sql = Image.EFFECTIVE_REGION_SQL.format(alias="i.")
        where_conditions.append(
            "image_id IN (SELECT i.id FROM images_image i "
            "WHERE i.duplicate_of_id IS NULL AND "
            + effective_region_sql
            + " = ANY(%s))"
        )
        where_params.append(region.self_and_descendant_ids())
    if subject_id:
        # Include images mapped to this subject OR to any descendant subject
        # (one whose materialized ancestor set in subjects_subjectancestor
        # contains this subject's WikidataItem).
        where_conditions.append(
            """image_id IN (
                SELECT image_id FROM images_subjectmapping
                WHERE subject_id = %s
                   OR subject_id IN (
                       SELECT sa.subject_id
                       FROM subjects_subjectancestor sa
                       JOIN subjects_subject s ON s.id = %s
                       WHERE sa.ancestor_id = s.wikidata_item_id
                   )
            )"""
        )
        where_params.append(subject_id)
        where_params.append(subject_id)
    if album_id:
        where_conditions.append(
            "image_id IN (SELECT image_id FROM images_albumimage WHERE album_id = %s)"
        )
        where_params.append(album_id)
    if georeferenced_by:
        # Matches on georeference_id, not image_id: the materialized view keeps
        # only the latest georeference per image, so this is "images whose
        # current georeference is this user's". Images they georeferenced but
        # someone else has since corrected are deliberately not their pins.
        where_conditions.append(
            "georeference_id IN (SELECT id FROM images_georeference WHERE georeferenced_by_id = %s)"
        )
        where_params.append(georeferenced_by)

    where_clause = " AND ".join(where_conditions)

    if where_clause:
        where_clause_sql = f"WHERE {where_clause} AND ST_Intersects(point_3857, ST_TileEnvelope(%s, %s, %s))"
    else:
        where_clause_sql = (
            "WHERE ST_Intersects(point_3857, ST_TileEnvelope(%s, %s, %s))"
        )

    sql = f"""
        SELECT ST_AsMVT(mvtgeoms.*, 'image_points') as mvt FROM (
            SELECT
                ST_AsMVTGeom(point_3857, ST_TileEnvelope(%s, %s, %s)) AS geom,
                image_id as id,
                thumbnail,
                original_date,
                edtf_date,
                start_decdate,
                fuzzy_start_decdate,
                end_decdate,
                fuzzy_end_decdate,
                scale,
                direction,
                confidence
            FROM public_georeferences_mvt
            {where_clause_sql}
        ) mvtgeoms
    """

    query_params = [z, x, y] + where_params + [z, x, y]

    with connection.cursor() as cursor:
        cursor.execute(sql, query_params)
        result = cursor.fetchone()

        if result and result[0]:
            return bytes(result[0])
        else:
            return b""


def osm_elements_vector_tiles_endpoint(request, z, x, y):
    """Return MVT vector tiles of OSM elements (mixed geometries: points, lines, polygons)"""

    sql = """
        SELECT ST_AsMVT(mvtgeoms.*, 'osm_elements') as mvt FROM (
            SELECT
                ST_AsMVTGeom(ST_Transform(oe.geometry, 3857), ST_TileEnvelope(%s, %s, %s)) AS geom,
                oe.osm_id as osm_id,
                ST_GeometryType(oe.geometry) as geom_type,
                s.title as subject_name,
                s.slug as subject_slug,
                COALESCE(
                    string_agg(CAST(sm.image_id AS text), ','),
                    ''
                ) as image_ids,
                oe.geometry_area as geometry_area
            FROM subjects_osmelement oe
            LEFT JOIN subjects_subject s ON s.id = oe.subject_id
            INNER JOIN images_subjectmapping sm ON s.id = sm.subject_id
            WHERE ST_Intersects(oe.geometry, ST_Transform(ST_TileEnvelope(%s, %s, %s), 4326))
            GROUP BY oe.id, s.id, oe.osm_id, oe.geometry, s.title, s.slug, oe.geometry_area
            ORDER BY
                CASE
                    WHEN ST_GeometryType(oe.geometry) IN ('ST_Polygon', 'ST_MultiPolygon') THEN oe.geometry_area
                    ELSE 0
                END ASC,
                oe.osm_id ASC
        ) mvtgeoms
    """

    query_params = [z, x, y, z, x, y]

    with connection.cursor() as cursor:
        cursor.execute(sql, query_params)
        result = cursor.fetchone()

        if result and result[0]:
            mvt_data = bytes(result[0])
            response = HttpResponse(mvt_data, content_type="application/x-protobuf")
            return response
        else:
            return HttpResponse(b"", content_type="application/x-protobuf")
