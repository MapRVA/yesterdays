import json
import logging
import mimetypes
import os
import uuid as uuid_mod

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.db import transaction
from django.db.models import Count, Max
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods
from iiif_prezi3 import (
    Annotation,
    AnnotationBody,
    AnnotationPage,
    Canvas,
    Collection,
    Manifest,
    ServiceV3,
)

from images.utils import R2Uploader
from subjects.models import Business, Occupation, Person

from .models import (
    ADDRESS_FIELDS,
    Address,
    Directory,
    Entry,
    EntryBusinessLink,
    EntryComment,
    EntryHistory,
    EntryOccupationLink,
    EntryPersonLink,
    LinkMethod,
    OCRModel,
    Page,
)

logger = logging.getLogger(__name__)


def _build_entry_snapshot(entry):
    """Build a dict snapshot of an Entry and its linked records."""
    snapshot = {"original_text": entry.original_text}

    person_link = entry.person_links.first()
    if person_link:
        p = person_link.person
        snapshot["first_name"] = p.first_name
        snapshot["middle_name"] = p.middle_name
        snapshot["last_name"] = p.last_name

    addresses = []
    for a in entry.addresses.all():
        addr_snap = {f: getattr(a, f) for f in ADDRESS_FIELDS if getattr(a, f)}
        if a.type:
            addr_snap["type"] = a.type
        if addr_snap:
            addresses.append(addr_snap)
    if addresses:
        snapshot["addresses"] = addresses

    business_link = entry.business_links.first()
    if business_link:
        snapshot["business"] = business_link.business.name

    occupation_link = entry.occupation_links.first()
    if occupation_link:
        snapshot["occupation"] = occupation_link.occupation.name

    return snapshot


def _extract_addresses(entry_data):
    """Parse address(es) from an entry payload, accepting either shape.

    Two payload shapes are supported, so OCR producers and clients can use
    whichever is convenient:

    - **Nested** ``addresses=[{type, housenumber, street, …}, …]`` — multiple
      addresses per entry, with optional ``type`` labels.
    - **Flat** ``addr:housenumber``, ``addr:street``, … keys at the top
      level — yields a single address with empty ``type``.

    Returns a list of dicts suitable for ``Address.objects.create(entry=…, **d)``.
    """
    if isinstance(entry_data.get("addresses"), list):
        out = []
        for addr in entry_data["addresses"]:
            if not isinstance(addr, dict):
                continue
            fields = {
                f: str(addr.get(f, "")).strip() for f in ADDRESS_FIELDS if addr.get(f)
            }
            addr_type = str(addr.get("type", "") or "").strip()
            if fields or addr_type:
                out.append({"type": addr_type, **fields})
        return out

    fields = {
        key[5:]: str(val).strip()
        for key, val in entry_data.items()
        if key.startswith("addr:") and key[5:] in ADDRESS_FIELDS and val
    }
    if fields:
        return [{"type": "", **fields}]
    return []


def _queue_tile_generation(page):
    """Queue IIIF tile generation, setting status to pending on success."""
    from .tasks import generate_iiif_tiles

    def _dispatch():
        try:
            generate_iiif_tiles.apply_async(args=[page.id])
        except Exception:
            logger.exception("Failed to queue tile generation for page %s", page.uuid)

    transaction.on_commit(_dispatch)


