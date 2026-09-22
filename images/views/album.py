from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db import models, transaction
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from ..models import (
    Album,
    AlbumImage,
)
from ..policies import (
    album_contains_private_images,
    album_for_owner_or_404,
    any_image_is_private,
    editable_images_for,
    get_image_or_404,
    get_images_or_404,
    parse_image_ids,
)
from ..validation import InvalidInput, parse_json_body, validate_text

# An album is a public surface. Adding a non-public image to one, or flipping a
# private album that holds one to public, would republish staged material, so
# both paths refuse rather than silently disclosing.
PRIVATE_IMAGE_IN_PUBLIC_ALBUM = (
    "A public album cannot contain images from a private source or collection."
)


def edit_album(request, album_id):
    """Edit album title and description (owner only)"""
    if not request.user.is_authenticated:
        return redirect("login")

    album = get_object_or_404(Album, id=album_id)

    # Check that the user is the owner
    if album.owner != request.user:
        raise Http404("Album not found")

    if request.method == "POST":
        title = request.POST.get("title", "").strip()
        description = request.POST.get("description", "").strip()

        # Validate title
        if not title:
            messages.error(request, "Album title is required.")
            return render(
                request,
                "images/album_edit.html",
                {
                    "album": album,
                    "profile_user": album.owner,
                    "display_name": album.owner.get_display_name()
                    if hasattr(album.owner, "get_display_name")
                    else album.owner.username,
                },
            )

        if len(title) > 500:
            messages.error(request, "Album title must be 500 characters or less.")
            return render(
                request,
                "images/album_edit.html",
                {
                    "album": album,
                    "profile_user": album.owner,
                    "display_name": album.owner.get_display_name()
                    if hasattr(album.owner, "get_display_name")
                    else album.owner.username,
                },
            )

        # Update album
        album.title = title
        album.description = description
        album.map_mode = "map_mode" in request.POST
        album.save(update_fields=["title", "description", "map_mode"])

        messages.success(request, "Album updated successfully.")
        return redirect(
            "images:album_detail",
            album_id=album.id,
        )

    # GET request - display the edit form
    display_name = (
        album.owner.get_display_name()
        if hasattr(album.owner, "get_display_name")
        else album.owner.username
    )

    context = {
        "album": album,
        "profile_user": album.owner,
        "display_name": display_name,
    }

    return render(request, "images/album_edit.html", context)


def delete_album(request, album_id):
    """Delete album (owner only) - confirmation page"""
    if not request.user.is_authenticated:
        return redirect("login")

    album = get_object_or_404(Album, id=album_id)

    # Check that the user is the owner
    if album.owner != request.user:
        raise Http404("Album not found")

    if request.method == "POST":
        # Confirm deletion
        album_title = album.title
        # Get the display name (OSM username) for the redirect
        # Special case: hardcoded_admin needs to use the Django username
        if album.owner.username == "hardcoded_admin":
            album_owner_username = "hardcoded_admin"
        else:
            album_owner_username = (
                album.owner.get_display_name()
                if hasattr(album.owner, "get_display_name")
                else album.owner.username
            )
        album.delete()
        messages.success(request, f"Album '{album_title}' has been deleted.")
        return redirect("user_albums_list", username=album_owner_username)

    # GET request - display confirmation page
    display_name = (
        album.owner.get_display_name()
        if hasattr(album.owner, "get_display_name")
        else album.owner.username
    )

    context = {
        "album": album,
        "profile_user": album.owner,
        "display_name": display_name,
    }

    return render(request, "images/album_delete_confirm.html", context)


@require_http_methods(["GET"])
def user_albums_api(request):
    """API endpoint to get user's albums as JSON"""
    if not request.user.is_authenticated:
        return JsonResponse({"albums": []})

    image_id = request.GET.get("image_id")

    albums = Album.objects.filter(owner=request.user).order_by("-created_at")

    albums_data = []
    for album in albums:
        album_dict = {
            "id": str(album.id),  # Convert UUID to string for JavaScript
            "title": album.title,
            "public": album.public,
            "has_image": False,
        }
        # Check if the image is in this album
        if image_id:
            album_dict["has_image"] = album.images.filter(id=image_id).exists()
        albums_data.append(album_dict)

    return JsonResponse({"albums": albums_data})


