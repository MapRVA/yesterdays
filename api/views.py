import logging
import mimetypes
from datetime import datetime

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import DatabaseError, connection, transaction
from django.db.models import (
    Case,
    CharField,
    Count,
    Exists,
    F,
    Max,
    OuterRef,
    Q,
    Subquery,
    Value,
    When,
)
from django.db.models.functions import Coalesce
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from oauth2_provider.models import get_application_model
from psycopg import sql
from rest_framework import generics, mixins, status, viewsets
from rest_framework import serializers as drf_serializers
from rest_framework.decorators import (
    action,
    api_view,
    permission_classes,
    throttle_classes,
)
from rest_framework.filters import OrderingFilter
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework_gis.filters import InBBoxFilter

from activity.views import get_activity_events
from images.models import (
    AerialGeoreference,
    AerialGeoreferenceValidation,
    Collection,
    Georeference,
    GeoreferenceValidation,
    Image,
    ImportSlot,
    License,
    SiteSettings,
    Source,
    SubjectMapping,
    SubjectMappingActivity,
)
from images.in_view import in_view_count, in_view_rows, parse_radius
from images.tasks import process_image
from images.utils import R2Uploader, get_confidence_breakdown, get_overall_stats
from images.validation import InvalidInput, validate_latitude, validate_longitude
from images.views.search import HAS_POSTGRES_SEARCH, _get_text_embedding
from subjects.models import OsmElement, Subject

from .filters import FromAboveGeoreferenceFilter, GeoreferenceFilter, ImageFilter
from .pagination import GeoJsonDefaultPagination
from .permissions import IsImporter, is_importer
from .serializers import (
    AppRegistrationSerializer,
    CollectionCreateSerializer,
    CollectionSerializer,
    FromAboveGeoreferenceGeoSerializer,
    GeoreferenceGeoSerializer,
    ImageListSerializer,
    ImageReplaceSerializer,
    ImageSerializer,
    ImportCancelSerializer,
    ImportCommitSerializer,
    LicenseSerializer,
    OsmElementGeoSerializer,
    SourceCreateSerializer,
    SourceSerializer,
    SubjectSerializer,
    UserSerializer,
)
from .throttling import AppRegistrationThrottle


class RemappingOrderingFilter(OrderingFilter):
    """OrderingFilter that remaps API field names to queryset field names.

    Views can set ``ordering_field_map`` to a dict like
    ``{"username": "first_name"}`` and users can order by ``?ordering=username``
    while the actual ``order_by()`` uses ``first_name``.
    """

    def filter_queryset(self, request, queryset, view):
        ordering = self.get_ordering(request, queryset, view)
        field_map = getattr(view, "ordering_field_map", {})
        if ordering:

            def _remap(field):
                if field.startswith("-"):
                    return f"-{field_map.get(field[1:], field[1:])}"
                return field_map.get(field, field)

            ordering = [_remap(f) for f in ordering]
            return queryset.order_by(*ordering)
        return queryset


class UserViewSet(viewsets.ReadOnlyModelViewSet):
    """Contributors who have georeferenced at least one image."""

    serializer_class = UserSerializer
    filter_backends = [RemappingOrderingFilter]
    ordering_fields = ["username", "point_georeferences", "from_above_georeferences"]
    ordering = ["first_name"]
    ordering_field_map = {
        "username": "first_name",
    }

    def get_queryset(self):
        return User.objects.annotate(
            point_georeferences=Count(
                "georeferenced_images",
                filter=Q(georeferenced_images__image__is_searchable=True),
                distinct=True,
            ),
            from_above_georeferences=Count(
                "aerial_georeferenced_images",
                filter=Q(aerial_georeferenced_images__image__is_searchable=True),
                distinct=True,
            ),
        ).filter(Q(point_georeferences__gt=0) | Q(from_above_georeferences__gt=0))

    def get_object(self):
        osm_id = self.kwargs[self.lookup_field]
        if str(osm_id) == "0":
            username = "hardcoded_admin"
        else:
            username = f"osm_{osm_id}"
        return generics.get_object_or_404(self.get_queryset(), username=username)


class SourceViewSet(
    mixins.CreateModelMixin,
    viewsets.ReadOnlyModelViewSet,
):
    """Archive sources containing collections of historical images."""

    serializer_class = SourceSerializer
    filterset_fields = ["slug"]
    ordering_fields = ["name"]
    ordering = ["name"]

    def get_serializer_class(self):
        if self.action == "create":
            return SourceCreateSerializer
        return SourceSerializer

    def get_permissions(self):
        if self.action == "create":
            return [IsImporter()]
        return super().get_permissions()

    def get_queryset(self):
        if is_importer(self.request):
            return Source.objects.annotate(
                collection_count=Count("collections", distinct=True),
                image_count=Count(
                    "collections__images",
                    filter=Q(collections__images__duplicate_of__isnull=True),
                    distinct=True,
                ),
            )
        return Source.objects.filter(public=True).annotate(
            collection_count=Count(
                "collections",
                filter=Q(collections__public=True),
                distinct=True,
            ),
            image_count=Count(
                "collections__images",
                filter=Q(
                    collections__public=True,
                    collections__images__duplicate_of__isnull=True,
                ),
                distinct=True,
            ),
        )


class CollectionViewSet(
    mixins.CreateModelMixin,
    viewsets.ReadOnlyModelViewSet,
):
    """Collections of historical images within an archive source."""

    serializer_class = CollectionSerializer
    filterset_fields = ["source", "slug"]
    ordering_fields = ["name", "source__name"]
    ordering = ["source__name", "name"]

    def get_serializer_class(self):
        if self.action == "create":
            return CollectionCreateSerializer
        return CollectionSerializer

    def get_permissions(self):
        if self.action == "create":
            return [IsImporter()]
        return super().get_permissions()

    def get_queryset(self):
        base = Collection.objects.select_related("source").annotate(
            image_count=Count("images", filter=Q(images__duplicate_of__isnull=True)),
        )
        if is_importer(self.request):
            qs = base
        else:
            qs = base.filter(public=True, source__public=True)

        # Support nested URL: /api/v2/sources/{source_pk}/collections/
        source_pk = self.kwargs.get("source_pk")
        if source_pk is not None:
            qs = qs.filter(source_id=source_pk)

        return qs