def entry_validate(request, entry_uuid=None):
    """Show a specific Entry for validation, or pick one at random."""
    qs = (
        Entry.objects.filter(x__isnull=False)
        .select_related("page__directory")
        .prefetch_related(
            "person_links__person",
            "addresses",
            "business_links__business",
            "occupation_links__occupation",
        )
    )
    if entry_uuid is not None:
        entry = get_object_or_404(qs, uuid=entry_uuid)
    else:
        entry = qs.order_by("?").first()

    if entry is None:
        return render(request, "directories/entry_detail.html", {"entry": None})

    page = entry.page

    iiif_info_url = None
    if page.tile_status == Page.TileStatus.COMPLETE and page.width and page.height:
        iiif_info_url = (
            f"{settings.R2_PUBLIC_URL_BASE}/directory/{page.uuid}/iiif/info.json"
        )

    entry_data = {
        "uuid": str(entry.uuid),
        "original_text": entry.original_text,
        "x": entry.x,
        "y": entry.y,
        "w": entry.w,
        "h": entry.h,
    }

    # Flatten linked records into the entry dict
    person_link = entry.person_links.first()
    if person_link:
        p = person_link.person
        entry_data["first_name"] = p.first_name
        entry_data["middle_name"] = p.middle_name
        entry_data["last_name"] = p.last_name

    addresses_list = []
    for a in entry.addresses.all():
        addr_dict = {"type": a.type or ""}
        for field in ADDRESS_FIELDS:
            val = getattr(a, field, "")
            if val:
                addr_dict[field] = val
        addresses_list.append(addr_dict)
    entry_data["addresses"] = addresses_list

    # Backward-compat: surface the first address as flat addr:* keys so the
    # existing single-address validation UI keeps working without changes.
    first_addr = entry.addresses.first()
    if first_addr:
        for field in ADDRESS_FIELDS:
            val = getattr(first_addr, field, "")
            if val:
                entry_data[f"addr:{field}"] = val

    business_link = entry.business_links.first()
    if business_link:
        entry_data["business"] = business_link.business.name

    occupation_link = entry.occupation_links.first()
    if occupation_link:
        entry_data["occupation"] = occupation_link.occupation.name

    # Build interleaved timeline from history + comments
    history = list(
        entry.history.select_related("user").values(
            "action", "snapshot", "created_at", "user__username"
        )
    )
    comments = list(
        entry.comments.select_related("user").values(
            "text", "created_at", "user__username"
        )
    )
    timeline = []
    for h in history:
        timeline.append(
            {
                "type": h["action"],
                "label": dict(EntryHistory.Action.choices).get(
                    h["action"], h["action"]
                ),
                "user": h["user__username"],
                "time": h["created_at"],
                "snapshot": h["snapshot"],
            }
        )
    for c in comments:
        timeline.append(
            {
                "type": "comment",
                "label": "Comment",
                "user": c["user__username"],
                "time": c["created_at"],
                "text": c["text"],
            }
        )
    timeline.sort(key=lambda t: t["time"])

    # Check if current user already approved or edited the current version
    already_approved = False
    if request.user.is_authenticated:
        last_change_by_others = (
            entry.history.exclude(user=request.user).order_by("-created_at").first()
        )
        last_action_by_user = (
            entry.history.filter(user=request.user).order_by("-created_at").first()
        )
        if last_action_by_user:
            if (
                last_change_by_others is None
                or last_action_by_user.created_at >= last_change_by_others.created_at
            ):
                already_approved = True

    # Serialize timeline for Alpine.js
    timeline_json_data = []
    for t in timeline:
        timeline_json_data.append(
            {
                "type": t["type"],
                "label": t["label"],
                "user": t.get("user", ""),
                "time": t["time"].isoformat(),
                "text": t.get("text", ""),
            }
        )

    return render(
        request,
        "directories/entry_detail.html",
        {
            "entry": entry,
            "entry_json": json.dumps(entry_data),
            "page": page,
            "directory": page.directory,
            "iiif_info_url": iiif_info_url,
            "timeline_json": json.dumps(timeline_json_data),
            "already_approved": already_approved,
        },
    )


