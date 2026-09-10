from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import render

from .models import Region
from .summaries import get_region_summaries

# Same DoS bound as the subjects autocompletes (subjects/views.py).
_MAX_AUTOCOMPLETE_QUERY_LEN = 100

# Keep the navbar dropdown focused while the full directory remains available
# through the "Explore all regions" link.
_AUTOCOMPLETE_LIMIT = 3


def region_index(request):
    """A browsable directory of the places represented on Yesterdays.

    The resting page and map show regions an admin chose to advertise.
    Every other region still gets a card, busiest first like the rest, but
    it renders hidden and only surfaces when the visitor searches. Both
    lists are built from one pass and split here.
    """
    summaries = get_region_summaries(advertised_only=False)
    return render(
        request,
        "regions/browse_regions.html",
        {
            "regions": summaries,
            "region_summaries": [
                summary for summary in summaries if summary["advertise"]
            ],
        },
    )


def _popular_regions():
    """The most-georeferenced regions, in the shape the endpoint returns.

    Built from the picker summaries so "how many photographs are placed
    here" means the same thing in the navbar as it does on the region
    directory — public holdings only, rolled up each P131 chain — then
    reranked: the directory sorts by library size, this list by how much
    of that library is on the map.

    Only explicitly advertised regions are candidates. Searching still
    finds every region.
    """
    summaries = sorted(
        get_region_summaries(),
        # Ties fall back to name, keeping the order stable while young
        # regions sit at zero.
        key=lambda entry: (-entry["georeferenced_count"], entry["long_name"]),
    )
    return [
        {
            "slug": summary["slug"],
            "short_name": summary["short_name"],
            "long_name": summary["long_name"],
            "wikidata_id": summary["wikidata_id"],
        }
        for summary in summaries[:_AUTOCOMPLETE_LIMIT]
    ]


def region_autocomplete(request):
    """Public GET returning regions matching ``q`` as a JSON array.

    An empty or missing ``q`` returns the three most-georeferenced
    advertised regions, which the dropdown heads with "Popular". A query
    instead filters every region by either display name, alphabetically,
    before that same cap is applied.
    """
    query = request.GET.get("q", "").strip()
    if len(query) > _MAX_AUTOCOMPLETE_QUERY_LEN:
        return JsonResponse([], safe=False)

    if not query:
        return JsonResponse(_popular_regions(), safe=False)

    regions = Region.objects.select_related("wikidata_item").filter(
        Q(short_name__icontains=query) | Q(long_name__icontains=query)
    )
    results = [
        {
            "slug": region.slug,
            "short_name": region.short_name,
            "long_name": region.long_name,
            "wikidata_id": region.wikidata_item.wikidata_id,
        }
        for region in regions[:_AUTOCOMPLETE_LIMIT]
    ]
    return JsonResponse(results, safe=False)
