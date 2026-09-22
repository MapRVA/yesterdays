from django.conf import settings
from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import path, reverse
from django.utils import timezone
from django.utils.formats import localize
from django.utils.html import format_html

from .models import (
    Business,
    Occupation,
    OsmElement,
    Person,
    Subject,
    WikidataItem,
)


@admin.register(WikidataItem)
class WikidataItemAdmin(admin.ModelAdmin):
    list_display = (
        "wikidata_id",
        "title",
        "description_truncated",
        "va_landmark_id",
        "inception",
        "demolished",
        "last_updated",
    )
    list_filter = ("last_updated", "inception", "demolished")
    search_fields = ("wikidata_id", "title", "description", "va_landmark_id")
    readonly_fields = ("created_at", "last_updated", "wikidata_url", "refresh_button")
    actions = ["refresh_selected_wikidata_items"]

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "<path:object_id>/refresh/",
                self.admin_site.admin_view(self.refresh_individual_item),
                name="subjects_wikidataitem_refresh",
            ),
        ]
        return custom_urls + urls

    def refresh_individual_item(self, request, object_id):
        """Refresh Wikidata information for a single item"""
        wikidata_item = get_object_or_404(WikidataItem, pk=object_id)

        messages.info(
            request,
            f"Refreshing Wikidata information for {wikidata_item.wikidata_id}... This may take a moment.",
        )

        try:
            if wikidata_item.populate_from_wikidata():
                wikidata_item.save()
                messages.success(
                    request,
                    f"Successfully refreshed Wikidata information for {wikidata_item.wikidata_id}. "
                    f"Title: {wikidata_item.title}",
                )
            else:
                messages.warning(
                    request,
                    f"No data found for {wikidata_item.wikidata_id}. "
                    f"The item may not exist or may not have an English or "
                    f"default label.",
                )
        except ValidationError as e:
            error_msg = str(e)
            if "Network error" in error_msg:
                messages.error(
                    request,
                    f"Network error refreshing {wikidata_item.wikidata_id}. "
                    f"The system automatically retried the request. Please try again if this persists.",
                )
            else:
                messages.error(
                    request, f"Error refreshing Wikidata information: {error_msg}"
                )
        except Exception as e:
            messages.error(
                request, f"Unexpected error refreshing Wikidata information: {e}"
            )

        return HttpResponseRedirect(
            reverse("admin:subjects_wikidataitem_change", args=[object_id])
        )

    def refresh_selected_wikidata_items(self, request, queryset):
        """Refresh Wikidata information for selected items"""
        total_count = queryset.count()
        updated_count = 0
        failed_count = 0
        network_errors = 0
        error_messages = []

        self.message_user(
            request,
            f"Refreshing {total_count} Wikidata item(s)... This may take a moment.",
        )

        for item in queryset:
            try:
                if item.populate_from_wikidata():
                    item.save()
                    updated_count += 1
                else:
                    failed_count += 1
            except ValidationError as e:
                failed_count += 1
                error_str = str(e)
                if "Network error" in error_str:
                    network_errors += 1
                error_messages.append(f"{item.wikidata_id}: {error_str}")
            except Exception as e:
                failed_count += 1
                error_messages.append(f"{item.wikidata_id}: Unexpected error - {e}")

        if updated_count > 0:
            self.message_user(
                request, f"Successfully updated {updated_count} Wikidata item(s)."
            )

        if failed_count > 0:
            error_msg = (
                f"Failed to update {failed_count} of {total_count} Wikidata item(s)."
            )
            if network_errors > 0:
                error_msg += (
                    f" ({network_errors} network errors - these may succeed if retried)"
                )
            if error_messages and len(error_messages) <= 3:
                error_msg += f" Errors: {'; '.join(error_messages)}"
            elif error_messages:
                error_msg += f" First 3 errors: {'; '.join(error_messages[:3])} (and {len(error_messages) - 3} more)"
            self.message_user(request, error_msg, level=messages.WARNING)

    refresh_selected_wikidata_items.short_description = "Refresh Wikidata information"

    fieldsets = (
        (
            "Wikidata Information",
            {
                "fields": (
                    "wikidata_id",
                    "title",
                    "description",
                    "wikidata_url",
                    "wikipedia_url",
                    "refresh_button",
                )
            },
        ),
        (
            "Additional Metadata",
            {
                "fields": (
                    "va_landmark_id",
                    "architect",
                    "image_url",
                    "inception",
                    "demolished",
                ),
                "description": "Optional additional information about the subject",
            },
        ),
        (
            "System Information",
            {
                "fields": ("created_at", "last_updated"),
                "classes": ("collapse",),
            },
        ),
    )

    def description_truncated(self, obj):
        if obj.description:
            return (
                obj.description[:100] + "..."
                if len(obj.description) > 100
                else obj.description
            )
        return ""

    description_truncated.short_description = "Description"

    def wikidata_url(self, obj):
        return obj.wikidata_url if obj.wikidata_id else ""

    wikidata_url.short_description = "Wikidata URL"

    def refresh_button(self, obj):
        if obj.pk:
            return format_html(
                '<a class="default" href="{}" style="background: #417690; color: white; padding: 8px 12px; text-decoration: none; border-radius: 4px; display: inline-block; margin: 5px 0; font-size: 12px;" title="Fetch latest information from Wikidata API">Refresh from Wikidata</a>',
                reverse("admin:subjects_wikidataitem_refresh", args=[obj.pk]),
            )
        return '<span style="color: #999; font-style: italic;">Save item first</span>'

    refresh_button.short_description = "Actions"