@staff_member_required
@require_http_methods(["POST"])
def entry_update(request, entry_uuid):
    """Update a single Entry and its linked records."""
    entry = get_object_or_404(
        Entry.objects.prefetch_related(
            "person_links__person",
            "addresses",
            "business_links__business",
            "occupation_links__occupation",
        ),
        uuid=entry_uuid,
    )

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError, KeyError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    with transaction.atomic():
        entry.original_text = data.get("original_text", entry.original_text)
        entry.save(update_fields=["original_text", "updated_at"])

        # Update or create person
        user = request.user if request.user.is_authenticated else None
        person_fields = {
            k: str(data[k]).strip()
            for k in ("first_name", "middle_name", "last_name")
            if data.get(k) is not None
        }
        if person_fields:
            link = entry.person_links.first()
            if link:
                for k, v in person_fields.items():
                    setattr(link.person, k, v)
                link.person.save()
            else:
                person = Person.objects.create(**person_fields)
                EntryPersonLink.objects.create(
                    entry=entry,
                    person=person,
                    method=LinkMethod.USER,
                    created_by=user,
                )

        # Update addresses. Two payload shapes are accepted:
        #   - Nested ``addresses=[…]`` fully replaces the entry's addresses.
        #   - Flat ``addr:*`` keys update the first address in place,
        #     preserving any additional ones — this keeps the existing
        #     single-address validation UI working unchanged.
        if isinstance(data.get("addresses"), list):
            entry.addresses.all().delete()
            for addr_payload in _extract_addresses(data):
                Address.objects.create(entry=entry, **addr_payload)
        else:
            addr_keys_present = any(
                key.startswith("addr:") and key[5:] in ADDRESS_FIELDS for key in data
            )
            if addr_keys_present:
                addr_fields = {
                    key[5:]: str(val).strip()
                    for key, val in data.items()
                    if key.startswith("addr:") and key[5:] in ADDRESS_FIELDS
                }
                first = entry.addresses.first()
                if first:
                    for k, v in addr_fields.items():
                        setattr(first, k, v)
                    first.save()
                elif any(addr_fields.values()):
                    Address.objects.create(entry=entry, **addr_fields)

        # Update or create business
        if "business" in data:
            biz_name = str(data["business"]).strip()
            link = entry.business_links.first()
            if link:
                link.business.name = biz_name
                link.business.save()
            else:
                business, _ = Business.objects.get_or_create(name=biz_name)
                EntryBusinessLink.objects.create(
                    entry=entry,
                    business=business,
                    method=LinkMethod.USER,
                    created_by=user,
                )

        # Update or create occupation
        if "occupation" in data:
            occ_name = str(data["occupation"]).strip()
            link = entry.occupation_links.first()
            if link:
                link.occupation.name = occ_name
                link.occupation.save()
            else:
                occupation, _ = Occupation.objects.get_or_create(name=occ_name)
                EntryOccupationLink.objects.create(
                    entry=entry,
                    occupation=occupation,
                    method=LinkMethod.USER,
                    created_by=user,
                )

        # Refresh prefetched relations before building snapshot
        entry = Entry.objects.prefetch_related(
            "person_links__person",
            "addresses",
            "business_links__business",
            "occupation_links__occupation",
        ).get(pk=entry.pk)

        action = (
            EntryHistory.Action.EDITED
            if data.get("edited")
            else EntryHistory.Action.APPROVED
        )
        EntryHistory.objects.create(
            entry=entry,
            action=action,
            snapshot=_build_entry_snapshot(entry),
            user=request.user if request.user.is_authenticated else None,
        )

    return JsonResponse({"success": True})


@require_http_methods(["POST"])
def add_entry_comment(request, entry_uuid):
    """API endpoint to add a comment to an entry."""
    if not request.user.is_authenticated:
        return JsonResponse(
            {"success": False, "error": "Authentication required"}, status=401
        )

    entry = get_object_or_404(Entry, uuid=entry_uuid)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    text = data.get("text", "").strip()
    if not text:
        return JsonResponse(
            {"success": False, "error": "Comment text is required"}, status=400
        )

    comment = EntryComment.objects.create(entry=entry, user=request.user, text=text)

    return JsonResponse({"success": True, "comment_id": comment.id}, status=201)


