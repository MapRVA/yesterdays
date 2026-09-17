from urllib.parse import urlencode

from django.contrib.gis.geos import Point
from django.core.paginator import Page, Paginator
from django.db.models import (
    Avg,
    Case,
    Count,
    F,
    IntegerField,
    Q,
    Sum,
    Value,
    When,
)
from django.shortcuts import get_object_or_404, render
from django.urls import reverse

from regions.context_processors import get_current_region
from regions.models import Region

from ..models import (
    Collection,
    CollectionRegionStats,
    Image,
    ImageRating,
    Source,
    TopRatedImageView,
)
from ..utils import _public_collection_stats, get_overall_stats, render_markdown_safe


def apply_image_filters(request, queryset):
    """
    Apply standard filters from filter_cards.html to a queryset.

    This is a unified helper function used across all views that include
    the filter_cards.html partial.

    Supports:
    - georeference_status: georeferenced, pending, will_not_georef
    - start_year, end_year: year range filtering
    - with_subjects: images that have ALL specified subjects
    - without_subjects: images that don't have ANY specified subjects
    - no_subjects: images with no subjects at all
    """
    # Get filter parameters from URL
    georeference_status = (
        request.GET.get("georeference_status", "").split(",")
        if request.GET.get("georeference_status")
        else []
    )
    start_year = request.GET.get("start_year")
    end_year = request.GET.get("end_year")
    with_subjects = (
        request.GET.get("with_subjects", "").split(",")
        if request.GET.get("with_subjects")
        else []
    )
    without_subjects = (
        request.GET.get("without_subjects", "").split(",")
        if request.GET.get("without_subjects")
        else []
    )
    no_subjects = request.GET.get("no_subjects") == "true"

    # Apply year filtering
    if start_year:
        try:
            start_year_int = int(start_year)
            queryset = queryset.filter(
                Q(fuzzy_start_decdate__gte=start_year_int)
                | Q(start_decdate__gte=start_year_int)
            )
        except ValueError:
            pass

    if end_year:
        try:
            end_year_int = int(end_year)
            queryset = queryset.filter(
                Q(fuzzy_end_decdate__lte=end_year_int)
                | Q(end_decdate__lte=end_year_int)
            )
        except ValueError:
            pass

    # Apply subject filtering
    if no_subjects:
        queryset = queryset.filter(subjects__isnull=True)
    elif with_subjects:
        # Include only images with ALL of these subjects
        for subject_id in with_subjects:
            if subject_id:
                queryset = queryset.filter(subjects__id=subject_id)
    elif without_subjects:
        # Exclude images with ANY of these subjects
        queryset = queryset.exclude(subjects__id__in=without_subjects)

    # Apply georeference status filtering
    if georeference_status:
        # Build the filter conditions based on selected statuses
        filter_conditions = Q()

        if "georeferenced" in georeference_status:
            filter_conditions |= Q(aerial=False, georeferences__isnull=False) | Q(
                aerial=True, aerial_georeferences__isnull=False
            )

        if "pending" in georeference_status:
            filter_conditions |= (
                Q(georeferences__isnull=True)
                & Q(aerial=False)
                & Q(will_not_georef=False)
            ) | (
                Q(aerial_georeferences__isnull=True)
                & Q(aerial=True)
                & Q(will_not_georef=False)
            )

        if "will_not_georef" in georeference_status:
            filter_conditions |= Q(will_not_georef=True)

        # Apply the filter if any conditions were added
        if filter_conditions:
            queryset = queryset.filter(filter_conditions)

    return queryset


def _browse_sources_stats(region=None):
    """Read per-source and overall counts, optionally scoped to a region.

    The denormalized stats table is maintained eagerly by signal handlers,
    so these are cheap sums over a few hundred rows at most.
    """
    rows = _public_collection_stats(region)
    if region is not None:
        rows = rows.filter(total_images__gt=0)
    per_source = (
        rows.values("collection__source")
        .annotate(
            collections=Count("collection"),
            total=Sum("total_images"),
            georeferenced=Sum(
                F("georeferenced_low")
                + F("georeferenced_medium")
                + F("georeferenced_high")
            ),
            will_not_georef=Sum("will_not_georef_images"),
        )
    )
    return {
        "by_source": {row["collection__source"]: row for row in per_source},
        "overall": get_overall_stats(region),
    }


