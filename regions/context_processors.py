from .models import Region

# Cookie persisting the navbar region selection (value: a Wikidata QID).
# Written client-side by assets/js/components/region_selector.ts; the name
# reaches that component via a data attribute on the selector partial.
REGION_COOKIE_NAME = "region"


def get_current_region(request):
    """The Region picked in the navbar selector, or None.

    New cookies contain a Wikidata QID. Region slugs remain readable as a
    transition for existing visitors, but every frontend writer stores QIDs.
    Unknown or stale values resolve to None rather than erroring. Views
    that branch on the selection (e.g. the homepage) share this with the
    context processor below.
    """
    identifier = request.COOKIES.get(REGION_COOKIE_NAME, "")
    if not identifier:
        return None
    if identifier.startswith("Q") and identifier[1:].isdigit():
        return Region.objects.select_related("wikidata_item").filter(
            wikidata_item__wikidata_id=identifier
        ).first()
    return Region.objects.select_related("wikidata_item").filter(
        slug=identifier
    ).first()


def current_region(request):
    """Resolve the region cookie and its frontend map configuration."""
    region = get_current_region(request)
    center = None
    map_bounds = None
    search_bounds = None
    if region is not None:
        point = region.coordinate_location
        center = [point.x, point.y]
        map_bounds = region.map_bbox
        search_bounds = region.search_bbox
    return {
        "current_region": region,
        "region_cookie_name": REGION_COOKIE_NAME,
        "region_map_center": center,
        "region_map_bounds": map_bounds,
        "region_search_bbox": search_bounds,
    }
