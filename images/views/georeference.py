import json
import logging

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.gis.geos import Point
from django.db import IntegrityError, models, transaction
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_http_methods

from osm_auth.models import UserPreferences
from regions.context_processors import get_current_region
from subjects.models import Subject

from ..models import (
    AerialGeoreference,
    Album,
    Collection,
    Georeference,
    GeoreferenceValidation,
    Image,
    Source,
)
from ..policies import (
    editable_images_for,
    georeferenceable_images_for,
    get_image_or_404,
)
from ..validation import (
    WGS84_SRID,
    InvalidInput,
    build_polygon,
    parse_json_body,
    validate_choice,
    validate_direction,
    validate_latitude,
    validate_longitude,
    validate_text,
)

logger = logging.getLogger(__name__)

POINT_CONFIDENCE_LEVELS = [value for value, _ in Georeference.CONFIDENCE_CHOICES]
POLYGON_CONFIDENCE_LEVELS = [
    value for value, _ in AerialGeoreference.CONFIDENCE_CHOICES
]
VALIDATION_CHOICES = [
    value for value, _ in GeoreferenceValidation.VALIDATION_CHOICES
]

ANONYMOUS_ALREADY_GEOREFERENCED = (
    "This image has already been georeferenced. "
    "Please login to submit a correction."
)