def browse_sources(request):
    """Browse public sources, scoped to the navbar region when selected."""
    total_source_count = Source.objects.filter(public=True).count()
    sources = (
        Source.objects.filter(public=True)
        .annotate(
            public_collections_count=Count(
                "collections", filter=Q(collections__public=True)
            )
        )
        .order_by("name")
    )

    region = get_current_region(request)
    stats = _browse_sources_stats(region)
    if region is not None:
        sources = sources.filter(pk__in=stats["by_source"])

    for source in sources:
        row = stats["by_source"].get(source.id)
        if region is not None:
            source.public_collections_count = row["collections"]
        source.total_images = row["total"] if row else 0
        source.georeferenced_images = row["georeferenced"] if row else 0
        source.will_not_georef_images = row["will_not_georef"] if row else 0
        source.pending_images = (
            source.total_images
            - source.georeferenced_images
            - source.will_not_georef_images
        )

    overall_stats = stats["overall"]

    top_rated_entries = TopRatedImageView.objects.all()
    if region is not None:
        top_rated_entries = top_rated_entries.filter(
            image_id__in=Image.objects.in_region(region).values("pk")
        )
    top_rated_entry = (
        top_rated_entries.order_by("-sort_value", "-avg_rating", "-vote_count", "image_id")
        .first()
    )
    top_rated_image = None
    if top_rated_entry:
        top_rated_image = Image.objects.select_related("collection__source").get(
            id=top_rated_entry.image_id
        )

    context = {
        "sources": sources,
        "browse_region": region,
        "total_source_count": total_source_count,
        "overall_stats": overall_stats,
        "top_rated_image": top_rated_image,
    }
    return render(request, "images/browse_sources.html", context)


def source_detail(request, slug):
    """Public collections and counts in the selected region, or a global fallback."""
    source = get_object_or_404(Source, slug=slug, public=True)
    collections = list(source.collections.filter(public=True).select_related("stats"))
    total_collection_count = len(collections)
    region = get_current_region(request)
    regional_stats = {}
    if region is not None:
        regional_stats = {
            row.collection_id: row
            for row in CollectionRegionStats.objects.filter(
                region=region,
                collection__source=source,
                collection__public=True,
                total_images__gt=0,
            )
        }
    region_fallback = region is not None and not regional_stats
    if region_fallback:
        region = None
    if region is not None:
        collections = [c for c in collections if c.pk in regional_stats]

    # Attach per-collection statistics from the denormalized stats table, and
    # sum them for the source-level totals
    total_images = 0
    georeferenced_images = 0
    will_not_georef_images = 0
    for collection in collections:
        stats = (
            regional_stats[collection.pk]
            if region is not None
            else getattr(collection, "stats", None)
        )
        collection.total_images = stats.total_images if stats else 0
        collection.georeferenced_images = stats.georeferenced_images if stats else 0
        collection.will_not_georef_images = stats.will_not_georef_images if stats else 0
        collection.pending_images = stats.pending_images if stats else 0
        total_images += collection.total_images
        georeferenced_images += collection.georeferenced_images
        will_not_georef_images += collection.will_not_georef_images

    # Stable sort retains alphabetical ordering for equal regional counts.
    if region is not None:
        collections.sort(key=lambda collection: -collection.total_images)

    preview_images = Image.objects.filter(
        collection__source=source, collection__public=True
    )
    if region is not None:
        preview_images = preview_images.in_region(region)
    top_rated_entry = (
        TopRatedImageView.objects.filter(image_id__in=preview_images.values("pk"))
        .order_by("-sort_value", "-avg_rating", "-vote_count", "image_id")
        .first()
    )
    top_rated_image = None
    if top_rated_entry:
        top_rated_image = Image.objects.select_related("collection__source").get(
            id=top_rated_entry.image_id
        )

    # Render markdown description
    rendered_description = None
    if source.description:
        rendered_description = render_markdown_safe(source.description)

    context = {
        "source": source,
        "collections": collections,
        "browse_region": region,
        "region_fallback": region_fallback,
        # `collections` is a list, so the template can't call .count on it
        "collection_count": len(collections),
        "total_collection_count": total_collection_count,
        "total_images": total_images,
        "georeferenced_images": georeferenced_images,
        "pending_images": total_images - georeferenced_images - will_not_georef_images,
        "will_not_georef_images": will_not_georef_images,
        "completion_percentage": (georeferenced_images / total_images * 100)
        if total_images > 0
        else 0,
        "top_rated_image": top_rated_image,
        "rendered_description": rendered_description,
    }
    return render(request, "images/source_detail.html", context)