class ImageViewSet(viewsets.ReadOnlyModelViewSet):
    """Historical images available for georeferencing."""

    filterset_class = ImageFilter
    ordering_fields = [
        "order",
        "title",
        "original_date",
        "created_at",
        "last_georeferenced_at",
    ]
    ordering = ["-order"]

    def get_serializer_class(self):
        if self.action == "list":
            return ImageListSerializer
        return ImageSerializer

    def get_queryset(self):
        qs = (
            Image.objects.filter(
                collection__public=True,
                collection__source__public=True,
            )
            .annotate(
                order=F("id"),
                last_georeferenced_at=Max("georeferences__georeferenced_at"),
            )
            .select_related(
                "collection__source",
                "license",
            )
        )

        if self.action == "list":
            # Annotate georeference_status in SQL to avoid N+1 queries.
            # Mirrors the logic in Image.georeference_status property.
            has_georef = Exists(Georeference.objects.filter(image=OuterRef("pk")))
            has_aerial_georef = Exists(
                AerialGeoreference.objects.filter(image=OuterRef("pk"))
            )
            qs = qs.annotate(
                _georeference_status=Case(
                    When(duplicate_of__isnull=False, then=Value("duplicate")),
                    When(will_not_georef=True, then=Value("will_not_georef")),
                    # Aerial images: georeferenced only with a polygon georef
                    When(
                        Q(aerial=True) & has_aerial_georef, then=Value("georeferenced")
                    ),
                    # Non-aerial images: georeferenced with a point georef
                    When(Q(aerial=False) & has_georef, then=Value("georeferenced")),
                    default=Value("pending"),
                    output_field=CharField(),
                ),
            )
        else:
            # Detail view: prefetch related objects (property is fine here)
            qs = qs.prefetch_related(
                "subjects__wikidata_item",
                "georeferences__georeferenced_by",
                "georeferences__validations__validated_by",
                "aerial_georeferences__georeferenced_by",
                "aerial_georeferences__validations__validated_by",
                "comments__commented_by",
            )

        return qs


class SubjectViewSet(viewsets.ReadOnlyModelViewSet):
    """Subjects (buildings, people, monuments, etc.) that appear in images."""

    serializer_class = SubjectSerializer
    filterset_fields = ["slug"]
    ordering_fields = ["title", "image_count"]
    ordering = ["title"]

    def get_queryset(self):
        return (
            Subject.objects.select_related("wikidata_item")
            .annotate(
                image_count=Count(
                    "image_mappings",
                    filter=Q(image_mappings__image__is_searchable=True),
                ),
            )
            .filter(image_count__gt=0)
        )

    @action(detail=True, methods=["get"])
    def geometry(self, request, pk=None):
        """GeoJSON FeatureCollection of OSM geometries for this subject."""
        generics.get_object_or_404(Subject, pk=pk)
        elements = OsmElement.objects.filter(subject_id=pk)
        serializer = OsmElementGeoSerializer(elements, many=True)
        return Response(serializer.data)


class LicenseViewSet(viewsets.ReadOnlyModelViewSet):
    """Licenses recognized by this instance, used when importing images."""

    serializer_class = LicenseSerializer
    queryset = License.objects.all().order_by("name")
    pagination_class = None


class GeoreferenceViewSet(viewsets.ReadOnlyModelViewSet):
    """Point georeferences as GeoJSON.

    Returns a GeoJSON FeatureCollection. Each feature represents the most
    recent point georeference for an image, with the photographer's location
    as its geometry.

    Supports bounding box filtering via the `in_bbox` parameter:
        ?in_bbox=west,south,east,north
    """

    serializer_class = GeoreferenceGeoSerializer
    pagination_class = GeoJsonDefaultPagination
    filterset_class = GeoreferenceFilter
    filter_backends = [DjangoFilterBackend, RemappingOrderingFilter, InBBoxFilter]
    bbox_filter_field = "point"
    ordering_fields = ["georeferenced_at", "confidence", "validation_count"]
    ordering = ["-georeferenced_at"]
    ordering_field_map = {
        "validation_count": "_validation_count",
    }

    def get_queryset(self):
        # Subquery: most recent georeference ID per image
        latest_ids = (
            Georeference.objects.filter(image__is_searchable=True)
            .order_by("image_id", "-georeferenced_at")
            .distinct("image_id")
            .values("id")
        )
        return (
            Georeference.objects.filter(id__in=latest_ids)
            .select_related("image", "georeferenced_by")
            .annotate(
                _validation_count=Coalesce(
                    Subquery(
                        GeoreferenceValidation.objects.filter(
                            georeference=OuterRef("pk"),
                        )
                        .values("georeference")
                        .annotate(c=Count("*"))
                        .values("c"),
                    ),
                    Value(0),
                ),
            )
        )


class FromAboveGeoreferenceViewSet(viewsets.ReadOnlyModelViewSet):
    """From-above (polygon) georeferences as GeoJSON.

    Returns a GeoJSON FeatureCollection. Each feature represents the most
    recent from-above georeference for an image, with the coverage polygon as
    its geometry.

    Supports bounding box filtering via the `in_bbox` parameter:
        ?in_bbox=west,south,east,north
    """

    serializer_class = FromAboveGeoreferenceGeoSerializer
    pagination_class = GeoJsonDefaultPagination
    filterset_class = FromAboveGeoreferenceFilter
    filter_backends = [DjangoFilterBackend, RemappingOrderingFilter, InBBoxFilter]
    bbox_filter_field = "polygon"
    ordering_fields = ["georeferenced_at", "confidence", "validation_count"]
    ordering = ["-georeferenced_at"]
    ordering_field_map = {
        "validation_count": "_validation_count",
    }

    def get_queryset(self):
        # Subquery: most recent from-above georeference ID per image
        latest_ids = (
            AerialGeoreference.objects.filter(
                image__is_searchable=True,
                image__aerial=True,
            )
            .order_by("image_id", "-georeferenced_at")
            .distinct("image_id")
            .values("id")
        )
        return (
            AerialGeoreference.objects.filter(id__in=latest_ids)
            .select_related("image", "georeferenced_by")
            .annotate(
                _validation_count=Coalesce(
                    Subquery(
                        AerialGeoreferenceValidation.objects.filter(
                            georeference=OuterRef("pk"),
                        )
                        .values("georeference")
                        .annotate(c=Count("*"))
                        .values("c"),
                    ),
                    Value(0),
                ),
            )
        )