def directory_list(request):
    directories = Directory.objects.all()
    return render(
        request,
        "directories/directory_list.html",
        {"directories": directories},
    )


def directory_view(request, slug):
    directory = get_object_or_404(Directory, slug=slug)
    manifest_url = request.build_absolute_uri(
        f"/directories/{directory.slug}/manifest.json"
    )

    # Build entries keyed by TIFY page number (1-based)
    pages = directory.pages.filter(
        tile_status=Page.TileStatus.COMPLETE,
        width__isnull=False,
        height__isnull=False,
    ).order_by("order")
    page_list = list(pages.values_list("id", "uuid"))
    page_ids = [p[0] for p in page_list]
    page_uuids = [str(p[1]) for p in page_list]

    entries = Entry.objects.filter(page_id__in=page_ids).prefetch_related(
        "person_links__person",
        "addresses",
        "business_links__business",
        "occupation_links__occupation",
        "history",
    )

    entries_by_page = {}
    for entry in entries:
        page_index = page_ids.index(entry.page_id) + 1  # 1-based page number
        entry_data = {
            "id": entry.id,
            "uuid": str(entry.uuid),
            "original_text": entry.original_text,
        }
        if entry.x is not None:
            entry_data["x"] = entry.x
            entry_data["y"] = entry.y
            entry_data["w"] = entry.w
            entry_data["h"] = entry.h
        for link in entry.person_links.all():
            p = link.person
            entry_data["person"] = {
                "first_name": p.first_name,
                "middle_name": p.middle_name,
                "last_name": p.last_name,
                "birth_date": p.birth_date,
            }
        addresses = list(entry.addresses.all())
        if addresses:
            entry_data["addresses"] = [
                {"type": a.type or "", "text": str(a)} for a in addresses
            ]
        for link in entry.business_links.all():
            entry_data["business"] = link.business.name
        for link in entry.occupation_links.all():
            entry_data["occupation"] = link.occupation.name
        entry_data["approved"] = any(
            h.action in (EntryHistory.Action.APPROVED, EntryHistory.Action.EDITED)
            for h in entry.history.all()
        )
        entries_by_page.setdefault(page_index, []).append(entry_data)

    return render(
        request,
        "directories/directory_view.html",
        {
            "directory": directory,
            "manifest_url": manifest_url,
            "entries_json": json.dumps(entries_by_page),
            "page_uuids_json": json.dumps(page_uuids),
        },
    )


@staff_member_required
def page_ocr(request, page_uuid):
    page = get_object_or_404(Page, uuid=page_uuid)
    directory = page.directory

    iiif_info_url = None
    if page.tile_status == Page.TileStatus.COMPLETE and page.width and page.height:
        iiif_info_url = (
            f"{settings.R2_PUBLIC_URL_BASE}/directory/{page.uuid}/iiif/info.json"
        )

    entries_locked = page.entries.exists()

    return render(
        request,
        "directories/page_ocr.html",
        {
            "page": page,
            "directory": directory,
            "ocr_prompt": directory.ocr_prompt,
            "ocr_models": OCRModel.objects.all(),
            "iiif_info_url": iiif_info_url,
            "entries_locked": entries_locked,
        },
    )


@staff_member_required
@require_http_methods(["POST"])
def run_ocr(request, page_uuid):
    from .tasks import run_page_ocr

    page = get_object_or_404(Page, uuid=page_uuid)

    if page.entries.exists():
        return JsonResponse(
            {
                "error": "Entries have already been saved for this page. Edit them individually instead of re-running OCR."
            },
            status=409,
        )

    data = json.loads(request.body)
    prompt = data.get("prompt", "").strip()
    model_identifier = data.get("model", "").strip()

    if not prompt:
        return JsonResponse({"error": "Prompt is required"}, status=400)
    if not model_identifier:
        return JsonResponse({"error": "Model is required"}, status=400)
    if not OCRModel.objects.filter(identifier=model_identifier).exists():
        return JsonResponse({"error": "Invalid model"}, status=400)

    Page.objects.filter(pk=page.pk).update(
        ocr_status="pending", ocr_error="", ocr_raw=""
    )
    Directory.objects.filter(pk=page.directory_id).update(ocr_prompt=prompt)
    run_page_ocr.delay(page.id, prompt, model_identifier)

    return JsonResponse({"success": True})


