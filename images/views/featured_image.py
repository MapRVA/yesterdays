import datetime
import json

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from images.models import Image, ImageOfTheDay
from regions.context_processors import get_current_region
from regions.models import Region


@staff_member_required
def featured_image_queue(request):
    """Staff-facing list of images queued for the Image of the Day.

    Queues are per region, so this shows the queue for whichever region is
    selected in the navbar; with none selected there is no queue to show.
    """
    today = timezone.localdate()
    region = get_current_region(request)
    entries = None
    days_queued = 0
    if region is not None:
        entries = list(
            ImageOfTheDay.objects.select_related("image", "image__collection", "user")
            .filter(region=region, day__gte=today)
            .order_by("day")
        )
        # How many days beyond today are covered without a gap. Today itself
        # isn't counted, but the run has to start there: a queue with a hole
        # today has nothing lined up, however many later days are filled.
        for entry in entries:
            if entry.day != today + datetime.timedelta(days=days_queued):
                break
            days_queued += 1
        days_queued = max(days_queued - 1, 0)
    context = {
        "entries": entries,
        "region": region,
        "today": today,
        "days_queued": days_queued,
    }
    return render(request, "images/featured_image_queue.html", context)


@require_http_methods(["POST"])
@staff_member_required
def queue_featured_image(request, image_id):
    """Queue an image for the Image of the Day, optionally on a chosen date.

    With no date, the image is appended to the next open day in the queue.
    """
    image = get_object_or_404(Image, id=image_id)

    try:
        data = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return JsonResponse(
            {"success": False, "error": "Invalid JSON in request body"}, status=400
        )

    # Each region curates its own queue, so which one this entry joins is an
    # explicit choice — the modal defaults it to the image's region, but staff
    # can queue an image anywhere.
    region_slug = (data.get("region") or "").strip()
    if not region_slug:
        return JsonResponse(
            {"success": False, "error": "Choose a region for this image."}, status=400
        )
    region = Region.objects.filter(slug=region_slug).first()
    if region is None:
        return JsonResponse({"success": False, "error": "Unknown region."}, status=400)

    day = None
    day_str = (data.get("day") or "").strip()
    if day_str:
        try:
            day = datetime.date.fromisoformat(day_str)
        except ValueError:
            return JsonResponse(
                {"success": False, "error": "Invalid date format."}, status=400
            )

    note = (data.get("note") or "").strip() or None
    if note and len(note) > 500:
        return JsonResponse(
            {"success": False, "error": "Note must be 500 characters or fewer."},
            status=400,
        )

    # Only attribute the entry to the submitter if they opted in to being
    # publicly quoted; otherwise keep it anonymous.
    user = request.user if data.get("quote_me") else None

    try:
        # Choosing an explicit date pins the image to that day (locks it);
        # leaving it blank appends to the next open day, unlocked.
        entry = ImageOfTheDay.place(
            image, region, day=day, locked=day is not None, note=note, user=user
        )
    except ValidationError as e:
        return JsonResponse(
            {"success": False, "error": " ".join(e.messages)}, status=400
        )

    return JsonResponse(
        {
            "success": True,
            "day": entry.day.isoformat(),
            "message": f"Queued for {entry.day:%b %d, %Y} in {region.short_name}.",
        },
        status=201,
    )


@staff_member_required
def user_autocomplete(request):
    """Search users for the featured image reassignment dropdown."""
    query = request.GET.get("q", "").strip()
    if len(query) < 2:
        return JsonResponse([], safe=False)

    users = User.objects.filter(
        Q(first_name__icontains=query) | Q(username__icontains=query)
    ).order_by("first_name", "username")[:10]

    results = [
        {
            "id": u.id,
            "name": u.get_display_name(),
            "username": u.username,
            "picture": u.get_profile_picture_url(),
        }
        for u in users
    ]
    return JsonResponse(results, safe=False)


@staff_member_required
def featured_image_edit(request, pk):
    """Edit a queued entry's note and reassign its user."""
    entry = get_object_or_404(
        ImageOfTheDay.objects.select_related("image", "user", "region"), pk=pk
    )

    if request.method == "POST":
        note = (request.POST.get("note") or "").strip()
        if len(note) > 500:
            messages.error(request, "Note must be 500 characters or fewer.")
            return redirect("images:featured_image_edit", pk=entry.pk)

        user_id = (request.POST.get("user_id") or "").strip()
        new_user = None
        if user_id:
            new_user = User.objects.filter(pk=user_id).first()
            if new_user is None:
                messages.error(request, "Selected user could not be found.")
                return redirect("images:featured_image_edit", pk=entry.pk)

        note = note or None
        wants_locked = bool(request.POST.get("locked"))

        if wants_locked:
            # The date field is only enabled (and submitted) while locking.
            day_str = (request.POST.get("day") or "").strip()
            new_day = entry.day
            if day_str:
                try:
                    new_day = datetime.date.fromisoformat(day_str)
                except ValueError:
                    messages.error(request, "Invalid date format.")
                    return redirect("images:featured_image_edit", pk=entry.pk)

            if new_day != entry.day:
                # Pin to a different day; move() claims it and locks.
                try:
                    ImageOfTheDay.move(entry, new_day, note=note, user=new_user)
                except ValidationError as e:
                    messages.error(request, " ".join(e.messages))
                    return redirect("images:featured_image_edit", pk=entry.pk)
            else:
                # Lock (or keep locked) on the current day.
                entry.note = note
                entry.user = new_user
                entry.locked = True
                entry.save(update_fields=["note", "user", "locked", "updated"])
        else:
            # Unlocking frees the image from a specific date: it rejoins the
            # flow and slides to the first open day in the queue.
            ImageOfTheDay.unlock(entry, note=note, user=new_user)

        messages.success(request, "Queue entry updated.")
        return redirect("images:featured_image_queue")

    return render(request, "images/featured_image_edit.html", {"entry": entry})


@require_http_methods(["POST"])
@staff_member_required
def featured_image_delete(request, pk):
    """Remove a queued entry, sliding the upcoming queue back to close the gap."""
    entry = get_object_or_404(ImageOfTheDay, pk=pk)
    # This is an explicit, confirmed removal, so unlock a locked anchor first
    # (delete() refuses to remove a locked entry directly).
    if entry.locked:
        ImageOfTheDay.objects.filter(pk=entry.pk).update(locked=False)
        entry.locked = False
    entry.delete()
    messages.success(request, "Removed from the queue.")
    return redirect("images:featured_image_queue")
