import hashlib

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db import connection
from django.db.models import F, Prefetch
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.cache import get_conditional_response, patch_cache_control
from django.views.decorators.http import require_http_methods

from images.utils import render_markdown_safe

from .forms import MapLayerForm
from .models import LayerCollection, MapLayer


@require_http_methods(["GET"])
def layer_extent_tile(request, z, x, y):
    """Cacheable discovery polygons; high-zoom visibility is filtered locally."""
    if not (
        0 <= z <= settings.MAP_LAYER_TILE_MAX_ZOOM
        and 0 <= x < 2**z
        and 0 <= y < 2**z
    ):
        return HttpResponse(status=404)

    # Use the existing WGS84 spatial index before transforming matching polygons.
    # At the source's maxzoom include ALL minimum zooms: the client overzooms
    # these tiles, so higher-minzoom sheets must already be present in them.
    sql = """
        WITH bounds AS (
            SELECT ST_TileEnvelope(%s, %s, %s) AS geom
        ), candidates AS (
            SELECT l.id, l.name, l.type, l.url, l.attribution, l.min_zoom,
                   l."order" AS layer_order, c.id AS collection_id,
                   c.name AS collection_name, c."order" AS collection_order,
                   ST_Transform(l.polygon, 3857) AS polygon, b.geom AS bounds
            FROM maps_maplayer l
            JOIN maps_layercollection c ON c.id = l.collection_id
            CROSS JOIN bounds b
            WHERE ST_Intersects(l.polygon, ST_Transform(b.geom, 4326))
              AND (l.min_zoom <= %s OR %s = %s)
        ), features AS (
            SELECT id, name, type, url, attribution, min_zoom, layer_order,
                   collection_id, collection_name, collection_order,
                   COALESCE(
                       ST_AsMVTGeom(polygon, bounds, 4096, 0),
                       ST_AsMVTGeom(
                           ST_PointOnSurface(ST_Intersection(polygon, bounds)),
                           bounds, 4096, 0
                       )
                   ) AS geom
            FROM candidates
        )
        SELECT ST_AsMVT(features.*, 'layer_extents', 4096, 'geom', 'id' ORDER BY id)
        FROM features WHERE geom IS NOT NULL
    """
    # Tiny polygons can collapse during tile quantization. Retain a point for
    # discovery instead of silently dropping those sheets from every zoom.
    with connection.cursor() as cursor:
        cursor.execute(sql, [z, x, y, z, z, settings.MAP_LAYER_TILE_MAX_ZOOM])
        tile = bytes(cursor.fetchone()[0] or b"")

    response = HttpResponse(tile, content_type="application/vnd.mapbox-vector-tile")
    patch_cache_control(
        response, public=True, max_age=settings.MAP_LAYER_TILE_CACHE_SECONDS
    )
    etag = f'"{hashlib.sha256(tile).hexdigest()}"'
    response["ETag"] = etag
    return get_conditional_response(request, etag=etag, response=response)


def browse_maps(request):
    """Display all map layers organized by collections."""
    collections = LayerCollection.objects.prefetch_related(
        Prefetch("layers", queryset=MapLayer.objects.order_by("order"))
    ).order_by("order")
    # Render markdown for collection descriptions and layer descriptions
    collections_with_rendered = []
    for collection in collections:
        rendered_description = None
        if collection.description:
            rendered_description = render_markdown_safe(collection.description)

        # Render markdown for each layer description
        layers_with_rendered = []
        for layer in collection.layers.all():
            rendered_layer_description = None
            if layer.description:
                rendered_layer_description = render_markdown_safe(layer.description)
            layers_with_rendered.append(
                {"layer": layer, "rendered_description": rendered_layer_description}
            )

        collections_with_rendered.append(
            {
                "collection": collection,
                "rendered_description": rendered_description,
                "layers": layers_with_rendered,
            }
        )
    return render(
        request, "maps/browse_maps.html", {"collections": collections_with_rendered}
    )


def layer_detail(request, collection_slug, layer_slug):
    """Display a single map layer."""
    layer = get_object_or_404(
        MapLayer, collection__slug=collection_slug, slug=layer_slug
    )

    # Render markdown for layer description
    rendered_description = None
    if layer.description:
        rendered_description = render_markdown_safe(layer.description)

    context = {
        "layer": layer,
        "rendered_description": rendered_description,
        "protomaps_api_key": settings.PROTOMAPS_API_KEY or "",
    }
    return render(request, "maps/map_detail.html", context)


@staff_member_required
def layer_manage(request):
    """Staff list of every map layer, primaries first."""
    layers = MapLayer.objects.select_related("collection").order_by(
        F("collection__order").asc(nulls_first=True),
        "collection__name",
        "order",
        "name",
    )
    return render(request, "maps/layer_manage.html", {"layers": layers})


def _render_layer_form(request, form, layer=None):
    context = {
        "form": form,
        "layer": layer,
        "protomaps_api_key": settings.PROTOMAPS_API_KEY or "",
    }
    return render(request, "maps/layer_form.html", context)


@staff_member_required
def layer_create(request):
    """Create a map layer, then land on its edit page."""
    if request.method == "POST":
        form = MapLayerForm(request.POST)
        if form.is_valid():
            layer = form.save()
            messages.success(request, f'Created layer "{layer.name}".')
            return redirect("maps:layer_edit", pk=layer.pk)
    else:
        form = MapLayerForm()
    return _render_layer_form(request, form)


@staff_member_required
def layer_edit(request, pk):
    """Edit an existing map layer beside a live preview."""
    layer = get_object_or_404(MapLayer.objects.select_related("collection"), pk=pk)
    if request.method == "POST":
        form = MapLayerForm(request.POST, instance=layer)
        if form.is_valid():
            layer = form.save()
            messages.success(request, f'Saved layer "{layer.name}".')
            return redirect("maps:layer_edit", pk=layer.pk)
    else:
        form = MapLayerForm(instance=layer)
    return _render_layer_form(request, form, layer=layer)


@require_http_methods(["POST"])
@staff_member_required
def layer_delete(request, pk):
    """Delete a map layer (confirmed by the modal on the edit page)."""
    layer = get_object_or_404(MapLayer, pk=pk)
    name = layer.name
    layer.delete()
    messages.success(request, f'Deleted layer "{name}".')
    return redirect("maps:layer_manage")
