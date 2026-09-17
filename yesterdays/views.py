import json
import random

from django.core.cache import cache
from django.db import connection
from django.db.models import Avg, Count, Exists, F, Max, OuterRef
from django.db.models.functions import TruncDate
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from activity.views import get_activity_events
from images.models import (
    AerialGeoreference,
    Georeference,
    GeoreferenceValidation,
    Image,
    ImageOfTheDay,
    ImageRating,
    SiteSettings,
    TopRatedImageView,
)
from images.utils import get_confidence_breakdown, get_overall_stats
from regions.context_processors import get_current_region
from regions.summaries import get_region_summaries


# The homepage picture lookups cache for the same 15 minutes, and the
# single-image ones store "" for "there is nothing to show" — see
# get_top_rated_image.
PICTURE_CACHE_SECONDS = 900

# The hero draws a fresh photograph on every request from a cached pool of
# ids: every public, georeferenced photograph the community has rated at
# least this many stars (out of five; ratings are stored out of ten). Ids
# rather than flattened features, because the pool is the whole qualifying
# set rather than a sample and only one entry is ever needed per request.
HERO_MIN_STARS = 4.5
HERO_POOL_CACHE_KEY = "global_home_hero_pool_v1"

# How long the hero remembers having nothing to offer. A full pool can hold
# for the whole period, but an empty one means nobody has rated an image
# highly enough yet, which stops being true the moment someone does — and
# the band would otherwise sit in its fallback for the rest of the period.
# The cache is per-process LocMem, so a save-time invalidation couldn't
# reach the other workers anyway; a short retry is the honest fix.
POOL_MISS_CACHE_SECONDS = 60

# A pooled candidate may have changed since the pool was built — deleted,
# made private, or stripped of its georeference — so each draw is checked
# and retried. Bounded because each retry is a query, and an exhausted draw
# just falls through to the fallback.
POOL_DRAW_ATTEMPTS = 3

# The global homepage's activity band is a three-card panel laid out beside
# its heading, so it asks for three whatever the feed settings say.
# SiteSettings.home_feed_item_count still governs the region homepage, where
# the feed is a list and any length reads fine.
GLOBAL_HOME_ACTIVITY_COUNT = 3

# The subjects band labels one admin-chosen photograph with the things
# tagged in it. Past this many labels the row stops reading as a caption of
# the picture and starts reading as a list, so the rest are folded into a
# link to the photograph's own page rather than dropped silently.
HOME_SUBJECT_LABEL_LIMIT = 10


def get_top_rated_image():
    """Get the top-rated image for Open Graph metadata, with caching."""
    cached = cache.get("top_rated_image")
    if cached is not None:
        # "" is the sentinel for a cached *absence*. Caching None outright
        # is indistinguishable from a cache miss, so a site with no ratings
        # yet re-ran the query below on every single request.
        return cached or None

    top_rated_entry = (
        TopRatedImageView.objects.all()
        .order_by("-sort_value", "-avg_rating", "-vote_count", "image_id")
        .first()
    )
    top_rated_image = None
    if top_rated_entry:
        top_rated_image = Image.objects.select_related("collection__source").get(
            id=top_rated_entry.image_id
        )

    cache.set("top_rated_image", top_rated_image or "", PICTURE_CACHE_SECONDS)
    return top_rated_image


def _hero_pool():
    """Ids of every photograph the hero may lead with, cached.

    A photograph qualifies when the community has rated it HERO_MIN_STARS
    or better on average — the homepage's opening image should be one
    people have already vouched for — and when it can actually play the
    part: public, not a duplicate, carrying a point georeference (an
    aerial's is a polygon covering ground, not a spot a photographer stood
    on, which is the whole demonstration), and with a thumbnail, since the
    hero is the page's largest paint and renders nothing else.

    The rating threshold is a HAVING over the ratings table alone, so
    unrated photographs — the vast majority — never join the aggregate.
    """
    cached = cache.get(HERO_POOL_CACHE_KEY)
    if cached is not None:
        return cached

    highly_rated = (
        ImageRating.objects.values("image_id")
        .annotate(avg_rating=Avg("rating"))
        .filter(avg_rating__gte=HERO_MIN_STARS * 2)
        .values("image_id")
    )
    pool = list(
        Image.objects.filter(
            id__in=highly_rated,
            collection__public=True,
            collection__source__public=True,
            duplicate_of__isnull=True,
            aerial=False,
        )
        # Exists(), not georeferences__isnull=False: the reverse FK is
        # multi-valued, so that filter would yield a row per georeference.
        .filter(Exists(Georeference.objects.filter(image_id=OuterRef("id"))))
        .exclude(thumbnail__isnull=True)
        .exclude(thumbnail="")
        .values_list("id", flat=True)
    )

    cache.set(
        HERO_POOL_CACHE_KEY,
        pool,
        PICTURE_CACHE_SECONDS if pool else POOL_MISS_CACHE_SECONDS,
    )
    return pool