def georeference_interface(request):
    """Main georeferencing interface - can be filtered by source/collection/subject/album or show specific image"""
    source_slug = request.GET.get("source")
    collection_slug = request.GET.get("collection")
    subject_slug = request.GET.get("subject")
    difficulty = request.GET.get("difficulty")
    image_id = request.GET.get("image") or request.GET.get("current_image")
    album_id = request.GET.get("album")

    # If specific image ID is requested, try to load it
    current_image = None
    if image_id:
        try:
            # For specific image requests, allow both georeferenced and ungeoreferenced images
            # This enables corrections for already georeferenced images
            # But exclude duplicate images
            # Also allow aerials if user is an admin
            query_params = {
                "id": int(image_id),
                "will_not_georef": False,
                "duplicate_of__isnull": True,
                "collection__public": True,
                "collection__source__public": True,
            }

            # Only allow aerials for admin users
            if not request.user.is_staff:
                query_params["aerial"] = False

            # query_params already excludes aerials for non-staff, so an
            # aerial reaching a non-staff caller here is impossible. The old
            # follow-up PermissionDenied check was dead code that would have
            # raised NameError (the name is never imported) rather than a 403.
            current_image = Image.objects.get(**query_params)
        except (Image.DoesNotExist, ValueError):
            # If specific image not found or invalid, fall back to random selection
            pass

    # Start with all ungeoreferenced images from public sources/collections
    # Exclude duplicate images from being suggested
    images = Image.objects.filter(
        georeferences__isnull=True,
        will_not_georef=False,
        aerial=False,
        duplicate_of__isnull=True,
        collection__public=True,
        collection__source__public=True,
    )

    # Note: We deliberately do NOT exclude skipped images here because:
    # 1. It makes the remaining count misleading (looks like fewer images need work)
    # 2. Users should be able to go back and georeference images they previously skipped
    # 3. Skip tracking is still useful for statistics, but shouldn't hide images

    images = images.select_related("collection__source")

    # The navbar region is a preference for the default random queue, not an
    # additional restriction on links that deliberately target another queue
    # or image. Difficulty is intentionally absent here: it narrows either the
    # regional default queue or an explicit queue below.
    has_explicit_scope = any(
        (
            current_image is not None,
            source_slug,
            collection_slug,
            album_id,
            subject_slug,
        )
    )
    if not has_explicit_scope:
        region = get_current_region(request)
        if region is not None:
            images = images.in_region(region)

    # Filter by album if specified
    album = None
    album_owner_display_name = None
    if album_id:
        try:
            album = Album.objects.get(id=album_id)
            # Check if album is public or if user is the owner
            if not album.public and (
                not request.user.is_authenticated or album.owner != request.user
            ):
                # Private album and user is not the owner - return 404
                raise Http404("Album not found")
            # Filter images to only those in this album
            images = images.filter(albums=album)
            # Get the album owner's display name for the breadcrumb
            album_owner_display_name = (
                album.owner.get_display_name()
                if hasattr(album.owner, "get_display_name")
                else album.owner.username
            )
        except Album.DoesNotExist:
            raise Http404("Album not found")

    # Filter by source if specified
    source = None
    if source_slug:
        source = get_object_or_404(Source, slug=source_slug, public=True)
        images = images.filter(collection__source=source, collection__public=True)

    # Filter by collection if specified
    collection = None
    if collection_slug and source:
        collection = get_object_or_404(
            Collection, source=source, slug=collection_slug, public=True
        )
        images = images.filter(collection=collection)

    # Filter by subject if specified
    subject = None
    if subject_slug:
        subject = get_object_or_404(Subject, slug=subject_slug)
        images = images.filter(subject_mappings__subject=subject)

    # Filter by difficulty if specified - can be multiple values (plus-separated)
    difficulty_param = request.GET.get("difficulty", "")
    difficulty_filters = []
    if difficulty_param:
        # Split plus-separated values and validate
        difficulty_filters = [
            d.strip()
            for d in difficulty_param.split("+")
            if d.strip() in ["easy", "medium", "hard", "unlabeled"]
        ]
        if difficulty_filters:
            # Handle unlabeled separately since it needs a different query
            regular_difficulties = [d for d in difficulty_filters if d != "unlabeled"]
            has_unlabeled = "unlabeled" in difficulty_filters

            if regular_difficulties and has_unlabeled:
                # Include both regular difficulties and unlabeled images
                images = images.filter(
                    models.Q(difficulty__in=regular_difficulties)
                    | models.Q(difficulty__isnull=True)
                )
            elif regular_difficulties:
                # Only regular difficulties
                images = images.filter(difficulty__in=regular_difficulties)
            elif has_unlabeled:
                # Only unlabeled images
                images = images.filter(difficulty__isnull=True)

    # If no specific image or it wasn't found, select randomly from filtered set
    if not current_image:
        # Get a random image for georeferencing
        current_image = images.order_by("?").first()

    # Set source and collection from the current image if not already set
    if current_image:
        if not source_slug:
            source = current_image.collection.source
        if not collection_slug:
            collection = current_image.collection

    # Build location hint data for the map
    # Priority: 1) Previous georeference (for corrections), 2) Source point (from archive metadata),
    # 3) Detected address (from address parsing)
    location_hint = None
    if current_image:
        existing_georef = current_image.get_georeference()
        if existing_georef:
            location_hint = {
                "type": "georeference",
                "lat": existing_georef.point.y,
                "lng": existing_georef.point.x,
                "direction": existing_georef.direction,
                "label": "Previous Georeference",
            }
        elif current_image.source_point:
            location_hint = {
                "type": "source",
                "lat": current_image.source_point.y,
                "lng": current_image.source_point.x,
                "direction": None,
                "label": "Hint From Source",
            }
        elif current_image.detected_address:
            location_hint = {
                "type": "detected",
                "lat": current_image.detected_address.y,
                "lng": current_image.detected_address.x,
                "direction": None,
                "label": "Detected Address",
            }

    # Build subject hints - always shown, rendered below other hints
    # These come from subjects linked to the image that have OSM elements with centroids
    subject_hints = []
    if current_image:
        subject_mappings = current_image.subject_mappings.prefetch_related(
            "subject__osm_elements"
        ).order_by("order")
        for mapping in subject_mappings:
            subject = mapping.subject
            # Use the first OSM element with a centroid
            osm_element = subject.osm_elements.filter(centroid__isnull=False).first()
            if osm_element:
                subject_hints.append(
                    {
                        "lat": osm_element.centroid.y,
                        "lng": osm_element.centroid.x,
                        "label": subject.title,
                    }
                )

    if request.user.is_authenticated:
        user_preferences, _ = UserPreferences.objects.get_or_create(user=request.user)
    else:
        user_preferences = UserPreferences()

    context = {
        "current_image": current_image,
        "source": source,
        "collection": collection,
        "subject": subject,
        "album": album,
        "album_owner_display_name": album_owner_display_name,
        "difficulty_filters": difficulty_filters,
        "difficulty_filters_json": json.dumps(difficulty_filters),
        "remaining_count": images.count(),
        "next_image": current_image.get_next_image() if current_image else None,
        "previous_image": current_image.get_previous_image() if current_image else None,
        "location_hint": location_hint,
        "location_hint_json": json.dumps(location_hint),
        "subject_hints": subject_hints,
        "subject_hints_json": json.dumps(subject_hints),
        "user_preferences": user_preferences,
    }

    # Remove duplicate message - template already shows appropriate message when no image available

    return render(request, "images/georeference_interface.html", context)