@staff_member_required
@require_http_methods(["GET"])
def ocr_status(request, page_uuid):
    page = get_object_or_404(Page, uuid=page_uuid)

    result = {
        "ocr_status": page.ocr_status,
        "ocr_error": page.ocr_error,
    }

    if page.ocr_raw:
        try:
            result["ocr_raw"] = json.loads(page.ocr_raw)
        except json.JSONDecodeError, ValueError:
            result["ocr_raw"] = page.ocr_raw

    result["has_saved_entries"] = page.entries.exists()

    return JsonResponse(result)


def _delete_page_entries(page):
    """Delete all entries for a page and clean up orphaned Person records.

    Addresses cascade with their Entry — no orphan cleanup needed.
    """
    entries = Entry.objects.filter(page=page).prefetch_related("person_links")
    person_ids = []
    for entry in entries:
        person_ids.extend(link.person_id for link in entry.person_links.all())
    entries.delete()
    if person_ids:
        Person.objects.filter(pk__in=person_ids, entry_links__isnull=True).delete()


@staff_member_required
@require_http_methods(["POST"])
def save_entries(request, page_uuid):
    page = get_object_or_404(Page, uuid=page_uuid)

    try:
        data = json.loads(request.body)
        submitted = data.get("entries", [])
        if not isinstance(submitted, list):
            return JsonResponse({"error": "entries must be a list"}, status=400)
    except json.JSONDecodeError, KeyError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    with transaction.atomic():
        _delete_page_entries(page)

        count = 0
        for entry_data in submitted:
            original_text = str(entry_data.get("original_text", "")).strip()
            bbox = {}
            for dim in ("x", "y", "w", "h"):
                val = entry_data.get(dim)
                if val is not None:
                    try:
                        bbox[dim] = int(val)
                    except ValueError, TypeError:
                        pass
            entry = Entry.objects.create(page=page, original_text=original_text, **bbox)

            person_fields = {
                k: str(entry_data[k]).strip()
                for k in ("first_name", "middle_name", "last_name")
                if entry_data.get(k)
            }
            if person_fields:
                person = Person.objects.create(**person_fields)
                EntryPersonLink.objects.create(
                    entry=entry,
                    person=person,
                    method=LinkMethod.OCR,
                )

            for addr_payload in _extract_addresses(entry_data):
                Address.objects.create(entry=entry, **addr_payload)

            if entry_data.get("business"):
                biz_name = str(entry_data["business"]).strip()
                business, created = Business.objects.get_or_create(name=biz_name)
                EntryBusinessLink.objects.create(
                    entry=entry,
                    business=business,
                    method=LinkMethod.OCR if created else LinkMethod.AUTO,
                )

            if entry_data.get("occupation"):
                occ_name = str(entry_data["occupation"]).strip()
                occupation, created = Occupation.objects.get_or_create(name=occ_name)
                EntryOccupationLink.objects.create(
                    entry=entry,
                    occupation=occupation,
                    method=LinkMethod.OCR if created else LinkMethod.AUTO,
                )

            EntryHistory.objects.create(
                entry=entry,
                action=EntryHistory.Action.OCR_CREATED,
                snapshot=entry_data,
            )

            count += 1

    return JsonResponse({"success": True, "entry_count": count})