def _draw_hero_image():
    """A random photograph from the hero pool, loaded, or None.

    Drawn per request rather than cached, so the homepage leads with a
    different photograph on every visit. A pooled id may have stopped
    qualifying since the pool was built, so the draw re-checks what it can
    cheaply and retries; a stale rating is tolerated for the pool's
    lifetime, since it's the one thing the retry can't see without
    re-running the aggregate.
    """
    pool = list(_hero_pool())

    for _ in range(POOL_DRAW_ATTEMPTS):
        if not pool:
            return None

        image_id = random.choice(pool)
        image = (
            Image.objects.filter(
                id=image_id,
                collection__public=True,
                collection__source__public=True,
                duplicate_of__isnull=True,
                aerial=False,
            )
            .exclude(thumbnail__isnull=True)
            .exclude(thumbnail="")
            .select_related(
                "collection__source__region", "collection__region", "region"
            )
            .first()
        )
        if image is not None and image.get_georeference() is not None:
            return image

        # Only this draw's copy of the pool: rewriting the cached one would
        # renew its lifetime, and it's per-process anyway.
        pool = [entry for entry in pool if entry != image_id]

    return None


def _fallback_hero_image():
    """Any public, georeferenced photograph, for a site whose hero pool is empty.

    Prefers the sitewide top-rated image, which is already cached for the
    Open Graph tags, and otherwise takes the most recently georeferenced
    photograph — also the most alive thing on the site.
    """
    top_rated = get_top_rated_image()
    if (
        top_rated is not None
        and top_rated.thumbnail
        and not top_rated.aerial
        and top_rated.duplicate_of_id is None
        and top_rated.collection.public
        and top_rated.collection.source.public
        and top_rated.get_georeference() is not None
    ):
        return top_rated

    recent = (
        Georeference.objects.filter(
            image__collection__public=True,
            image__collection__source__public=True,
            image__duplicate_of__isnull=True,
            image__aerial=False,
        )
        .exclude(image__thumbnail__isnull=True)
        .exclude(image__thumbnail="")
        .select_related("image__collection__source", "image__region")
        .order_by("-georeferenced_at")
        .first()
    )
    return recent.image if recent else None


def get_hero_feature():
    """The photograph the global homepage leads with, and where it was taken.

    A landing page for a georeferencing project should *show* a
    georeference rather than define one: a real photograph overlapping a
    map of the point where its camera stood. Everything the figure and its
    map need is flattened into one JSON-serializable dict — the same shape
    get_region_summaries returns, for the same reason: the template reads
    it, the map reads a few keys off data attributes, and neither has to
    know which of the candidates below supplied the photograph.

    Ordinarily a fresh random draw from the highly rated pool, so no two
    visits need lead with the same picture. A site where nothing has been
    rated that highly yet falls back to a single dependable photograph;
    None when nothing qualifies at all (a fresh deployment, or a site with
    no georeferences yet), and the hero then renders as copy and buttons
    alone.
    """
    image = _draw_hero_image() or _fallback_hero_image()
    georeference = image.get_georeference() if image else None
    if georeference is None:
        return None

    region = image.effective_region
    return {
        "title": image.title,
        "url": image.get_absolute_url(),
        "thumbnail": image.thumbnail,
        "source_name": image.collection.source.name,
        "source_url": image.collection.source.get_absolute_url(),
        "region_long_name": region.long_name if region else None,
        "lng": georeference.point.x,
        "lat": georeference.point.y,
        "direction": georeference.direction,
    }