@require_http_methods(["POST"])
def georeference_image(request, image_id):
    """API endpoint to georeference an image"""
    # Authorization runs first and outside every broad exception handler, so an
    # image the caller may not georeference answers 404 exactly as a missing
    # one does — no 400, no 500, and nothing that distinguishes "private" from
    # "never existed".
    policy = georeferenceable_images_for(request.user, aerial=False)
    get_image_or_404(policy, image_id)

    try:
        data = parse_json_body(request)
        longitude = validate_longitude(data)
        latitude = validate_latitude(data)
        direction = validate_direction(data)
        confidence = validate_choice(data, "confidence", POINT_CONFIDENCE_LEVELS)
        notes = validate_text(
            data, "notes", max_length=settings.GEOREFERENCE_NOTES_MAX_LENGTH
        )
        if confidence == "low" and not notes:
            raise InvalidInput("Low confidence requires explanatory notes")
    except InvalidInput as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=400)

    try:
        with transaction.atomic():
            # Lock the image row for the whole decision. Anonymous callers may
            # only place the *first* georeference on an image; without the lock
            # two simultaneous anonymous requests both see an empty set and
            # both write. Authenticated submissions take the same lock so one
            # cannot slip between an anonymous check and its insert.
            image = get_image_or_404(policy.select_for_update(of=("self",)), image_id)

            if not request.user.is_authenticated and image.georeferences.exists():
                return JsonResponse(
                    {"success": False, "error": ANONYMOUS_ALREADY_GEOREFERENCED},
                    status=400,
                )

            # Submissions are append-only. The model deliberately allows a user
            # several georeferences per image so corrections keep the full
            # history; there is nothing here to update in place.
            georeference = Georeference.objects.create(
                image=image,
                point=Point(longitude, latitude, srid=WGS84_SRID),
                direction=direction,
                confidence=confidence,
                georeferenced_by=(
                    request.user if request.user.is_authenticated else None
                ),
                confidence_notes=notes,
            )
    except Http404:
        raise
    except IntegrityError:
        # The partial unique index on anonymous georeferences is the second
        # line of defence behind the row lock; losing that race means somebody
        # else placed the first georeference.
        return JsonResponse(
            {"success": False, "error": ANONYMOUS_ALREADY_GEOREFERENCED},
            status=400,
        )
    except Exception:
        logger.exception(
            "Point georeference failed for image %s (user %s)",
            image_id,
            request.user.pk if request.user.is_authenticated else "anonymous",
        )
        return JsonResponse(
            {"success": False, "error": "Unable to save this georeference."},
            status=500,
        )

    return JsonResponse(
        {
            "success": True,
            "georeference_id": georeference.id,
            "message": "Image successfully georeferenced",
        }
    )


@login_required
def aerial_georeference_interface(request, image_id):
    """Display the aerial georeference interface for a specific image"""
    # Same policy the submission endpoint enforces, so the page and the POST
    # agree about which images are eligible.
    try:
        image = get_image_or_404(
            georeferenceable_images_for(request.user, aerial=True), image_id
        )
    except Http404:
        return render(
            request,
            "images/from_above_georeference_interface.html",
            {"image": None},
            status=404,
        )

    # Get the existing aerial georeference if it exists
    aerial_georeference = image.get_aerial_georeference()

    context = {
        "image": image,
        "aerial_georeference": aerial_georeference,
        "user": request.user,
    }

    return render(request, "images/from_above_georeference_interface.html", context)