@require_http_methods(["POST"])
def add_image_to_album(request):
    """API endpoint to add an image to an existing album"""
    if not request.user.is_authenticated:
        return JsonResponse(
            {"success": False, "error": "Not authenticated"}, status=401
        )

    try:
        data = parse_json_body(request)
    except InvalidInput as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=400)

    image_id = data.get("image_id")
    album_id = data.get("album_id")
    if not image_id or not album_id:
        return JsonResponse(
            {"success": False, "error": "Missing image_id or album_id"}, status=400
        )

    album = album_for_owner_or_404(request.user, album_id)
    image = get_image_or_404(editable_images_for(request.user), image_id)

    if album.public and album_contains_private_images(album, extra_images=[image]):
        return JsonResponse(
            {"success": False, "error": PRIVATE_IMAGE_IN_PUBLIC_ALBUM}, status=400
        )

    max_order = album.album_images.aggregate(models.Max("order"))["order__max"] or 0
    _, created = AlbumImage.objects.get_or_create(
        album=album, image=image, defaults={"order": max_order + 1}
    )

    message = (
        f"Image added to album '{album.title}'"
        if created
        else f"Image already in album '{album.title}'"
    )
    return JsonResponse({"success": True, "message": message})


@require_http_methods(["POST"])
def create_and_add_to_album(request):
    """API endpoint to create a new album and add an image to it"""
    if not request.user.is_authenticated:
        return JsonResponse(
            {"success": False, "error": "Not authenticated"}, status=401
        )

    try:
        data = parse_json_body(request)
        album_title = validate_text(
            data,
            "album_title",
            max_length=settings.ALBUM_TITLE_MAX_LENGTH,
            required=True,
        )
        album_description = validate_text(
            data,
            "album_description",
            max_length=settings.ALBUM_DESCRIPTION_MAX_LENGTH,
        )
    except InvalidInput as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=400)

    image_id = data.get("image_id")
    if not image_id:
        return JsonResponse({"success": False, "error": "Missing image_id"}, status=400)

    album_public = bool(data.get("album_public", False))
    image = get_image_or_404(editable_images_for(request.user), image_id)

    if album_public and any_image_is_private([image]):
        return JsonResponse(
            {"success": False, "error": PRIVATE_IMAGE_IN_PUBLIC_ALBUM}, status=400
        )

    with transaction.atomic():
        album = Album.objects.create(
            owner=request.user,
            title=album_title,
            description=album_description,
            public=album_public,
        )
        AlbumImage.objects.create(album=album, image=image, order=1)

    return JsonResponse(
        {
            "success": True,
            "message": f"Album '{album.title}' created and image added",
            "album_id": str(album.id),
        }
    )


@require_http_methods(["POST"])
def remove_image_from_album(request):
    """API endpoint to remove an image from an album"""
    if not request.user.is_authenticated:
        return JsonResponse(
            {"success": False, "error": "Not authenticated"}, status=401
        )

    try:
        data = parse_json_body(request)
    except InvalidInput as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=400)

    image_id = data.get("image_id")
    album_id = data.get("album_id")
    if not image_id or not album_id:
        return JsonResponse(
            {"success": False, "error": "Missing image_id or album_id"}, status=400
        )

    album = album_for_owner_or_404(request.user, album_id)

    # Deleted by ID through the owned album, so no separate image lookup is
    # needed: removing something from your own album discloses nothing, and a
    # membership row is the only thing this can touch.
    try:
        deleted_count, _ = AlbumImage.objects.filter(
            album=album, image_id=int(image_id)
        ).delete()
    except TypeError, ValueError:
        return JsonResponse({"success": False, "error": "Invalid image_id"}, status=400)

    if not deleted_count:
        return JsonResponse(
            {"success": False, "error": "Image was not in this album"}, status=400
        )

    return JsonResponse(
        {"success": True, "message": f"Image removed from album '{album.title}'"}
    )


def album_detail(request, album_id):
    """Display a specific album with its images"""

    album = get_object_or_404(Album, id=album_id)
    user = album.owner

    # Check permissions - only show if public or user is viewing their own
    if not album.public and (not request.user.is_authenticated or request.user != user):
        raise Http404("Album not found")

    # Get images in album order
    album_images = album.album_images.select_related("image").order_by("order")

    # Extract the actual Image objects from AlbumImage objects
    images = [ai.image for ai in album_images]

    # Calculate pending images count for the georeference button
    pending_images = sum(1 for image in images if not image.is_georeferenced)

    # Calculate georeferenced images count for map display
    georeferenced_images = sum(1 for image in images if image.is_georeferenced)

    # Paginate images for browsing
    paginator = Paginator(images, 24)  # 24 images per page for grid layout
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    # Check if map_mode is available (handle migration period)
    album_map_mode = getattr(album, "map_mode", False)

    display_name = (
        user.get_display_name() if hasattr(user, "get_display_name") else user.username
    )
    is_owner = request.user.is_authenticated and request.user == user

    context = {
        "album": album,
        "page_obj": page_obj,
        "profile_user": user,
        "display_name": display_name,
        "is_owner": is_owner,
        "pending_images": pending_images,
        "georeferenced_images": georeferenced_images,
        "album_map_mode": album_map_mode,
    }
    return render(request, "images/album_detail.html", context)


