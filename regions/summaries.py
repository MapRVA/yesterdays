from django.db.models import F, Sum

from images.models import CollectionRegionStats

from .models import Region


def get_region_summaries(advertised_only=True):
    """Regions, with everything a region picker shows for each of them.

    One entry per region: identity and centerpoint for the map pin, public
    image counts for the picker card, and the region's hand-picked
    representative image as a thumbnail. The same dicts render the
    server-side cards and are serialized inline for the map (see
    templates/partials/global_home_map.html) — one fewer round trip before
    the map has anything on it, and regions are a small curated list, so
    the inline payload stays cheap. Key names follow the autocomplete
    endpoint's (regions.views) so both describe a region to the frontend
    the same way.

    ``advertised_only`` limits the result to regions an admin chose to
    promote. The global homepage, directory map, and navbar's Popular list
    use that subset. The directory also requests every region so it can keep
    unadvertised cards hidden until a search surfaces them.
    """
    # Counts come from the denormalized per-region stats, restricted — as
    # every read of CollectionStats is — to public collections and sources
    # at read time. The table already rolls images up each P131 chain, so
    # summing rows per region is the whole aggregation.
    counts = {
        row["region_id"]: row
        for row in CollectionRegionStats.objects.filter(
            collection__public=True, collection__source__public=True
        )
        .values("region_id")
        .annotate(
            # Net of will-not-georeference, matching get_overall_stats and
            # get_confidence_breakdown: the sitewide band and these cards
            # sit on the same page and have to mean the same thing by "a
            # photograph". (They still won't sum to each other — the table
            # rolls each image up its whole P131 chain on purpose, so a
            # Richmond photo counts toward Richmond and Virginia both.)
            total=Sum(F("total_images") - F("will_not_georef_images")),
            georeferenced=Sum(
                F("georeferenced_low")
                + F("georeferenced_medium")
                + F("georeferenced_high")
            ),
        )
    }
    regions = Region.objects.select_related(
        "representative_image", "wikidata_item"
    )
    if advertised_only:
        regions = regions.filter(advertise=True)

    summaries = []
    for region in regions:
        row = counts.get(region.id)
        summaries.append(
            {
                "short_name": region.short_name,
                "long_name": region.long_name,
                "wikidata_id": region.wikidata_item.wikidata_id,
                "subtitle": region.subtitle,
                "lng": region.coordinate_location.x,
                "lat": region.coordinate_location.y,
                "image_count": row["total"] if row else 0,
                "georeferenced_count": row["georeferenced"] if row else 0,
                # Normalized so blank and null both read as "no photo" on
                # both sides of the wire.
                "thumbnail": (
                    region.representative_image.thumbnail or None
                    if region.representative_image
                    else None
                ),
                "advertise": region.advertise,
            }
        )
    # Busiest regions first (the same "densest first" ordering the source
    # page uses for collections); ties fall back to name so the order is
    # stable while a young region is still at zero.
    summaries.sort(key=lambda entry: (-entry["image_count"], entry["long_name"]))
    return summaries
