"""Object-level authorization for image-targeting mutation endpoints.

Every view that writes something attached to an ``Image`` resolves that image
through one of the querysets here rather than through a bare
``Image.objects.get``. Because the visibility rules live in the queryset, an
inaccessible image simply is not in it, and the lookup raises ``Http404`` the
same way a genuinely missing ID does — a caller cannot tell a private image
from a nonexistent one.

``Image.is_searchable`` is deliberately not used. It is a denormalized field
maintained by signals for search performance; bulk writes can leave it stale,
so it is not sound as the authoritative access control. These querysets check
``collection.public`` and ``collection.source.public`` directly.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import Http404

from .models import Album, Image

__all__ = [
    "public_images",
    "accessible_images_for",
    "editable_images_for",
    "georeferenceable_images_for",
    "representable_images",
    "get_image_or_404",
    "get_images_or_404",
    "parse_image_ids",
    "album_contains_private_images",
    "album_for_owner_or_404",
    "any_image_is_private",
    "non_public_images",
]


def _is_staff(user):
    return bool(user and user.is_authenticated and user.is_staff)


def public_images():
    """Images the public is allowed to see at all."""
    return Image.objects.filter(
        collection__public=True,
        collection__source__public=True,
    )


def accessible_images_for(user):
    """Images ``user`` may read.

    Staff see everything, including images staged in private sources and
    collections. Everyone else sees the public set.
    """
    if _is_staff(user):
        return Image.objects.all()
    return public_images()


def editable_images_for(user):
    """Images ``user`` may attach community contributions to.

    On top of read access this drops duplicates: a duplicate is a second copy
    of a photograph we already hold, and contributions belong on the original
    so they are not split across two records. The rule applies to staff too —
    there is no correct way to comment on, tag, or curate a copy.
    """
    return accessible_images_for(user).filter(duplicate_of__isnull=True)


def georeferenceable_images_for(user, *, aerial=False):
    """Images ``user`` may submit a georeference for.

    ``aerial=True`` selects the polygon path, which requires an aerial image of
    everyone including staff. ``aerial=False`` selects the point path, which
    restricts ordinary users to non-aerial images but leaves staff unfiltered:
    ``georeference_interface`` already drops the ``aerial=False`` filter for
    staff, so staff can load an aerial into the point interface and submit a
    point georeference for it. That capability is preserved here rather than
    being removed as a side effect of centralizing the policy.
    """
    queryset = editable_images_for(user).filter(will_not_georef=False)

    if aerial:
        return queryset.filter(aerial=True)
    if _is_staff(user):
        return queryset
    return queryset.filter(aerial=False)


def representable_images():
    """Images that may be published as a subject's representative image.

    A representative image is rendered on public subject pages, so it must be
    public whoever sets it — a staff member picking a staged image would turn
    an authorized action into a public disclosure. Duplicates are excluded for
    the same reason ``editable_images_for`` excludes them.
    """
    return public_images().filter(duplicate_of__isnull=True)


def get_image_or_404(queryset, image_id):
    """Fetch one image from a policy queryset, or raise ``Http404``.

    Unlike ``get_object_or_404`` this also swallows the errors a non-numeric
    ID would raise, so a malformed ID and an inaccessible one are
    indistinguishable to the caller.
    """
    try:
        return queryset.get(pk=int(image_id))
    except Image.DoesNotExist, OverflowError, TypeError, ValueError:
        raise Http404("Image not found")


def parse_image_ids(raw_ids, *, limit=None):
    """Normalize a client-supplied list of image IDs.

    Returns a de-duplicated list of ints in first-seen order. Raises
    ``ValueError`` for a non-list, an unparseable member, an empty list, or a
    list longer than ``limit`` (``settings.BULK_MAX_IMAGE_IDS`` by default).
    """
    if limit is None:
        limit = settings.BULK_MAX_IMAGE_IDS

    if not isinstance(raw_ids, (list, tuple)):
        raise ValueError("image IDs must be a list")

    if len(raw_ids) > limit:
        raise ValueError(f"at most {limit} images may be requested at once")

    seen = {}
    for raw_id in raw_ids:
        # bool is an int subclass and True would silently become image 1.
        if isinstance(raw_id, bool):
            raise ValueError("invalid image ID")
        try:
            seen.setdefault(int(raw_id), None)
        except OverflowError, TypeError, ValueError:
            # OverflowError covers float infinities, which int() refuses.
            raise ValueError("invalid image ID")

    if not seen:
        raise ValueError("no images requested")

    return list(seen)


def get_images_or_404(queryset, image_ids):
    """Fetch every requested image through a policy queryset, all or nothing.

    Raises ``Http404`` unless every ID in ``image_ids`` resolves, so a bulk
    request that mixes authorized and unauthorized IDs writes nothing and tells
    the caller nothing about which IDs existed.
    """
    images = list(queryset.filter(pk__in=image_ids))
    if len(images) != len(set(image_ids)):
        raise Http404("Image not found")
    return images


def non_public_images(queryset):
    """Rows of ``queryset`` whose source or collection is not public.

    ``exclude(a=True, b=True)`` drops rows where *both* hold, leaving exactly
    the rows failing at least one visibility flag.
    """
    return queryset.exclude(collection__public=True, collection__source__public=True)


def any_image_is_private(images):
    """Whether any of ``images`` comes from a non-public source or collection."""
    ids = {image.pk for image in images}
    if not ids:
        return False
    return non_public_images(Image.objects.filter(pk__in=ids)).exists()


def album_contains_private_images(album, *, extra_images=()):
    """Whether publishing ``album`` would expose a non-public image.

    ``extra_images`` lets callers test images that are about to be added but
    are not members yet, so the invariant can be checked before the write.
    """
    if non_public_images(Image.objects.filter(albums=album)).exists():
        return True

    return any_image_is_private(extra_images)


def album_for_owner_or_404(user, album_id):
    """Fetch an album owned by ``user``, or raise ``Http404``.

    Ownership is part of the queryset so somebody else's album 404s exactly as
    a nonexistent one does. The primary key is a UUID, so a malformed ID
    arrives as a ``ValidationError`` rather than a ``ValueError``.
    """
    if not user or not user.is_authenticated:
        raise Http404("Album not found")
    try:
        return Album.objects.get(pk=album_id, owner=user)
    except Album.DoesNotExist, ValidationError, TypeError, ValueError:
        raise Http404("Album not found")