@require_http_methods(["POST"])
def aerial_georeference_image(request, image_id):
    """API endpoint to submit an aerial georeference with polygon"""
    if not request.user.is_authenticated:
        return JsonResponse(
            {"success": False, "error": "Authentication required"}, status=401
        )

    # Aerial-only for everyone, staff included: a polygon georeference is
    # meaningless on a ground-level photograph.
    policy = georeferenceable_images_for(request.user, aerial=True)
    get_image_or_404(policy, image_id)

    try:
        data = parse_json_body(request, max_bytes=settings.POLYGON_MAX_BODY_BYTES)
        if "polygon" not in data:
            raise InvalidInput("Missing required field: polygon")
        # Structural limits (ring count, vertex counts, coordinate bounds) are
        # applied to the raw GeoJSON before GEOS or the database sees it.
        polygon = build_polygon(data["polygon"])
        confidence = validate_choice(data, "confidence", POLYGON_CONFIDENCE_LEVELS)
        notes = validate_text(
            data, "notes", max_length=settings.GEOREFERENCE_NOTES_MAX_LENGTH
        )
        if confidence == "low" and not notes:
            raise InvalidInput("Low confidence requires explanatory notes")
    except InvalidInput as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=400)

    try:
        with transaction.atomic():
            # Same row lock as the point path, so the two submission routes
            # serialize against each other on a given image.
            image = get_image_or_404(policy.select_for_update(of=("self",)), image_id)

            # Append-only, matching the point path: corrections add a row.
            aerial_georeference = AerialGeoreference.objects.create(
                image=image,
                polygon=polygon,
                confidence=confidence,
                georeferenced_by=request.user,
                confidence_notes=notes,
            )
    except Http404:
        raise
    except Exception:
        logger.exception(
            "Polygon georeference failed for image %s (user %s)",
            image_id,
            request.user.pk,
        )
        return JsonResponse(
            {"success": False, "error": "Unable to save this georeference."},
            status=500,
        )

    return JsonResponse(
        {
            "success": True,
            "georeference_id": aerial_georeference.id,
            "message": "Polygonal georeference successfully created",
        }
    )


@require_http_methods(["POST"])
def validate_georeference(request, georeference_id):
    """API endpoint to validate a georeference"""
    if not request.user.is_authenticated:
        return JsonResponse(
            {"success": False, "error": "Authentication required"}, status=401
        )

    # Resolve the georeference through its image, so a vote can never be
    # attached to something the caller cannot see. editable_images_for rather
    # than georeferenceable_images_for: the staff validation queue serves
    # georeferences whose image may since have been flagged will_not_georef,
    # and voting on that existing history stays legitimate.
    try:
        georeference = Georeference.objects.select_related("image").get(
            id=georeference_id,
            image__in=editable_images_for(request.user),
        )
    except (Georeference.DoesNotExist, TypeError, ValueError):
        raise Http404("Georeference not found")

    try:
        data = parse_json_body(request)
        validation_choice = validate_choice(data, "validation", VALIDATION_CHOICES)
        notes = validate_text(
            data, "notes", max_length=settings.VALIDATION_NOTES_MAX_LENGTH
        )
    except InvalidInput as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=400)

    if georeference.georeferenced_by_id == request.user.pk:
        return JsonResponse(
            {"success": False, "error": "Cannot validate your own georeference"},
            status=400,
        )

    try:
        with transaction.atomic():
            validation = GeoreferenceValidation.objects.create(
                georeference=georeference,
                validated_by=request.user,
                validation=validation_choice,
                notes=notes,
            )
    except IntegrityError:
        # unique_together (georeference, validated_by). Catching the constraint
        # rather than pre-checking with exists() covers the concurrent
        # double-submit that used to surface as a 500.
        return JsonResponse(
            {
                "success": False,
                "error": "You have already validated this georeference",
            },
            status=400,
        )
    except Exception:
        logger.exception(
            "Validation failed for georeference %s (user %s)",
            georeference_id,
            request.user.pk,
        )
        return JsonResponse(
            {"success": False, "error": "Unable to record this validation."},
            status=500,
        )

    return JsonResponse(
        {
            "success": True,
            "validation_id": validation.id,
            "message": "Validation recorded successfully",
        }
    )