@staff_member_required
def directory_create(request):
    if request.method == "POST":
        title = request.POST.get("title", "").strip()
        slug = request.POST.get("slug", "").strip()
        description = request.POST.get("description", "").strip()

        errors = []
        if not title:
            errors.append("Title is required.")
        if not slug:
            errors.append("Slug is required.")
        elif Directory.objects.filter(slug=slug).exists():
            errors.append("A directory with this slug already exists.")

        if errors:
            return render(
                request,
                "directories/directory_create.html",
                {
                    "errors": errors,
                    "title": title,
                    "slug": slug,
                    "description": description,
                },
            )

        directory = Directory.objects.create(
            title=title,
            slug=slug,
            description=description,
        )
        return redirect("directories:directory_edit", slug=directory.slug)

    return render(request, "directories/directory_create.html")


@staff_member_required
@ensure_csrf_cookie
def directory_edit(request, slug):
    directory = get_object_or_404(Directory, slug=slug)

    # Backfill sequential order values when pages share duplicate orders
    # (e.g. all defaulting to 0), so new uploads append correctly.
    page_qs = directory.pages.filter(image_url__gt="").order_by("order", "created_at")
    distinct_orders = page_qs.values_list("order", flat=True).distinct().count()
    total_pages = page_qs.count()
    if total_pages > 1 and distinct_orders < total_pages:
        for i, page in enumerate(page_qs):
            if page.order != i:
                page.order = i
                page.save(update_fields=["order", "updated_at"])

    pages = (
        directory.pages.filter(image_url__gt="")
        .order_by("order")
        .annotate(entry_count=Count("entries"))
    )
    return render(
        request,
        "directories/directory_edit.html",
        {
            "directory": directory,
            "pages": pages,
            "ocr_models": OCRModel.objects.all(),
        },
    )


@staff_member_required
@require_http_methods(["POST"])
def presign_upload(request, slug):
    directory = get_object_or_404(Directory, slug=slug)
    data = json.loads(request.body)
    filename = data.get("filename", "")

    if not filename:
        return JsonResponse({"error": "filename is required"}, status=400)

    ext = os.path.splitext(filename)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"):
        return JsonResponse({"error": f"Unsupported file type: {ext}"}, status=400)

    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    # Generate a UUID for this page — no DB record yet
    page_uuid = uuid_mod.uuid4()
    key = f"directory/{page_uuid}/original{ext}"

    uploader = R2Uploader()
    presigned_url = uploader.generate_presigned_put_url(key, content_type)
    public_url = uploader.get_public_url(key)

    return JsonResponse(
        {
            "upload_url": presigned_url,
            "content_type": content_type,
            "page_uuid": str(page_uuid),
            "public_url": public_url,
            "filename": filename,
        }
    )


@staff_member_required
@require_http_methods(["POST"])
def confirm_upload(request, slug):
    directory = get_object_or_404(Directory, slug=slug)
    data = json.loads(request.body)
    page_uuid = data.get("page_uuid", "")
    public_url = data.get("public_url", "")
    filename = data.get("filename", "")

    if not page_uuid or not public_url:
        return JsonResponse(
            {"error": "page_uuid and public_url are required"}, status=400
        )

    # Determine next order value
    max_order = directory.pages.aggregate(max_order=Max("order"))["max_order"]
    next_order = (max_order or -1) + 1

    page = Page.objects.create(
        uuid=page_uuid,
        directory=directory,
        order=next_order,
        original_filename=filename,
        image_url=public_url,
        tile_status=Page.TileStatus.PENDING,
    )

    # Queue IIIF tile generation after transaction commits
    _queue_tile_generation(page)

    return JsonResponse(
        {
            "success": True,
            "page_uuid": str(page.uuid),
            "page_order": page.order,
            "tile_status": page.tile_status,
        }
    )