@admin.register(Person)
class PersonAdmin(admin.ModelAdmin):
    list_display = ["last_name", "first_name", "middle_name", "birth_date"]
    search_fields = ["first_name", "middle_name", "last_name"]


@admin.register(Business)
class BusinessAdmin(admin.ModelAdmin):
    list_display = ["name"]
    search_fields = ["name"]


@admin.register(Occupation)
class OccupationAdmin(admin.ModelAdmin):
    list_display = ["name"]
    search_fields = ["name"]


class OsmElementInline(admin.TabularInline):
    model = OsmElement
    extra = 0
    fields = ("osm_type", "osm_id", "geometry_area", "updated_at")
    readonly_fields = ("osm_type", "osm_id", "geometry_area", "updated_at")
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(OsmElement)
class OsmElementAdmin(admin.ModelAdmin):
    list_display = (
        "osm_type",
        "osm_id",
        "subject",
        "geometry_area",
        "updated_at",
        "created_at",
    )
    list_filter = ("osm_type", "updated_at", "created_at")
    search_fields = ("osm_id", "subject__title")
    readonly_fields = ("created_at", "updated_at", "geometry_area")
    autocomplete_fields = ["subject"]


@admin.register(Subject)
class SubjectAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "description_truncated",
        "wikidata_item_link",
        "osm_element_count",
        "image_count",
        "ancestor_count",
        "created_at",
    )
    list_filter = ("created_at",)
    search_fields = (
        "title",
        "wikidata_item__wikidata_id",
        "wikidata_item__title",
        "wikidata_item__description",
    )
    readonly_fields = (
        "created_at",
        "updated_at",
        "wikidata_refresh_status",
        "wikidata_item_display",
    )
    autocomplete_fields = ["wikidata_item"]
    inlines = [OsmElementInline]
    actions = ["queue_selected_for_wikidata_refresh"]

    fieldsets = (
        (
            "Subject Information",
            {"fields": ("title", "slug")},
        ),
        (
            "Linked Data",
            {
                "fields": ("wikidata_item", "wikidata_refresh_status"),
                "description": (
                    "The Wikidata item is chosen once, when the subject is "
                    "created, and can't be changed afterwards. OSM elements "
                    "are shown below."
                ),
            },
        ),
        (
            "System Information",
            {
                "fields": ("created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )

    def get_fieldsets(self, request, obj=None):
        """Swap the Wikidata picker for a read-only link on existing subjects.

        A Subject *is* its Wikidata item — the one-to-one is the subject's
        identity, so the picker only makes sense on the add form.
        Repointing it later would silently re-identify every image already
        tagged with the subject; the fix for a wrong link is a new subject,
        not an edit.
        """
        fieldsets = super().get_fieldsets(request, obj)
        if obj is None:
            return fieldsets
        return [
            (
                name,
                {
                    **options,
                    "fields": tuple(
                        "wikidata_item_display" if field == "wikidata_item" else field
                        for field in options["fields"]
                    ),
                },
            )
            for name, options in fieldsets
        ]

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "<path:object_id>/queue-wikidata-refresh/",
                self.admin_site.admin_view(self.queue_wikidata_refresh),
                name="subjects_subject_queue_wikidata_refresh",
            ),
        ]
        return custom_urls + urls

    def queue_wikidata_refresh(self, request, object_id):
        """Put this subject's Wikidata item at the front of the refresh line."""
        subject = get_object_or_404(Subject, pk=object_id)
        subject.wikidata_item.queue_sparql_refresh()

        messages.success(
            request,
            f"Queued {subject.wikidata_item.wikidata_id} for a Wikidata refresh. "
            f"A background worker picks up the next queued item roughly every "
            f"{settings.METADATA_REFRESH_WIKIDATA_INTERVAL} seconds.",
        )

        return HttpResponseRedirect(
            reverse("admin:subjects_subject_change", args=[object_id])
        )

    @admin.action(description="Queue for Wikidata metadata refresh")
    def queue_selected_for_wikidata_refresh(self, request, queryset):
        queued = 0

        for subject in queryset.select_related("wikidata_item"):
            subject.wikidata_item.queue_sparql_refresh()
            queued += 1

        self.message_user(
            request,
            f"Queued {queued} subject(s) for a Wikidata refresh, one every "
            f"~{settings.METADATA_REFRESH_WIKIDATA_INTERVAL} seconds.",
        )

    def wikidata_item_display(self, obj):
        """Read-only stand-in for the ``wikidata_item`` picker."""
        return format_html(
            '{} (<a href="{}">edit</a> · <a href="{}" target="_blank" '
            'rel="noopener">{}</a>)',
            obj.wikidata_item.title,
            reverse("admin:subjects_wikidataitem_change", args=[obj.wikidata_item.pk]),
            obj.wikidata_item.wikidata_url,
            obj.wikidata_item.wikidata_id,
        )

    wikidata_item_display.short_description = "Wikidata item"

    def wikidata_refresh_status(self, obj):
        """Queue button, or the pending request if one is already in flight."""
        if not obj.pk:
            return "Save subject first"
        requested_at = obj.wikidata_item.sparql_refresh_requested_at
        if requested_at:
            return format_html(
                "Queued {} — waiting for the next background refresh.",
                localize(timezone.localtime(requested_at)),
            )
        return format_html(
            '<a class="default" href="{}" style="background: #417690; color: white; '
            "padding: 8px 12px; text-decoration: none; border-radius: 4px; "
            'display: inline-block; margin: 5px 0; font-size: 12px;" '
            'title="Jump to the front of the background Wikidata refresh queue">'
            "Manually Queue for Update</a>",
            reverse("admin:subjects_subject_queue_wikidata_refresh", args=[obj.pk]),
        )

    wikidata_refresh_status.short_description = "Wikidata refresh"

    def description_truncated(self, obj):
        description = obj.get_description()
        if description:
            return description[:100] + "..." if len(description) > 100 else description
        return ""

    description_truncated.short_description = "Description"

    def wikidata_item_link(self, obj):
        if obj.wikidata_item:
            return format_html(
                '<a href="{}" target="_blank">{}</a>',
                obj.wikidata_item.wikidata_url,
                obj.wikidata_item.wikidata_id,
            )
        return "None"

    wikidata_item_link.short_description = "Wikidata"

    def osm_element_count(self, obj):
        return obj.osm_elements.count()

    osm_element_count.short_description = "OSM Elements"

    def image_count(self, obj):
        return obj.image_mappings.count()

    image_count.short_description = "Images"

    def ancestor_count(self, obj):
        return obj.ancestors.count()

    ancestor_count.short_description = "Ancestors"