def get_subjects_feature(site_settings):
    """The photograph the subjects band labels, and what is tagged in it.

    Admin-chosen (SiteSettings.home_subjects_image) rather than drawn from
    a pool like the hero and the invitation: the band works by having its
    labels read as a caption of one frame, and whether a given photograph
    reads that way — enough subjects to be interesting, few enough to take
    in at a glance — is a judgement no query makes.

    Flattened into one JSON-serializable dict for the same reason the hero
    and the region cards are: the template reads keys, not models. None —
    and the band renders not at all — when no photograph is chosen, when
    the chosen one has since been made private, marked a duplicate, or lost
    its thumbnail, and when it carries no subjects to label it with.

    Deliberately uncached, unlike the pools above: this is two indexed
    lookups rather than an aggregate or an ORDER BY ?, and a band whose
    whole content is curated should follow the curation without a lag of
    its own. The settings row it reads is already cached for five minutes
    (SiteSettings.load), which is the only delay an admin sees.
    """
    image = (
        Image.objects.filter(
            id=site_settings.home_subjects_image_id,
            collection__public=True,
            collection__source__public=True,
            duplicate_of__isnull=True,
        )
        .exclude(thumbnail__isnull=True)
        .exclude(thumbnail="")
        .select_related("collection__source")
        .first()
    )
    if image is None:
        return None

    # The order curators put them in on the image page, which is the order
    # the labels should read in here too.
    mappings = (
        image.subject_mappings.select_related("subject")
        .order_by("order", "subject__title")
        .all()
    )
    subjects = [
        {
            "title": mapping.subject.title,
            "url": mapping.subject.get_absolute_url(),
        }
        for mapping in mappings
    ]
    if not subjects:
        return None

    # Handed over in the two halves the band arranges around the picture —
    # a column either side of it where the page is wide enough, one after
    # the other beneath it where it isn't, and in this order both ways. The
    # first half takes the odd one out, because the second also carries the
    # overflow link when there is one.
    shown = subjects[:HOME_SUBJECT_LABEL_LIMIT]
    split = (len(shown) + 1) // 2
    return {
        "title": image.title,
        "url": image.get_absolute_url(),
        "thumbnail": image.thumbnail,
        "source_name": image.collection.source.name,
        "source_url": image.collection.source.get_absolute_url(),
        "subjects_before": shown[:split],
        "subjects_after": shown[split:],
        "extra_subject_count": max(0, len(subjects) - HOME_SUBJECT_LABEL_LIMIT),
    }


def home(request):
    """Home page view.

    Visitors who haven't picked a region in the navbar selector get the
    global homepage (the region picker plus the sitewide activity feed);
    everyone else gets the per-region homepage. Both share the feed
    settings, while image-backed activity follows the selected region.
    """
    region = get_current_region(request)
    site_settings = SiteSettings.load()

    if region is None:
        return render(
            request,
            "home_no_region.html",
            {
                "page_title": "Home",
                "hero_feature": get_hero_feature(),
                # Read straight through, never cached: the stats tables are
                # signal-maintained precisely so these numbers carry no lag.
                "overall_stats": get_overall_stats(),
                "top_rated_image": get_top_rated_image(),  # og:image fallback
                "region_summaries": get_region_summaries(),
                "subjects_feature": get_subjects_feature(site_settings),
                "activity_events": get_activity_events(
                    limit=GLOBAL_HOME_ACTIVITY_COUNT,
                    event_types=site_settings.home_feed_event_types,
                ),
            },
        )

    context = {
        "page_title": "Home",
        "top_rated_image": get_top_rated_image(),
        "featured_entry": ImageOfTheDay.current_or_most_recent(region),
        "activity_events": get_activity_events(
            limit=site_settings.home_feed_item_count,
            event_types=site_settings.home_feed_event_types,
            region=region,
        ),
    }
    return render(request, "home.html", context)


def map(request):
    """Map page view"""
    context = {
        "page_title": "Map",
        "top_rated_image": get_top_rated_image(),
    }
    return render(request, "map.html", context)