@staff_member_required
@require_http_methods(["POST"])
def reorder_pages(request, slug):
    directory = get_object_or_404(Directory, slug=slug)
    data = json.loads(request.body)
    page_ids = data.get("page_ids", [])

    if not page_ids:
        return JsonResponse({"error": "page_ids is required"}, status=400)

    pages = {str(p.uuid): p for p in directory.pages.all()}
    for i, page_id in enumerate(page_ids):
        if page_id in pages:
            pages[page_id].order = i
            pages[page_id].save(update_fields=["order", "updated_at"])

    return JsonResponse({"success": True})


@staff_member_required
@require_http_methods(["DELETE"])
def delete_page(request, page_uuid):
    page = get_object_or_404(Page, uuid=page_uuid)

    # Delete from R2 if uploaded
    if page.image_url:
        try:
            ext = os.path.splitext(page.original_filename)[1].lower() or ".jpg"
            key = f"directory/{page.uuid}/original{ext}"
            uploader = R2Uploader()
            uploader.delete_file(key)
        except Exception:
            pass  # Don't block deletion if R2 cleanup fails

    page.delete()
    return JsonResponse({"success": True})


@staff_member_required
@require_http_methods(["GET"])
def page_statuses(request, slug):
    directory = get_object_or_404(Directory, slug=slug)
    pages = directory.pages.order_by("order").values(
        "uuid",
        "tile_status",
        "tile_error",
        "width",
        "height",
        "ocr_status",
        "ocr_error",
    )
    statuses = {
        str(p["uuid"]): {
            "tile_status": p["tile_status"],
            "tile_error": p["tile_error"],
            "width": p["width"],
            "height": p["height"],
            "ocr_status": p["ocr_status"],
            "ocr_error": p["ocr_error"],
        }
        for p in pages
    }
    return JsonResponse(statuses)


@staff_member_required
@require_http_methods(["POST"])
def queue_tiles(request, page_uuid):
    page = get_object_or_404(Page, uuid=page_uuid)

    if page.tile_status in (Page.TileStatus.PROCESSING,):
        return JsonResponse({"error": "Tiles are already being generated"}, status=409)

    Page.objects.filter(pk=page.pk).update(
        tile_status=Page.TileStatus.PENDING,
        tile_error="",
    )

    _queue_tile_generation(page)

    return JsonResponse({"success": True, "tile_status": Page.TileStatus.PENDING})


@staff_member_required
@require_http_methods(["POST"])
def queue_ocr_remaining(request, slug):
    """Queue OCR for all pages in a directory that haven't been OCR'd yet."""
    from .tasks import run_page_ocr

    directory = get_object_or_404(Directory, slug=slug)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError, KeyError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    model_identifier = data.get("model", "").strip()
    if not model_identifier:
        return JsonResponse({"error": "Model is required"}, status=400)
    if not OCRModel.objects.filter(identifier=model_identifier).exists():
        return JsonResponse({"error": "Invalid model"}, status=400)

    prompt = directory.ocr_prompt
    if not prompt:
        return JsonResponse(
            {"error": "No OCR prompt configured for this directory"}, status=400
        )

    pages = (
        directory.pages.filter(image_url__gt="")
        .exclude(ocr_status__in=["pending", "processing", "complete"])
        .annotate(entry_count=Count("entries"))
        .filter(entry_count=0)
    )

    count = 0
    for page in pages:
        Page.objects.filter(pk=page.pk).update(
            ocr_status="pending", ocr_error="", ocr_raw=""
        )
        run_page_ocr.delay(page.id, prompt, model_identifier)
        count += 1

    return JsonResponse({"success": True, "queued": count})


IIIF_CONTENT_TYPE = (
    'application/ld+json;profile="http://iiif.io/api/presentation/3/context.json"'
)


def _iiif_response(body):
    response = HttpResponse(body, content_type=IIIF_CONTENT_TYPE)
    response["Access-Control-Allow-Origin"] = "*"
    response["Cache-Control"] = "public, max-age=60"
    return response