def collection_detail(request, source_slug, collection_slug):
    """Detail view for a specific public collection"""
    source = get_object_or_404(Source, slug=source_slug, public=True)
    collection = get_object_or_404(
        Collection.objects.select_related("stats"),
        source=source,
        slug=collection_slug,
        public=True,
    )

    region = get_current_region(request)
    stats = getattr(collection, "stats", None)
    total_collection_images = stats.total_images if stats else 0
    region_fallback = False
    if region is not None:
        regional_stats = CollectionRegionStats.objects.filter(
            collection=collection, region=region, total_images__gt=0
        ).first()
        if regional_stats is None:
            region_fallback = True
            region = None
        else:
            stats = regional_stats

    scoped_images = collection.images.filter(duplicate_of__isnull=True)
    if region is not None:
        scoped_images = scoped_images.in_region(region)

    # Sort images: georeferenced images second-to-last, "will not reference" images at the end
    # Exclude duplicate images from the collection view
    images = (
        scoped_images.prefetch_related("subjects")
        .annotate(
            has_georeference=Case(
                When(
                    Q(aerial=False, georeferences__isnull=False)
                    | Q(aerial=True, aerial_georeferences__isnull=False),
                    then=Value(1),
                ),
                default=Value(0),
                output_field=IntegerField(),
            )
        )
        .order_by("will_not_georef", "has_georeference", "id")
    )

    images = apply_image_filters(request, images)
    # Statistics describe the regional collection before grid filters.
    total_images = stats.total_images if stats else 0
    georeferenced_images = stats.georeferenced_images if stats else 0
    will_not_georef_images = stats.will_not_georef_images if stats else 0

    # Paginate the filtered images for browsing
    paginator = Paginator(images.distinct(), 24)  # 24 images per page for grid layout
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    # Get top-rated image for Open Graph metadata
    top_rated_entry = (
        TopRatedImageView.objects.filter(
            image_id__in=(
                scoped_images if region is not None else collection.images.all()
            ).values("pk")
        )
        .order_by("-sort_value", "-avg_rating", "-vote_count", "image_id")
        .first()
    )
    top_rated_image = None
    if top_rated_entry:
        top_rated_image = Image.objects.select_related("collection__source").get(
            id=top_rated_entry.image_id
        )

    # Render markdown description
    rendered_description = None
    if collection.description:
        rendered_description = render_markdown_safe(collection.description)

    georeference_params = {"source": source.slug, "collection": collection.slug}
    if region is not None:
        georeference_params["region"] = region.wikidata_item.wikidata_id
    georeference_url = (
        reverse("images:georeference_interface") + "?" + urlencode(georeference_params)
    )

    context = {
        "source": source,
        "collection": collection,
        "browse_region": region,
        "region_fallback": region_fallback,
        "georeference_url": georeference_url,
        "page_obj": page_obj,
        "total_images": total_images,
        "total_collection_images": total_collection_images,
        "georeferenced_images": georeferenced_images,
        "pending_images": total_images - georeferenced_images - will_not_georef_images,
        "will_not_georef_images": will_not_georef_images,
        "completion_percentage": (
            georeferenced_images / (total_images - will_not_georef_images) * 100
        )
        if (total_images - will_not_georef_images) > 0
        else 0,
        "top_rated_image": top_rated_image,
        "rendered_description": rendered_description,
    }
    return render(request, "images/collection_detail.html", context)


def image_list(request):
    """List all images with filtering options"""
    images = (
        Image.objects.filter(collection__public=True, collection__source__public=True)
        .select_related("collection__source")
        .prefetch_related("georeferences")
    )

    # Filter by georeferencing status
    status = request.GET.get("status")
    if status == "pending":
        images = images.filter(georeferences__isnull=True, will_not_georef=False)
    elif status == "georeferenced":
        images = images.filter(georeferences__isnull=False).distinct()
    elif status == "will_not_georef":
        images = images.filter(will_not_georef=True)

    # Filter by difficulty
    difficulty = request.GET.get("difficulty")
    if difficulty in ["easy", "medium", "hard"]:
        images = images.filter(difficulty=difficulty)

    # Filter by collection
    collection_id = request.GET.get("collection")
    if collection_id:
        images = images.filter(collection_id=collection_id)

    # Pagination
    paginator = Paginator(images, 20)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    context = {
        "page_obj": page_obj,
        "status": status,
        "difficulty": difficulty,
        "collection_id": collection_id,
    }

    return render(request, "images/image_list.html", context)


