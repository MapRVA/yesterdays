import json

from django import forms
from django.contrib.gis import forms as gis_forms
from django.contrib.gis.gdal import GDALException

from .models import MapLayer, validate_layer_polygon


class LayerPolygonField(gis_forms.PolygonField):
    def to_python(self, value):
        try:
            return super().to_python(value)
        except GDALException as error:
            raise forms.ValidationError(
                self.error_messages["invalid_geom"], code="invalid_geom"
            ) from error


class MapLayerForm(forms.ModelForm):
    """Staff-facing create/edit form for a map layer.

    Field grouping mirrors MapLayerAdmin's fieldsets; the templates render the
    groups as cards via ``field_groups``.
    """

    polygon = LayerPolygonField(
        srid=4326,
        required=False,
        widget=forms.HiddenInput,
        validators=[validate_layer_polygon],
    )

    FIELD_GROUPS = (
        (
            "Basic Information",
            ("name", "slug", "collection", "order", "description"),
        ),
        ("Map Data", ("type", "url", "attribution")),
        ("Links", ("source_link", "iiif_link", "oim_link")),
    )

    class Meta:
        model = MapLayer
        fields = [
            "name",
            "slug",
            "collection",
            "order",
            "type",
            "url",
            "attribution",
            "description",
            "source_link",
            "iiif_link",
            "oim_link",
            "polygon",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            polygon = self.instance.polygon
            self.initial["polygon"] = polygon.geojson if polygon is not None else ""
        elif not self.data.get(self.add_prefix("collection")):
            # Global layers ignore even stale or malformed submitted geometry.
            self.data = self.data.copy()
            self.data[self.add_prefix("polygon")] = ""
        self.fields["slug"].help_text = (
            "URL-friendly identifier. Must be unique within the collection, or "
            "unique among Global layers when no collection is set."
        )
        self.fields["collection"].empty_label = "Global (no collection)"
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault("class", "form-check-input")
            elif isinstance(widget, forms.Select):
                widget.attrs.setdefault("class", "form-select")
            else:
                widget.attrs.setdefault("class", "form-control")

    def field_groups(self):
        """Yield (title, [bound fields]) pairs for the template's cards."""
        for title, names in self.FIELD_GROUPS:
            yield title, [self[name] for name in names]

    def polygon_editor_data(self):
        """Keep bound geometry on validation errors, including an explicit deletion."""
        value = self["polygon"].value()
        try:
            geometry = self.fields["polygon"].clean(value) if value else None
        except forms.ValidationError:
            geometry = None
        return {
            "polygon": json.loads(geometry.geojson) if geometry is not None else None,
        }

    def clean(self):
        cleaned = super().clean()
        if (
            cleaned.get("collection") is not None
            and "polygon" in cleaned
            and cleaned["polygon"] is None
        ):
            self.add_error("polygon", "Draw a polygon for this collection layer.")
        # MapLayer's slug uniqueness lives in conditional UniqueConstraints,
        # which ModelForm._post_clean() skips (validate_constraints=False), so
        # check explicitly to surface a field error instead of an IntegrityError.
        slug = cleaned.get("slug")
        if slug:
            siblings = MapLayer.objects.filter(
                slug=slug, collection=cleaned.get("collection")
            ).exclude(pk=self.instance.pk)
            if siblings.exists():
                if cleaned.get("collection") is not None:
                    message = (
                        "A layer with this slug already exists in this collection."
                    )
                else:
                    message = "A Global layer with this slug already exists."
                self.add_error("slug", message)
        return cleaned