@require_http_methods(["POST"])
def toggle_album_public(request, album_id):
    """Toggle album public/private status (owner only)"""
    if not request.user.is_authenticated:
        return JsonResponse(
            {"success": False, "error": "Authentication required"}, status=401
        )

    try:
        data = parse_json_body(request)
    except InvalidInput as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=400)

    new_public_status = bool(data.get("public", True))
    album = album_for_owner_or_404(request.user, album_id)

    # Publishing an album publishes everything in it. Staff may legitimately
    # stage private images in a private album, so the check happens here, at
    # the moment the album would become a public surface.
    if new_public_status and album_contains_private_images(album):
        return JsonResponse(
            {"success": False, "error": PRIVATE_IMAGE_IN_PUBLIC_ALBUM}, status=400
        )

    album.public = new_public_status
    album.save(update_fields=["public", "updated_at"])

    return JsonResponse({"success": True, "public": album.public})


@require_http_methods(["POST"])
def bulk_add_to_album(request):
    """Add multiple images to an album in a single request"""
    if not request.user.is_authenticated:
        return JsonResponse(
            {"success": False, "error": "Not authenticated"}, status=401
        )

    try:
        data = parse_json_body(request)
        image_ids = parse_image_ids(data.get("image_ids"))
    except (InvalidInput, ValueError) as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=400)

    album_id = data.get("album_id")
    if not album_id:
        return JsonResponse({"success": False, "error": "Missing album_id"}, status=400)

    album = album_for_owner_or_404(request.user, album_id)

    # All or nothing: every requested ID must resolve through the policy before
    # anything is written, so a request mixing authorized and unauthorized IDs
    # leaves no partial membership behind and reveals nothing about which IDs
    # exist.
    images = get_images_or_404(editable_images_for(request.user), image_ids)

    if album.public and album_contains_private_images(album, extra_images=images):
        return JsonResponse(
            {"success": False, "error": PRIVATE_IMAGE_IN_PUBLIC_ALBUM}, status=400
        )

    added_count = 0
    with transaction.atomic():
        next_order = (
            album.album_images.aggregate(models.Max("order"))["order__max"] or 0
        ) + 1
        for image in images:
            _, created = AlbumImage.objects.get_or_create(
                album=album, image=image, defaults={"order": next_order}
            )
            if created:
                added_count += 1
                next_order += 1

        album.save(update_fields=["updated_at"])

    return JsonResponse(
        {
            "success": True,
            "message": (
                f'Successfully added {added_count} images to album "{album.title}"'
            ),
            "added_count": added_count,
            "total_requested": len(image_ids),
            "album_title": album.title,
            "album_id": str(album.id),
        }
    )


@require_http_methods(["POST"])
def bulk_create_and_add_to_album(request):
    """Create a new album and add multiple images to it in a single request"""
    if not request.user.is_authenticated:
        return JsonResponse(
            {"success": False, "error": "Not authenticated"}, status=401
        )

    try:
        data = parse_json_body(request)
        title = validate_text(
            data, "title", max_length=settings.ALBUM_TITLE_MAX_LENGTH, required=True
        )
        description = validate_text(
            data, "description", max_length=settings.ALBUM_DESCRIPTION_MAX_LENGTH
        )
        image_ids = parse_image_ids(data.get("image_ids"))
    except (InvalidInput, ValueError) as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=400)

    is_public = bool(data.get("is_public", False))

    # Authorize the whole set before creating anything, so a rejected request
    # does not leave an orphan album behind the way the old clean-up path did.
    images = get_images_or_404(editable_images_for(request.user), image_ids)

    if is_public and any_image_is_private(images):
        return JsonResponse(
            {"success": False, "error": PRIVATE_IMAGE_IN_PUBLIC_ALBUM}, status=400
        )

    with transaction.atomic():
        album = Album.objects.create(
            owner=request.user,
            title=title,
            description=description,
            public=is_public,
        )
        AlbumImage.objects.bulk_create(
            [
                AlbumImage(album=album, image=image, order=order)
                for order, image in enumerate(images, 1)
            ]
        )

    return JsonResponse(
        {
            "success": True,
            "message": (
                f'Successfully created album "{album.title}" '
                f"and added {len(images)} images"
            ),
            "album": {
                "id": str(album.id),
                "title": album.title,
                "description": album.description,
                "public": album.public,
                "created_at": album.created_at.isoformat(),
                "image_count": len(images),
            },
            "added_count": len(images),
            "total_requested": len(image_ids),
        }
    )