def image_detail(request, image_id):
    """Display detailed view of an image for georeferencing"""
    image = get_object_or_404(
        Image.objects.select_related(
            "region", "collection__region", "collection__source__region", "license"
        ),
        id=image_id,
    )

    # Get current georeferences (cached HTML is used directly in templates)
    georeference = image.get_georeference()
    polygonal_georeference = image.get_aerial_georeference() if image.aerial else None

    # Get total count of images in this collection
    total_images_in_collection = image.collection.images.count()

    # Get the position of this image in the collection (ordered by ID)
    image_position = image.collection.images.filter(id__lte=image.id).count()

    # Get rating statistics
    image_ratings = image.ratings.all()
    avg_rating = image_ratings.aggregate(Avg("rating"))["rating__avg"]
    rating_count = image_ratings.count()
    user_rating = None
    if request.user.is_authenticated:
        try:
            user_rating = ImageRating.objects.get(image=image, user=request.user).rating
        except ImageRating.DoesNotExist:
            pass

    context = {
        "image": image,
        "has_georeference": image.georeferences.exists(),
        "georeference": georeference,
        "validations": georeference.validations.all() if georeference else [],
        "polygonal_georeference": polygonal_georeference,
        "next_image": image.get_next_image(),
        "previous_image": image.get_previous_image(),
        "total_images_in_collection": total_images_in_collection,
        "image_position": image_position,
        "avg_rating": avg_rating,
        "rating_count": rating_count,
        "user_rating": user_rating,
        # Only the staff-only "Queue for Image of the Day" modal needs these,
        # so don't spend the query on everyone else. The default is only a
        # suggestion: staff may queue an image into any region's queue.
        "queue_regions": Region.objects.all() if request.user.is_staff else None,
        "queue_default_region": (
            image.effective_region if request.user.is_staff else None
        ),
    }

    return render(request, "images/image_detail.html", context)


def top_rated_images(request):
    """Display paginated list of highest-rated images, optionally filtered by source or collection"""
    page_number = request.GET.get("page", 1)
    page_size = 24  # 24 images per page

    # Get optional filters
    source_id = request.GET.get("source")
    collection_id = request.GET.get("collection")
    region = get_current_region(request)

    # Convert page number to offset/limit
    try:
        page_number = int(page_number)
        if page_number < 1:
            page_number = 1
    except (ValueError, TypeError):
        page_number = 1

    # Start with all view entries
    view_entries = TopRatedImageView.objects.all()

    # Filter by source if specified
    if source_id:
        view_entries = view_entries.filter(
            image_id__in=Image.objects.filter(
                collection__source_id=source_id
            ).values_list("id", flat=True)
        )

    # Filter by collection if specified
    if collection_id:
        view_entries = view_entries.filter(
            image_id__in=Image.objects.filter(collection_id=collection_id).values_list(
                "id", flat=True
            )
        )

    # Scope to the navbar region (the region and everything inside it).
    if region is not None:
        view_entries = view_entries.filter(
            image_id__in=Image.objects.in_region(region).values_list("id", flat=True)
        )

    # Order by rating
    view_entries = view_entries.order_by(
        "-sort_value", "-avg_rating", "-vote_count", "image_id"
    )

    # Get total count for pagination info
    total_count = view_entries.count()

    # Calculate total pages
    total_pages = (total_count + page_size - 1) // page_size

    # Validate page number
    if page_number > total_pages and total_count > 0:
        page_number = total_pages

    offset = (page_number - 1) * page_size

    # Get image IDs for current page only
    page_image_ids = list(
        view_entries[offset : offset + page_size].values_list("image_id", flat=True)
    )

    # Create a queryset for the current page only, maintaining the correct order
    if page_image_ids:
        preserved_order = Case(
            *[
                When(pk=image_id, then=pos)
                for pos, image_id in enumerate(page_image_ids)
            ]
        )
        page_images = list(
            Image.objects.filter(id__in=page_image_ids)
            .select_related("collection__source")
            .order_by(preserved_order)
        )
    else:
        page_images = []

    # Create a Django Paginator that uses our manually-paginated queryset
    # but has the correct count for all pages
    paginator = Paginator(page_images, page_size)

    # Override the count to match the total from the database view
    paginator.count = total_count

    # We already have our page data, so just need to set up the Page object
    page_obj = Page(page_images, page_number, paginator)

    # Get top-rated image for Open Graph metadata (first from the ordered list)
    top_rated_image = page_images[0] if page_images else None

    context = {
        "page_obj": page_obj,
        "top_rated_image": top_rated_image,
    }
    return render(request, "images/favorites.html", context)