def stats(request):
    """Stats page view.

    Follows the navbar region selector: with a region chosen, every figure
    on the page — the tiles, both charts, and the contributor table — covers
    images of that region and the regions inside it; with no region ("Global")
    the page is sitewide, as it always was.
    """
    region = get_current_region(request)

    # Georeference-derived numbers (the cumulative chart, the contributor
    # table) can't come from the denormalized stats tables, which hold no
    # per-day or per-user dimension, so they reach the region through each
    # georeference's image. Restricting to public collections and sources
    # keeps them describing the same set of images as the tiles and pie,
    # which read the stats tables through that same visibility filter.
    public_images = Image.objects.filter(
        collection__public=True, collection__source__public=True
    )
    if region is not None:
        public_images = public_images.in_region(region)
    image_ids = public_images.values("pk")
    georeferences = Georeference.objects.filter(image_id__in=image_ids)
    aerial_georeferences = AerialGeoreference.objects.filter(image_id__in=image_ids)
    validations = GeoreferenceValidation.objects.filter(
        georeference__image_id__in=image_ids
    )

    # Daily georeferences (cumulative) - include both point and aerial georeferences
    point_daily = (
        georeferences.annotate(day=TruncDate("georeferenced_at"))
        .values("day")
        .annotate(count=Count("id"))
    )

    aerial_daily = (
        aerial_georeferences.annotate(day=TruncDate("georeferenced_at"))
        .values("day")
        .annotate(count=Count("id"))
    )

    # Merge daily counts from both types
    daily_counts_by_date = {}
    for entry in point_daily:
        daily_counts_by_date[entry["day"]] = entry["count"]
    for entry in aerial_daily:
        if entry["day"] in daily_counts_by_date:
            daily_counts_by_date[entry["day"]] += entry["count"]
        else:
            daily_counts_by_date[entry["day"]] = entry["count"]

    cumulative_data = []
    cumulative_count = 0
    for day in sorted(daily_counts_by_date.keys()):
        cumulative_count += daily_counts_by_date[day]
        cumulative_data.append({"date": day.isoformat(), "count": cumulative_count})

    daily_labels = [entry["date"] for entry in cumulative_data]
    daily_counts = [entry["count"] for entry in cumulative_data]

    # Image status pie chart
    breakdown = get_confidence_breakdown(region)

    status_labels = [
        "Not Georeferenced",
        "Low Confidence",
        "Medium Confidence",
        "High Confidence",
    ]
    status_counts = [
        breakdown["not_georeferenced"],
        breakdown["low"],
        breakdown["medium"],
        breakdown["high"],
    ]

    # Top contributors - aggregate georeferences and validations by username
    # Include both point georeferences and aerial georeferences
    point_georeference_contributors = (
        georeferences.values(username=F("georeferenced_by__first_name"))
        .annotate(
            georeference_count=Count("id"), last_georeference=Max("georeferenced_at")
        )
        .order_by("-georeference_count", "last_georeference")
    )

    aerial_georeference_contributors = (
        aerial_georeferences.values(username=F("georeferenced_by__first_name"))
        .annotate(
            georeference_count=Count("id"), last_georeference=Max("georeferenced_at")
        )
        .order_by("-georeference_count", "last_georeference")
    )

    validation_contributors = (
        validations.values(username=F("validated_by__first_name"))
        .annotate(validation_count=Count("id"))
        .order_by("-validation_count")
    )

    # Merge results with proper handling of anonymous users
    contributors = {}
    for entry in point_georeference_contributors:
        username = entry["username"] if entry["username"] else "Anonymous"
        contributors[username] = {
            "georeferences": entry["georeference_count"],
            "validations": 0,
            "last_georeference": entry["last_georeference"],
        }

    for entry in aerial_georeference_contributors:
        username = entry["username"] if entry["username"] else "Anonymous"
        if username in contributors:
            contributors[username]["georeferences"] += entry["georeference_count"]
            # Update last_georeference if aerial is more recent
            if entry["last_georeference"]:
                existing = contributors[username]["last_georeference"]
                if not existing or entry["last_georeference"] > existing:
                    contributors[username]["last_georeference"] = entry[
                        "last_georeference"
                    ]
        else:
            contributors[username] = {
                "georeferences": entry["georeference_count"],
                "validations": 0,
                "last_georeference": entry["last_georeference"],
            }

    for entry in validation_contributors:
        username = entry["username"] if entry["username"] else "Anonymous"
        if username in contributors:
            contributors[username]["validations"] = entry["validation_count"]
        else:
            contributors[username] = {
                "georeferences": 0,
                "validations": entry["validation_count"],
                "last_georeference": None,
            }

    sorted_contributors = sorted(
        contributors.items(),
        key=lambda item: (
            -item[1]["georeferences"],  # Primary: georeferences descending
            item[1]["last_georeference"]
            or "",  # Secondary: oldest first (None becomes empty string, sorts first)
        ),
    )

    # Get overall statistics using shared utility function
    overall_stats = get_overall_stats(region)

    context = {
        "page_title": "Stats",
        "daily_labels": json.dumps(daily_labels),
        "daily_counts": json.dumps(daily_counts),
        "status_labels": json.dumps(status_labels),
        "status_counts": json.dumps(status_counts),
        "contributors": sorted_contributors,
        "overall_stats": overall_stats,
    }
    return render(request, "stats.html", context)


@require_GET
def robots_txt(request):
    """Serve robots.txt"""
    lines = [
        "User-agent: *",
        "Disallow: /search/",
        "Disallow: */similar/",
        "Disallow: /api/",
        "Disallow: */georeference/*",
        "Disallow: */polygonal-georeference/*",
    ]
    return HttpResponse("\n".join(lines), content_type="text/plain")


@require_GET
def health_ready(request):
    """
    Kubernetes readiness probe endpoint.
    Returns 200 if the app is ready to serve traffic.
    """
    checks = {
        "database": False,
    }

    # Check database connectivity
    try:
        connection.ensure_connection()
        checks["database"] = True
    except Exception:
        pass

    all_ready = all(checks.values())
    status = 200 if all_ready else 503

    return JsonResponse({"ready": all_ready, "checks": checks}, status=status)
