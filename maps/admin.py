from django import forms
from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from .models import LayerCollection, MapLayer


@admin.register(LayerCollection)
class LayerCollectionAdmin(admin.ModelAdmin):
    list_display = ("name", "order", "layer_count", "created_at")
    list_filter = ("created_at",)
    search_fields = ("name", "description")
    readonly_fields = ("created_at", "updated_at")
    ordering = ("order", "name")

    def layer_count(self, obj):
        return obj.layers.count()

    layer_count.short_description = "Layers"


class MapLayerAdminForm(forms.ModelForm):
    def clean_collection(self):
        collection = self.cleaned_data["collection"]
        if collection is not None and self.instance.polygon is None:
            raise forms.ValidationError(
                "Use the map layer editor to draw an extent before assigning a collection."
            )
        return collection


@admin.register(MapLayer)
class MapLayerAdmin(admin.ModelAdmin):
    form = MapLayerAdminForm
    list_display = (
        "name",
        "layer_role",
        "collection",
        "order",
        "type",
        "is_default",
        "created_at",
    )
    list_filter = ("type", "is_default", "collection", "created_at")
    search_fields = ("name", "description", "collection__name")
    readonly_fields = ("created_at", "updated_at", "extent")
    ordering = ("order", "name")

    fieldsets = (
        (
            "Basic Information",
            {
                "fields": (
                    "name",
                    "slug",
                    "collection",
                    "is_default",
                    "order",
                    "description",
                )
            },
        ),
        ("Map Data", {"fields": ("type", "url", "attribution", "extent")}),
        ("Links", {"fields": ("source_link", "iiif_link", "oim_link")}),
        (
            "System Information",
            {"fields": ("created_at", "updated_at"), "classes": ("collapse",)},
        ),
    )

    @admin.display(description="Role")
    def layer_role(self, obj):
        return "Global" if obj.is_primary else "Collection"

    @admin.display(description="Layer extent")
    def extent(self, obj):
        if not obj or not obj.pk:
            return format_html(
                '<a href="{}">Create a collection layer and draw its extent</a>',
                reverse("maps:layer_create"),
            )
        return format_html(
            '{} <a href="{}">Edit layer extent</a>',
            obj.polygon.wkt if obj.polygon is not None else "Global layer (no extent).",
            reverse("maps:layer_edit", args=[obj.pk]),
        )