def browse_aerials(request):
    """Browse all aerial images with optional location-based filtering"""

    # Start with base queryset
    aerials = (
        Image.objects.filter(
            aerial=True,
            collection__public=True,
            collection__source__public=True,
            duplicate_of__isnull=True,
        )
        .select_related("collection__source")
        .prefetch_related("subjects")
    )

    # Apply standard filters from filter_cards.html
    aerials = apply_image_filters(request, aerials)

    # Check for location filtering
    lat = request.GET.get("lat")
    lon = request.GET.get("lon")
    is_filtered = False
    filter_point = None

    if lat and lon:
        try:
            lat = float(lat)
            lon = float(lon)

            # Validate coordinates are within reasonable bounds
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                # Create a Point from the coordinates
                filter_point = Point(lon, lat)

                # Filter images that have aerial georeferences containing this point
                filtered_image_ids = []
                for aerial in aerials:
                    aerial_georeference = aerial.get_aerial_georeference()
                    if aerial_georeference and aerial_georeference.polygon.contains(
                        filter_point
                    ):
                        filtered_image_ids.append(aerial.id)

                # Apply the filter - even if empty list (this will show no results)
                aerials = aerials.filter(id__in=filtered_image_ids)
                is_filtered = True

        except (ValueError, TypeError):
            # Invalid coordinates, ignore filtering
            pass

    # The card grid follows the navbar region by default. A location click is
    # intentionally global: its point-in-polygon results should show every
    # aerial that covers the selected spot, regardless of its assigned region.
    if not is_filtered:
        region = get_current_region(request)
        if region is not None:
            aerials = aerials.in_region(region)

    # Sort filtered results by georeference area (smallest to largest) if filtered
    if is_filtered and filter_point:
        # Get aerial georeferences and sort by area
        aerial_with_areas = []
        for aerial in aerials:
            aerial_georeference = aerial.get_aerial_georeference()
            if aerial_georeference:
                # Calculate area using PostGIS
                area = aerial_georeference.polygon.area
                aerial_with_areas.append((area, aerial.id))

        # Sort by area and get ordered IDs
        aerial_with_areas.sort(key=lambda x: x[0])
        ordered_ids = [item[1] for item in aerial_with_areas]

        # Preserve the order in the queryset
        if ordered_ids:
            preserved_order = Case(
                *[When(pk=pk, then=pos) for pos, pk in enumerate(ordered_ids)]
            )
            aerials = aerials.order_by(preserved_order)
    else:
        # Default ordering for non-filtered results
        aerials = aerials.order_by("id")

    # Paginate images for browsing
    paginator = Paginator(aerials, 24)  # 24 images per page for grid layout
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    # Calculate statistics
    total_images = aerials.count()
    georeferenced_images = (
        aerials.filter(aerial_georeferences__isnull=False).distinct().count()
    )
    will_not_georef_images = aerials.filter(will_not_georef=True).count()

    # Get top-rated aerial image for Open Graph metadata
    top_rated_entry = (
        TopRatedImageView.objects.filter(
            image_id__in=Image.objects.filter(aerial=True).values_list("id", flat=True)
        )
        .order_by("-sort_value", "-avg_rating", "-vote_count", "image_id")
        .first()
    )
    top_rated_image = None
    if top_rated_entry:
        top_rated_image = Image.objects.select_related("collection__source").get(
            id=top_rated_entry.image_id
        )

    context = {
        "page_obj": page_obj,
        "total_images": total_images,
        "georeferenced_images": georeferenced_images,
        "pending_images": total_images - georeferenced_images - will_not_georef_images,
        "completion_percentage": (georeferenced_images / total_images * 100)
        if total_images > 0
        else 0,
        "is_filtered": is_filtered,
        "filter_lat": lat if is_filtered else None,
        "filter_lon": lon if is_filtered else None,
        "top_rated_image": top_rated_image,
    }
    return render(request, "images/from_above.html", context)