def _build_canvas(page, manifest_uri, entries=None):
    """Build a IIIF Canvas for a single page, optionally with entry annotations."""
    base_url = settings.R2_PUBLIC_URL_BASE
    iiif_base = f"{base_url}/directory/{page.uuid}/iiif"
    canvas_uri = f"{manifest_uri}canvas/{page.order}"

    canvas = Canvas(
        id=canvas_uri,
        label={"none": [page.original_filename or f"p. {page.order + 1}"]},
        height=page.height,
        width=page.width,
    )

    body = AnnotationBody(
        id=f"{iiif_base}/full/max/0/default.jpg",
        type="Image",
        format="image/jpeg",
        height=page.height,
        width=page.width,
    )
    service = ServiceV3(id=iiif_base, type="ImageService3", profile="level0")
    body.service = [service]

    anno = Annotation(
        id=f"{canvas_uri}/anno",
        motivation="painting",
        body=body,
        target=canvas_uri,
    )
    anno_page = AnnotationPage(id=f"{canvas_uri}/page")
    anno_page.add_item(anno)
    canvas.add_item(anno_page)

    # Add entry annotations with spatial targets (xywh fragment selectors)
    if entries:
        for entry in entries:
            if entry.x is None:
                continue
            canvas.make_annotation(
                id=f"{canvas_uri}/comments/anno-{entry.pk}",
                motivation="commenting",
                body={
                    "type": "TextualBody",
                    "value": entry.original_text or f"Entry {entry.pk}",
                    "format": "text/plain",
                },
                target=f"{canvas_uri}#xywh={entry.x},{entry.y},{entry.w},{entry.h}",
                anno_page_id=f"{canvas_uri}/comments",
            )

    return canvas


@require_http_methods(["GET"])
def iiif_manifest(request, slug):
    directory = get_object_or_404(Directory, slug=slug)
    pages = directory.pages.filter(
        tile_status=Page.TileStatus.COMPLETE,
        width__isnull=False,
        height__isnull=False,
    ).order_by("order")

    # Prefetch entries with bounding boxes for annotation embedding
    entries_by_page = {}
    page_ids = [p.id for p in pages]
    if page_ids:
        for entry in Entry.objects.filter(page_id__in=page_ids, x__isnull=False):
            entries_by_page.setdefault(entry.page_id, []).append(entry)

    manifest = Manifest(
        id=request.build_absolute_uri(),
        label={"en": [directory.title]},
    )

    manifest_uri = request.build_absolute_uri()
    for page in pages:
        page_entries = entries_by_page.get(page.id, [])
        manifest.add_item(_build_canvas(page, manifest_uri, entries=page_entries))

    return _iiif_response(manifest.json(indent=2))


@require_http_methods(["GET"])
def iiif_page_manifest(request, page_uuid):
    page = get_object_or_404(
        Page,
        uuid=page_uuid,
        tile_status=Page.TileStatus.COMPLETE,
        width__isnull=False,
        height__isnull=False,
    )

    manifest = Manifest(
        id=request.build_absolute_uri(),
        label={"none": [page.original_filename or f"p. {page.order + 1}"]},
    )

    page_entries = list(page.entries.filter(x__isnull=False))
    manifest.add_item(
        _build_canvas(page, request.build_absolute_uri(), entries=page_entries)
    )

    return _iiif_response(manifest.json(indent=2))


@require_http_methods(["GET"])
def iiif_collection(request):
    directories = (
        Directory.objects.filter(
            pages__tile_status=Page.TileStatus.COMPLETE,
        )
        .distinct()
        .order_by("title")
    )

    collection = Collection(
        id=request.build_absolute_uri(),
        label={"en": ["City Directories"]},
    )

    for directory in directories:
        manifest_url = request.build_absolute_uri(
            f"/directories/{directory.slug}/manifest.json"
        )
        ref = Manifest(
            id=manifest_url,
            label={"en": [directory.title]},
        )
        collection.add_item(ref)

    return _iiif_response(collection.json(indent=2))