# ---------------------------------------------------------------------------
# Current user info
# ---------------------------------------------------------------------------


@api_view(["POST"])
@permission_classes([])
@throttle_classes([AppRegistrationThrottle])
def register_app_view(request):
    """Dynamically register an OAuth2 client (Mastodon-style /api/v1/apps).

    No authentication required so apps can self-register on first contact with
    an instance. Returns the client_secret in plaintext exactly once — the
    server stores only a hashed copy.
    """
    serializer = AppRegistrationSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    Application = get_application_model()
    app = Application(
        name=serializer.validated_data["name"],
        client_type=serializer.validated_data["client_type"],
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris=serializer.validated_data["redirect_uris"],
        skip_authorization=False,
        user=request.user if request.user.is_authenticated else None,
    )
    plaintext_secret = app.client_secret
    try:
        app.full_clean(exclude=["client_secret"])
    except DjangoValidationError as e:
        raise drf_serializers.ValidationError(e.message_dict)
    app.save()

    # Public clients can't keep a secret, so don't store one. QuerySet.update
    # bypasses ClientSecretField.pre_save, which would otherwise hash "" into a
    # PBKDF2 of empty string.
    if app.client_type == Application.CLIENT_PUBLIC:
        Application.objects.filter(pk=app.pk).update(client_secret="")
        plaintext_secret = ""

    return Response(
        {
            "id": app.pk,
            "name": app.name,
            "client_id": app.client_id,
            "client_secret": plaintext_secret,
            "client_type": app.client_type,
            "redirect_uris": app.redirect_uris,
        },
        status=status.HTTP_201_CREATED,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def me_view(request):
    """Return the authenticated user's profile and permissions."""
    user = request.user
    osm_id = int(user.username[4:]) if user.username.startswith("osm_") else 0

    # For OAuth, mirror IsImporter: capability is conditional on the token's
    # scope, not just user.is_staff. For session auth, no scope filter applies.
    if hasattr(request, "auth") and hasattr(request.auth, "scope"):
        scopes = request.auth.scope.split()
        can_import = user.is_staff and "import" in scopes
    else:
        scopes = []
        can_import = user.is_staff

    return Response(
        {
            "osm_id": osm_id,
            "username": user.get_display_name(),
            "is_staff": user.is_staff,
            "can_import": can_import,
            "scopes": scopes,
        }
    )


# ---------------------------------------------------------------------------
# Import endpoints
# ---------------------------------------------------------------------------


@api_view(["GET"])
@permission_classes([IsImporter])
def import_upload_url_view(request):
    """Create an import slot and return a presigned S3 upload URL.

    Query parameters:
        collection: collection ID to import into (required)
        content_type: MIME type of the image (default: image/jpeg)
    """
    collection_id = request.query_params.get("collection")
    if not collection_id:
        return Response(
            {"error": "The 'collection' query parameter is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        collection = Collection.objects.get(pk=int(collection_id))
    except (Collection.DoesNotExist, ValueError):
        return Response(
            {"error": "Collection not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    content_type = request.query_params.get("content_type", "image/jpeg")
    ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ""
    if ext in (".jpe", ".jpeg"):
        ext = ".jpg"

    slot = ImportSlot.objects.create(
        collection=collection,
        created_by=request.user,
        s3_key="",  # set below
        content_type=content_type,
    )
    slot.s3_key = f"imports/{slot.slot_id}{ext}"
    slot.save(update_fields=["s3_key"])

    r2 = R2Uploader()
    upload_url = r2.generate_presigned_put_url(
        slot.s3_key, content_type, expiration=900
    )

    return Response(
        {
            "slot_id": str(slot.slot_id),
            "upload_url": upload_url,
            "upload_headers": {"Content-Type": content_type},
            "cdn_url": r2.get_public_url(slot.s3_key),
        }
    )


@api_view(["POST"])
@permission_classes([IsImporter])
def import_commit_view(request):
    """Commit an uploaded image: verify, create Image row, move to permanent S3 key.

    Accepts metadata for a single image linked to an existing ImportSlot.
    The image must already be uploaded to S3 at the slot's presigned URL.
    """
    serializer = ImportCommitSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    try:
        slot = ImportSlot.objects.get(
            slot_id=data["slot_id"],
            created_by=request.user,
        )
    except ImportSlot.DoesNotExist:
        return Response(
            {"error": "Import slot not found or does not belong to you."},
            status=status.HTTP_404_NOT_FOUND,
        )

    # Verify the file was actually uploaded to S3
    r2 = R2Uploader()
    head = r2.head_object(slot.s3_key)
    if head is None:
        return Response(
            {"error": "Image has not been uploaded to S3 yet."},
            status=status.HTTP_409_CONFLICT,
        )

    # Resolve license (serializer already verified it exists)
    image_license = None
    if data["license_name"]:
        image_license = License.objects.get(name__iexact=data["license_name"])

    # Determine file extension from the slot's S3 key
    ext = ""
    if "." in slot.s3_key:
        ext = "." + slot.s3_key.rsplit(".", 1)[1]

    # Do all DB writes inside one atomic block, with the slot row locked via
    # SELECT FOR UPDATE so concurrent commits of the same slot_id serialize.
    # The slot.delete() stays inside the transaction — the lock is held until
    # the row is actually gone, so the loser of the race observes DoesNotExist.
    #
    # If copy_object succeeds but a later step in the block fails, the dest in
    # R2 becomes an orphan when the transaction rolls back — sweep it in the
    # except path. S3 DELETE is idempotent, so the sweep is a no-op if the
    # copy never happened.
    temp_key = slot.s3_key
    dest_key = None
    try:
        with transaction.atomic():
            slot = ImportSlot.objects.select_for_update().get(
                slot_id=data["slot_id"],
                created_by=request.user,
            )
            image = Image.objects.create(
                collection=slot.collection,
                title=data["title"],
                permalink="",  # placeholder, set after S3 copy
                original_url=data["source_url"] or None,
                description=data["description"] or None,
                creator=data["creator"] or None,
                ref=data["reference_id"] or None,
                original_date=data["original_date"],
                edtf_date=data["edtf_date"],
                license=image_license,
                rotation=data["rotation"],
                mirror=data["mirror"],
            )

            subjects = data.get("subjects", [])
            for idx, subject in enumerate(subjects):
                SubjectMapping.objects.create(image=image, subject=subject, order=idx)
                SubjectMappingActivity.objects.create(
                    user=request.user,
                    image=image,
                    subject=subject,
                    action=SubjectMappingActivity.ACTION_ADDED,
                )

            dest_key = f"images/{image.id}/original{ext}"
            cdn_url = r2.copy_object(slot.s3_key, dest_key)
            image.permalink = cdn_url
            image.save(update_fields=["permalink"])
            slot.delete()
    except ImportSlot.DoesNotExist:
        # Lost a race with a concurrent commit — the other request already
        # consumed this slot.
        return Response(
            {"error": "Import slot not found or does not belong to you."},
            status=status.HTTP_404_NOT_FOUND,
        )
    except Exception:
        if dest_key:
            try:
                r2.delete_file(dest_key)
            except Exception:
                logger.warning(
                    "Failed to clean up orphan copy at %s", dest_key, exc_info=True
                )
        raise

    # Post-commit: the slot row is already deleted inside the transaction;
    # delete the S3 temp file outside. Failures are non-fatal — stragglers
    # are reaped by cleanup_stale_import_slots.
    try:
        r2.delete_file(temp_key)
    except Exception:
        logger.warning("Failed to delete temp upload %s", temp_key, exc_info=True)

    # Image processing is queued on commit by the post_save signal from the
    # permalink save above — no explicit trigger here, so exactly one
    # process_image task runs per imported image. Duplicate concurrent tasks
    # each claim their own asset_generation and race to be the last DB write,
    # which can leave the row pointing at a generation directory that
    # cleanup_old_image_assets then deletes.

    return Response(
        {"image_id": image.id},
        status=status.HTTP_201_CREATED,
    )


@api_view(["POST"])
@permission_classes([IsImporter])
def import_cancel_view(request):
    """Cancel pending import slots and clean up their temporary S3 files."""
    serializer = ImportCancelSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    slots = ImportSlot.objects.filter(
        slot_id__in=serializer.validated_data["slot_ids"],
        created_by=request.user,
    )

    r2 = R2Uploader()
    deleted_count = 0
    for slot in slots:
        r2.delete_file(slot.s3_key)
        deleted_count += 1
    slots.delete()

    return Response({"deleted": deleted_count})


@api_view(["POST"])
@permission_classes([IsImporter])
def image_replace_view(request, id):
    """Replace an existing image's original bytes from an uploaded ImportSlot.

    The slot's R2 object becomes the image's new canonical original. Derived
    assets (thumbnail, transformed, IIIF tiles) are cleared and regenerated by
    ``process_image``. Rotation and mirror reset to 0/none — the replacement
    file is treated as correctly oriented as uploaded.
    """
    serializer = ImageReplaceSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    try:
        image = Image.objects.get(pk=id)
    except Image.DoesNotExist:
        return Response(
            {"error": "Image not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    try:
        slot = ImportSlot.objects.get(
            slot_id=data["slot_id"],
            created_by=request.user,
        )
    except ImportSlot.DoesNotExist:
        return Response(
            {"error": "Import slot not found or does not belong to you."},
            status=status.HTTP_404_NOT_FOUND,
        )

    if slot.collection_id != image.collection_id:
        return Response(
            {"error": "Slot belongs to a different collection than the target image."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    r2 = R2Uploader()
    if r2.head_object(slot.s3_key) is None:
        return Response(
            {"error": "Image has not been uploaded to S3 yet."},
            status=status.HTTP_409_CONFLICT,
        )

    ext = ""
    if "." in slot.s3_key:
        ext = "." + slot.s3_key.rsplit(".", 1)[1]

    temp_key = slot.s3_key
    dest_key = f"images/{image.id}/original{ext}"
    # Capture any existing top-level `original*` keys (e.g. original.jpg) under
    # image locks — once the DB commit replaces the canonical bytes, these are
    # orphans to be swept. Doing the list inside the transaction ensures
    # concurrent replaces don't delete each other's new files: serialized by
    # the image row lock, each transaction only sees its predecessor's keys.
    old_originals: list[str] = []
    try:
        with transaction.atomic():
            slot = ImportSlot.objects.select_for_update().get(
                slot_id=data["slot_id"],
                created_by=request.user,
            )
            Image.objects.select_for_update().only("id").get(pk=image.id)

            prefix = f"images/{image.id}/"
            for key in r2.iter_keys(prefix):
                remainder = key[len(prefix) :]
                if "/" in remainder:
                    continue
                if remainder.startswith("original"):
                    old_originals.append(key)

            cdn_url = r2.copy_object(slot.s3_key, dest_key)
            # thumbnail, transformed_permalink and iiif_url deliberately keep
            # pointing at the previous generation. Each flips to the new
            # generation the instant that asset exists on R2 — the thumbnail
            # when process_image finishes, iiif_url when generate_iiif_tiles
            # does — so holding them here delays nothing. It only avoids the
            # window where they are empty and every surface falls back to the
            # raw original, which for a TIFF upload no browser can display.
            # tile_status is cleared because it is the "tiles need rebuilding"
            # flag, not a display field.
            Image.objects.filter(pk=image.id).update(
                permalink=cdn_url,
                rotation=0,
                mirror="none",
                tile_status="",
                tile_error="",
            )
            slot.delete()
    except ImportSlot.DoesNotExist:
        return Response(
            {"error": "Import slot not found or does not belong to you."},
            status=status.HTTP_404_NOT_FOUND,
        )
    except Exception:
        try:
            r2.delete_file(dest_key)
        except Exception:
            logger.warning(
                "Failed to clean up orphan copy at %s", dest_key, exc_info=True
            )
        raise

    # Sweep the temp upload and any stale top-level `original*` keys that no
    # longer match the new extension. Failures are non-fatal — stragglers are
    # reaped by cleanup_stale_import_slots / future cleanup runs.
    try:
        r2.delete_file(temp_key)
    except Exception:
        logger.warning("Failed to delete temp upload %s", temp_key, exc_info=True)

    for key in old_originals:
        if key == dest_key:
            continue
        try:
            r2.delete_file(key)
        except Exception:
            logger.warning("Failed to delete stale original %s", key, exc_info=True)

    def trigger_replace_processing():
        try:
            # force=True: the derived-asset fields still hold the previous
            # generation's URLs, so process_image's "nothing to do"
            # short-circuit would otherwise skip rebuilding them from the
            # new bytes.
            process_image.delay(image.id, force=True)
        except Exception:
            logger.warning("Failed to queue process_image for image %d", image.id)

    # Ensure image is processed after the image has been created
    transaction.on_commit(trigger_replace_processing)

    return Response(
        {"image_id": image.id},
        status=status.HTTP_200_OK,
    )


# ---------------------------------------------------------------------------
# Site statistics
# ---------------------------------------------------------------------------


@api_view(["GET"])
def stats_view(request):
    """Site-wide statistics: source, collection, image, and georeferencing counts."""
    stats = get_overall_stats()
    breakdown = get_confidence_breakdown()
    total_georeferences = (
        Georeference.objects.filter(
            image__collection__public=True,
            image__collection__source__public=True,
        ).count()
        + AerialGeoreference.objects.filter(
            image__collection__public=True,
            image__collection__source__public=True,
        ).count()
    )
    return Response(
        {
            "name": SiteSettings.load().site_title,
            "total_sources": stats["total_sources"],
            "total_collections": stats["total_collections"],
            "total_images": stats["total_images"],
            "georeferenced_images": stats["total_georeferenced"],
            "total_georeferences": total_georeferences,
            "confidence_breakdown": {
                "low": breakdown["low"],
                "medium": breakdown["medium"],
                "high": breakdown["high"],
            },
        }
    )


# ---------------------------------------------------------------------------
# Activity feed
# ---------------------------------------------------------------------------


def _serialize_activity_event(event_type, obj, timestamp):
    """Convert one activity event tuple into a serializable dict."""
    if event_type == "group":
        images = []
        for member in obj.feed_members:
            image = member.image
            if image:
                images.append(
                    {
                        "id": image.id,
                        "title": image.title,
                        "thumbnail": image.thumbnail,
                    }
                )
        return {
            "type": "georeference_group",
            "timestamp": timestamp,
            "data": {
                "user": obj.user.get_display_name() if obj.user else "Anonymous",
                "count": obj.feed_count,
                "started_at": obj.started_at,
                "ended_at": obj.feed_ended_at,
                "images": images,
            },
        }
    elif event_type == "comment":
        return {
            "type": "comment",
            "timestamp": timestamp,
            "data": {
                "user": obj.commented_by.get_display_name(),
                "text": obj.text,
                "image_id": obj.image_id,
                "image_title": obj.image.title,
            },
        }
    elif event_type == "milestone":
        return {
            "type": "user_milestone",
            "timestamp": timestamp,
            "data": {
                "user": obj.user.get_display_name(),
                "count": obj.count,
            },
        }
    elif event_type == "sitewide":
        return {
            "type": "sitewide_milestone",
            "timestamp": timestamp,
            "data": {
                "count": obj.count,
            },
        }
    elif event_type == "subject":
        images = []
        for member in obj.feed_members:
            if member.image:
                images.append(
                    {
                        "id": member.image.id,
                        "title": member.image.title,
                        "thumbnail": member.image.thumbnail,
                    }
                )
        data = {
            "user": obj.user.get_display_name(),
            "action": obj.action,
            "count": obj.feed_count,
            "started_at": obj.started_at,
            "ended_at": obj.feed_ended_at,
            "subject_id": obj.subject_id,
            "subject_title": obj.subject.title if obj.subject else None,
            "images": images,
        }
        if obj.action == "reordered":
            first_member = obj.feed_members[0] if images else None
            if first_member is not None:
                data["previous_order"] = first_member.previous_order
                data["new_order"] = first_member.new_order
        return {
            "type": "subject_activity_group",
            "timestamp": timestamp,
            "data": data,
        }
    elif event_type == "new_subject":
        rep_image = obj.subject.get_representative_image()
        return {
            "type": "subject_introduction",
            "timestamp": timestamp,
            "data": {
                "user": obj.user.get_display_name() if obj.user else None,
                "subject_id": obj.subject_id,
                "subject_title": obj.subject.title,
                "representative_image_id": rep_image.id if rep_image else None,
                "representative_image_thumbnail": rep_image.thumbnail
                if rep_image
                else None,
            },
        }
    elif event_type == "new_collection":
        return {
            "type": "collection_introduction",
            "timestamp": timestamp,
            "data": {
                "collection_id": obj.collection_id,
                "collection_name": obj.collection.name,
            },
        }


@api_view(["GET"])
def activity_view(request):
    """Recent activity feed: georeference groups, comments, and milestones.

    Query parameters:
        before: ISO 8601 timestamp -- return events before this time (for pagination)
        types: comma-separated event types to include
               (georeference_group, comment, user_milestone, sitewide_milestone,
               subject_activity_group, subject_introduction,
               collection_introduction). Defaults to all.
        limit: number of events to return (default 20, max 100)
    """
    # Map API type names (matching response) to internal event type names
    _TYPE_MAP = {
        "georeference_group": "group",
        "comment": "comment",
        "user_milestone": "milestone",
        "sitewide_milestone": "sitewide",
        "subject_activity_group": "subject",
        "subject_introduction": "new_subject",
        "collection_introduction": "new_collection",
    }

    before = None
    before_param = request.query_params.get("before")
    if before_param:
        try:
            before = datetime.fromisoformat(before_param)
            if timezone.is_naive(before):
                before = timezone.make_aware(before)
        except (ValueError, TypeError):
            return Response({"error": "Invalid 'before' timestamp"}, status=400)

    types_param = request.query_params.get("types", "")
    if types_param:
        event_types = set()
        for t in types_param.split(","):
            if t in _TYPE_MAP:
                event_types.add(_TYPE_MAP[t])
        if not event_types:
            return Response([])
    else:
        event_types = set(_TYPE_MAP.values())

    try:
        limit = max(min(int(request.query_params.get("limit", 20)), 100), 1)
    except (ValueError, TypeError):
        return Response(
            {"error": "Invalid 'limit' parameter. Must be an integer."},
            status=400,
        )

    events = get_activity_events(before=before, limit=limit, event_types=event_types)

    result = []
    for event_type, obj, timestamp in events:
        serialized = _serialize_activity_event(event_type, obj, timestamp)
        if serialized:
            result.append(serialized)

    return Response(result)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)


_ALLOWED_TABLE_REFS = {"images_image", "i"}


def _parse_search_filters(params, table_ref="images_image"):
    """Parse shared filter query parameters for search endpoints.

    Args:
        params: query parameter dict
        table_ref: SQL table name or alias to use for column references.
            Must be one of the values in ``_ALLOWED_TABLE_REFS``.

    Returns (where_conditions, where_params, error_response) where
    where_conditions is a list of ``psycopg.sql.Composable`` fragments and
    error_response is a Response if validation failed, else None.
    """
    if table_ref not in _ALLOWED_TABLE_REFS:
        raise ValueError(
            f"table_ref must be one of {_ALLOWED_TABLE_REFS}, got {table_ref!r}"
        )
    t = sql.Identifier(table_ref)
    where_conditions = []
    where_params = {}

    # Georeferenced filtering (mirrors Image.is_georeferenced: aerial images are
    # judged by polygon georefs, others by point georefs)
    georeferenced = params.get("georeferenced")
    if georeferenced is not None:
        if georeferenced.lower() == "true":
            where_conditions.append(
                sql.SQL(
                    "(({t}.aerial = false AND EXISTS (SELECT 1 FROM images_georeference g"
                    " WHERE g.image_id = {t}.id))"
                    " OR ({t}.aerial = true AND EXISTS (SELECT 1 FROM images_aerialgeoreference ag"
                    " WHERE ag.image_id = {t}.id)))"
                ).format(t=t)
            )
        elif georeferenced.lower() == "false":
            where_conditions.append(
                sql.SQL(
                    "NOT (({t}.aerial = false AND EXISTS (SELECT 1 FROM images_georeference g"
                    " WHERE g.image_id = {t}.id))"
                    " OR ({t}.aerial = true AND EXISTS (SELECT 1 FROM images_aerialgeoreference ag"
                    " WHERE ag.image_id = {t}.id)))"
                ).format(t=t)
            )

    # Year filtering
    year_min = params.get("year_min")
    if year_min is not None:
        try:
            where_params["year_min"] = int(year_min)
            where_conditions.append(
                sql.SQL("({t}.fuzzy_end_decdate >= %(year_min)s)").format(t=t)
            )
        except ValueError:
            return (
                [],
                {},
                Response({"error": "year_min must be an integer."}, status=400),
            )

    year_max = params.get("year_max")
    if year_max is not None:
        try:
            where_params["year_max"] = int(year_max)
            where_conditions.append(
                sql.SQL("({t}.fuzzy_start_decdate <= %(year_max)s)").format(t=t)
            )
        except ValueError:
            return (
                [],
                {},
                Response({"error": "year_max must be an integer."}, status=400),
            )

    # Subject filtering
    subject = params.get("subject")
    if subject is not None:
        try:
            where_params["subject_id"] = int(subject)
            where_conditions.append(
                sql.SQL(
                    "EXISTS (SELECT 1 FROM images_subjectmapping sm"
                    " WHERE sm.image_id = {t}.id"
                    " AND sm.subject_id = %(subject_id)s)"
                ).format(t=t)
            )
        except ValueError:
            return (
                [],
                {},
                Response({"error": "subject must be an integer."}, status=400),
            )

    # Source filtering
    source = params.get("source")
    if source is not None:
        try:
            where_params["source_id"] = int(source)
            where_conditions.append(
                sql.SQL(
                    "{t}.collection_id IN (SELECT id FROM images_collection"
                    " WHERE source_id = %(source_id)s)"
                ).format(t=t)
            )
        except ValueError:
            return [], {}, Response({"error": "source must be an integer."}, status=400)

    # Collection filtering
    collection = params.get("collection")
    if collection is not None:
        try:
            where_params["collection_id"] = int(collection)
            where_conditions.append(
                sql.SQL("{t}.collection_id = %(collection_id)s").format(t=t)
            )
        except ValueError:
            return (
                [],
                {},
                Response({"error": "collection must be an integer."}, status=400),
            )

    return where_conditions, where_params, None


def _format_in_view_result(request, image, row):
    """Format one image + its nearest-georeference row into a result dict.

    Deliberately not ``_format_search_result``: that one reports a
    ``similarity`` of ``1.0 - distance``, which is meaningless for a distance
    measured in metres.
    """
    return {
        "id": image.id,
        "title": image.title,
        "permalink": image.display_permalink,
        "thumbnail": image.thumbnail or image.display_permalink,
        "original_date": image.original_date,
        "date_display": image.date_display,
        "distance_m": round(row["distance_m"], 1),
        "georeference": {
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "direction": row["direction"],
            "confidence": row["confidence"],
        },
        "collection": {
            "id": image.collection.id,
            "name": image.collection.name,
            "slug": image.collection.slug,
            "source_name": image.collection.source.name,
        },
        "detail_url": request.build_absolute_uri(f"/api/v2/images/{image.id}/"),
    }


def _format_search_result(request, image, distance):
    """Format one image + distance into a search result dict."""
    similarity = 1.0 - float(distance)
    result = {
        "id": image.id,
        "title": image.title,
        "permalink": image.display_permalink,
        "thumbnail": image.thumbnail or image.display_permalink,
        "original_date": image.original_date,
        "date_display": image.date_display,
        "similarity": round(similarity, 4),
        "collection": {
            "id": image.collection.id,
            "name": image.collection.name,
            "slug": image.collection.slug,
            "source_name": image.collection.source.name,
        },
        "detail_url": request.build_absolute_uri(f"/api/v2/images/{image.id}/"),
    }
    return result


@api_view(["GET"])
def semantic_search_view(request):
    """Search images by meaning using CLIP embeddings.

    Encodes the query text into a vector and finds images whose visual
    content is most similar, even if the words don't appear in the title
    or description.

    Query parameters:
        q: search query (required, max 500 characters)
        page: page number (default 1)
        page_size: results per page (default 20, max 100)
        georeferenced: true/false -- filter by georeferencing status
        year_min: minimum year
        year_max: maximum year
        source: filter by source ID
        collection: filter by collection ID
        subject: filter by subject ID
    """
    query = request.query_params.get("q", "").strip()
    if not query:
        return Response(
            {"error": "The 'q' query parameter is required."},
            status=400,
        )
    if len(query) > 500:
        return Response(
            {"error": "Query too long. Maximum 500 characters."},
            status=400,
        )

    try:
        page = max(int(request.query_params.get("page", 1)), 1)
        page_size = min(max(int(request.query_params.get("page_size", 20)), 1), 100)
    except ValueError:
        return Response({"error": "Invalid pagination parameters."}, status=400)
    offset = (page - 1) * page_size

    # Parse shared filters
    extra_conditions, extra_params, err = _parse_search_filters(request.query_params)
    if err:
        return err

    try:
        query_embedding = _get_text_embedding(query)
    except Exception:
        logger.warning("Failed to generate text embedding", exc_info=True)
        return Response({"error": "Could not process search query."}, status=400)

    where_conditions = [
        sql.SQL("embedding IS NOT NULL"),
        sql.SQL("is_searchable = true"),
    ]
    where_conditions.extend(extra_conditions)
    where_clause = sql.SQL(" AND ").join(where_conditions)

    try:
        with connection.cursor() as cursor:
            # Total count
            count_sql = sql.SQL(
                "SELECT COUNT(id) FROM images_image WHERE {where}"
            ).format(where=where_clause)
            cursor.execute(count_sql, extra_params)
            total_count = cursor.fetchone()[0]

            # Paginated results
            query_sql = sql.SQL(
                "SELECT id,"
                " (embedding::vector(768) <=> %(embedding)s::vector(768)) AS distance"
                " FROM images_image"
                " WHERE {where}"
                " ORDER BY embedding::vector(768) <=> %(embedding)s::vector(768), id"
                " LIMIT %(limit)s OFFSET %(offset)s"
            ).format(where=where_clause)

            params = {
                **extra_params,
                "embedding": query_embedding,
                "limit": page_size,
                "offset": offset,
            }
            cursor.execute(query_sql, params)
            rows = cursor.fetchall()

        # Hydrate with ORM for consistent serialization
        image_ids = [row[0] for row in rows]
        distances = {row[0]: row[1] for row in rows}
        images_by_id = {
            img.id: img
            for img in Image.objects.filter(id__in=image_ids).select_related(
                "collection__source"
            )
        }

        results = []
        for image_id in image_ids:
            image = images_by_id.get(image_id)
            if image:
                results.append(
                    _format_search_result(request, image, distances[image_id])
                )

        return Response(
            {
                "count": total_count,
                "page": page,
                "page_size": page_size,
                "query": query,
                "results": results,
            }
        )

    except DatabaseError:
        logger.error("Database error in semantic search", exc_info=True)
        return Response({"error": "Search failed. Please try again."}, status=500)


@api_view(["GET"])
def text_search_view(request):
    """Search images by text using PostgreSQL trigram word similarity.

    Searches across image titles, descriptions, comments, and georeference
    notes.  Results are ranked by how closely the query matches.

    Query parameters:
        q: search query (required, max 500 characters)
        threshold: distance threshold 0-1 (default 0.7, lower is stricter)
        page: page number (default 1)
        page_size: results per page (default 20, max 100)
        georeferenced: true/false -- filter by georeferencing status
        year_min: minimum year
        year_max: maximum year
        source: filter by source ID
        collection: filter by collection ID
        subject: filter by subject ID
    """
    if not HAS_POSTGRES_SEARCH:
        return Response(
            {"error": "Text search is not available."},
            status=503,
        )

    query = request.query_params.get("q", "").strip()
    if not query:
        return Response(
            {"error": "The 'q' query parameter is required."},
            status=400,
        )
    if len(query) > 500:
        return Response(
            {"error": "Query too long. Maximum 500 characters."},
            status=400,
        )

    try:
        page = max(int(request.query_params.get("page", 1)), 1)
        page_size = min(max(int(request.query_params.get("page_size", 20)), 1), 100)
        threshold = max(
            0.0, min(float(request.query_params.get("threshold", 0.7)), 1.0)
        )
    except ValueError:
        return Response({"error": "Invalid numeric parameters."}, status=400)
    offset = (page - 1) * page_size

    # Parse shared filters (text search aliases images_image as "i")
    extra_conditions, extra_params, err = _parse_search_filters(
        request.query_params, table_ref="i"
    )
    if err:
        return err

    filter_conditions = [sql.SQL("i.is_searchable = true")]
    filter_conditions.extend(extra_conditions)
    where_clause = sql.SQL(" AND ").join(filter_conditions)

    sql_params = {
        **extra_params,
        "query": query,
        "threshold": threshold,
        "limit": page_size,
        "offset": offset,
    }

    # The LATERAL joins search comments and georeference notes alongside
    # the image's own title and description.
    lateral_joins = """
        LEFT JOIN LATERAL (
            SELECT MIN(%(query)s <<-> c.text) AS best_comment_distance
            FROM images_comment c WHERE c.image_id = i.id
        ) comment_match ON true
        LEFT JOIN LATERAL (
            SELECT MIN(%(query)s <<-> g.confidence_notes) AS best_geo_distance
            FROM images_georeference g
            WHERE g.image_id = i.id AND g.confidence_notes != ''
        ) geo_match ON true
        LEFT JOIN LATERAL (
            SELECT MIN(%(query)s <<-> ag.confidence_notes) AS best_aerial_distance
            FROM images_aerialgeoreference ag
            WHERE ag.image_id = i.id AND ag.confidence_notes != ''
        ) aerial_match ON true
    """

    distance_expr = """
        LEAST(
            COALESCE(%(query)s <<-> i.title, 1.0),
            COALESCE(%(query)s <<-> i.description, 1.0),
            COALESCE(comment_match.best_comment_distance, 1.0),
            COALESCE(geo_match.best_geo_distance, 1.0),
            COALESCE(aerial_match.best_aerial_distance, 1.0)
        )
    """

    try:
        with connection.cursor() as cursor:
            # Total count
            count_sql = sql.SQL(
                "SELECT COUNT(i.id) FROM images_image i"
                " {laterals}"
                " WHERE {where} AND {distance} < %(threshold)s"
            ).format(
                laterals=sql.SQL(lateral_joins),
                where=where_clause,
                distance=sql.SQL(distance_expr),
            )
            cursor.execute(count_sql, sql_params)
            total_count = cursor.fetchone()[0]

            # Paginated results
            results_sql = sql.SQL(
                "SELECT i.id, {distance} AS distance"
                " FROM images_image i"
                " {laterals}"
                " WHERE {where} AND {distance} < %(threshold)s"
                " ORDER BY distance, i.id"
                " LIMIT %(limit)s OFFSET %(offset)s"
            ).format(
                laterals=sql.SQL(lateral_joins),
                where=where_clause,
                distance=sql.SQL(distance_expr),
            )
            cursor.execute(results_sql, sql_params)
            rows = cursor.fetchall()

        # Hydrate with ORM
        image_ids = [row[0] for row in rows]
        distances = {row[0]: row[1] for row in rows}
        images_by_id = {
            img.id: img
            for img in Image.objects.filter(id__in=image_ids).select_related(
                "collection__source"
            )
        }

        results = []
        for image_id in image_ids:
            image = images_by_id.get(image_id)
            if image:
                results.append(
                    _format_search_result(request, image, distances[image_id])
                )

        return Response(
            {
                "count": total_count,
                "page": page,
                "page_size": page_size,
                "query": query,
                "results": results,
            }
        )

    except DatabaseError:
        logger.error("Database error in text search", exc_info=True)
        return Response({"error": "Search failed. Please try again."}, status=500)


@api_view(["GET"])
def in_view_search_view(request):
    """Find images georeferenced near a coordinate, nearest first.

    The inverse of the other search endpoints: instead of describing an image
    and asking where it might be, you give a location and ask what it used to
    look like. Each image appears once, at its most recent georeference, so a
    location that has since been corrected is not resurfaced. Aerial
    photographs, whose coverage is a polygon rather than a point, are not
    included.

    Results are photographs *of* the coordinate, not merely near it: an image
    whose georeference records a direction is only returned when the
    coordinate falls inside the cone that camera was pointing through.
    Georeferences without a recorded direction are returned on distance alone.

    No radius is required. Results are read straight off the spatial index in
    distance order, so the query stops as soon as the page is full.

    Query parameters:
        lat: latitude of the origin (required)
        lon: longitude of the origin (required)
        radius: optional bound in metres (clamped to the configured maximum)
        page: page number (default 1)
        page_size: results per page (default 20, max 100)
        year_min: minimum year
        year_max: maximum year
        source: filter by source ID
        collection: filter by collection ID
        subject: filter by subject ID
    """
    params = request.query_params

    try:
        latitude = validate_latitude(params, "lat")
        longitude = validate_longitude(params, "lon")
        radius_m = parse_radius(params, "radius")
    except InvalidInput as exc:
        return Response({"error": str(exc)}, status=400)

    try:
        page = max(int(params.get("page", 1)), 1)
        page_size = min(max(int(params.get("page_size", 20)), 1), 100)
    except ValueError:
        return Response({"error": "Invalid pagination parameters."}, status=400)
    offset = (page - 1) * page_size

    # The shared filter helper already emits image-table fragments under the
    # alias `i`, which is exactly what images.in_view expects.
    extra_conditions, extra_params, err = _parse_search_filters(params, table_ref="i")
    if err:
        return err

    try:
        rows, has_more = in_view_rows(
            latitude=latitude,
            longitude=longitude,
            limit=page_size,
            offset=offset,
            radius_m=radius_m,
            extra_conditions=extra_conditions,
            extra_params=extra_params,
        )

        # Without a radius every georeferenced image is technically a result,
        # so a total would be a misleading way of saying "all of them". With
        # one, the count is a cheap bounded query over the same index.
        total_count = None
        if radius_m is not None:
            total_count = in_view_count(
                latitude=latitude,
                longitude=longitude,
                radius_m=radius_m,
                extra_conditions=extra_conditions,
                extra_params=extra_params,
            )
    except InvalidInput as exc:
        return Response({"error": str(exc)}, status=400)
    except DatabaseError:
        logger.error("Database error in in-view search", exc_info=True)
        return Response({"error": "Search failed. Please try again."}, status=500)

    images_by_id = {
        img.id: img
        for img in Image.objects.filter(
            id__in=[row["image_id"] for row in rows]
        ).select_related("collection__source")
    }

    results = []
    for row in rows:
        image = images_by_id.get(row["image_id"])
        if image:
            results.append(_format_in_view_result(request, image, row))

    return Response(
        {
            "latitude": latitude,
            "longitude": longitude,
            "radius": radius_m,
            "page": page,
            "page_size": page_size,
            "count": total_count,
            "has_more": has_more,
            "results": results,
        }
    )
