from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.postgres.search import TrigramWordSimilarity
from django.db.models import Case, IntegerField, Q, Value, When
from django.db.models.functions import Greatest, Lower
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from .forms import RegionForm
from .models import Region
from .summaries import get_region_summaries

# Same DoS bound as the subjects autocompletes (subjects/views.py).
_MAX_AUTOCOMPLETE_QUERY_LEN = 100

# Keep the navbar dropdown focused while the full directory remains available
# through the "Explore all regions" link.
_AUTOCOMPLETE_LIMIT = 3

# Minimum ``word_similarity(query, name)`` for a fuzzy-only hit, and the
# shortest query the fuzzy branch runs for. Both match the subject-tagging
# autocomplete (subjects/views.py): the threshold sits below Postgres's 0.3
# default so a transposition in a ~6-letter word still matches
# (``word_similarity('chruch', 'Church Hill')`` is about 0.27), and one
# character is too little to say anything about a name, so single-character
# queries stay on literal matching alone.
_AUTOCOMPLETE_WORD_SIMILARITY_THRESHOLD = 0.25
_MIN_FUZZY_QUERY_LEN = 2


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


@staff_member_required
def region_manage(request):
    """Staff list of regions available for metadata editing."""
    regions = Region.objects.select_related("wikidata_item").order_by("long_name")
    return render(request, "regions/region_manage.html", {"regions": regions})


def _point_data(point):
    return [point.x, point.y] if point is not None else None


def _render_region_form(request, form, region=None):
    effective_point = region.coordinate_location if region is not None else None
    wikidata_point = (
        region.wikidata_coordinate_location if region is not None else None
    )
    editor_config = {
        "protomapsApiKey": settings.PROTOMAPS_API_KEY or "",
        "effectiveCenter": _point_data(effective_point),
        "wikidataCenter": _point_data(wikidata_point),
    }
    return render(
        request,
        "regions/region_form.html",
        {"form": form, "region": region, "region_editor_config": editor_config},
    )


@staff_member_required
def region_create(request):
    """Create a region, then land on its edit page."""
    if request.method == "POST":
        form = RegionForm(request.POST)
        if form.is_valid():
            region = form.save()
            messages.success(request, f'Created region "{region.long_name}".')
            return redirect("regions:region_edit", pk=region.pk)
    else:
        form = RegionForm()
    return _render_region_form(request, form)


@staff_member_required
def region_edit(request, pk):
    """Edit region metadata and spatial settings on a MapLibre map."""
    region = get_object_or_404(
        Region.objects.select_related("wikidata_item", "representative_image"),
        pk=pk,
    )
    if request.method == "POST":
        form = RegionForm(request.POST, instance=region)
        if form.is_valid():
            region = form.save()
            messages.success(request, f'Saved region "{region.long_name}".')
            return redirect("regions:region_edit", pk=region.pk)
    else:
        form = RegionForm(instance=region)
    return _render_region_form(request, form, region)


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
    instead searches every region by either display name, fuzzily: a
    literal match (exact, then prefix, then substring) on either name
    always outranks a trigram-only hit, so a typo still finds a place
    without pushing the name that was actually typed down the list.

    Word similarity is measured against both names and the better of the
    two wins, since a visitor may type either the compact navbar name or
    the disambiguated one. It also orders within a tier, so a whole-word
    match leads the prefix matches that merely start the same way.
    """
    query = request.GET.get("q", "").strip()
    if len(query) > _MAX_AUTOCOMPLETE_QUERY_LEN:
        return JsonResponse([], safe=False)

    if not query:
        return JsonResponse(_popular_regions(), safe=False)

    literal = Q(short_name__icontains=query) | Q(long_name__icontains=query)
    matches = literal
    if len(query) >= _MIN_FUZZY_QUERY_LEN:
        matches |= Q(similarity__gte=_AUTOCOMPLETE_WORD_SIMILARITY_THRESHOLD)

    regions = (
        Region.objects.annotate(
            similarity=Greatest(
                TrigramWordSimilarity(query, "short_name"),
                TrigramWordSimilarity(query, "long_name"),
            ),
            match_rank=Case(
                When(
                    Q(short_name__iexact=query) | Q(long_name__iexact=query),
                    then=Value(0),
                ),
                When(
                    Q(short_name__istartswith=query)
                    | Q(long_name__istartswith=query),
                    then=Value(1),
                ),
                When(literal, then=Value(2)),
                default=Value(3),
                output_field=IntegerField(),
            ),
            lower_long_name=Lower("long_name"),
        )
        .filter(matches)
        .select_related("wikidata_item")
        .order_by("match_rank", "-similarity", "lower_long_name")
    )
    results = [
        {
            "short_name": region.short_name,
            "long_name": region.long_name,
            "wikidata_id": region.wikidata_item.wikidata_id,
        }
        for region in regions[:_AUTOCOMPLETE_LIMIT]
    ]
    return JsonResponse(results, safe=False)
