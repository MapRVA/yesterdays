import json

from django.conf import settings
from django.urls import reverse

from maps.models import MapLayer
from regions.context_processors import get_current_region

from .models import ImageOfTheDay, SiteSettings
from .views import get_tile_version


def _build_map_layers_data():
    """Build the map layers JSON for the frontend layer control."""
    primary_layers = []
    for layer in MapLayer.objects.filter(collection__isnull=True).order_by("order"):
        layer_data = {
            "slug": layer.slug,
            "name": layer.name,
            "type": layer.type,
            "url": layer.url,
            "is_default": layer.is_default,
        }
        if layer.attribution:
            layer_data["attribution"] = layer.attribution
        primary_layers.append(layer_data)

    return {
        "primary_layers": primary_layers,
        "overlay_tiles": {
            "url": reverse("maps:layer_extent_tile", args=[0, 0, 0]).replace(
                "/0/0/0.mvt", "/{z}/{x}/{y}.mvt"
            ),
            "maxzoom": settings.MAP_LAYER_TILE_MAX_ZOOM,
        },
    }


def site_settings(request):
    """
    Context processor to make site settings available globally in all templates
    """
    site_settings_model = SiteSettings.load()
    return {
        "site_title": site_settings_model.site_title,
        "site_subtitle": site_settings_model.site_subtitle,
        "footer_content": site_settings_model.footer_content,
        "protomaps_api_key": settings.PROTOMAPS_API_KEY,
        "default_map_center": [
            site_settings_model.default_map_longitude,
            site_settings_model.default_map_latitude,
        ],
        "default_map_zoom": site_settings_model.default_map_zoom,
        "default_search_bbox": [
            site_settings_model.default_search_bbox_west,
            site_settings_model.default_search_bbox_south,
            site_settings_model.default_search_bbox_east,
            site_settings_model.default_search_bbox_north,
        ],
        "admin_email": site_settings_model.admin_email,
        "tile_version": get_tile_version(),
        "DIRECTORIES_ENABLED": settings.DIRECTORIES_ENABLED,
        "map_layers_json": json.dumps(_build_map_layers_data()),
    }


def featured_image_queue_alert(request):
    """Warn staff in the navbar when the current region's queue runs low.

    Only staff see the Admin menu, so everyone else skips the queries.
    ``featured_queue_badge_class`` is None when there is nothing to flag.

    Regions that have never featured an image are skipped too: an empty
    queue is that region's normal state, so nagging about it would leave the
    dot permanently lit for anyone who simply isn't using the feature.
    """
    user = getattr(request, "user", None)
    if not (user and user.is_staff):
        return {}
    region = get_current_region(request)
    if region is None:
        return {}
    if not ImageOfTheDay.ever_featured(region):
        return {}
    days_queued = ImageOfTheDay.days_queued(region)
    return {
        "featured_queue_days": days_queued,
        "featured_queue_badge_class": ImageOfTheDay.queue_badge_class(days_queued),
    }
