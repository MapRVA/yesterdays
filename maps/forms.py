from django import forms

from .models import MapLayer


class MapLayerForm(forms.ModelForm):
    """Staff-facing create/edit form for a map layer.

    Field grouping mirrors MapLayerAdmin's fieldsets; the templates render the
    groups as cards via ``field_groups``.
    """

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
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["slug"].help_text = (
            "URL-friendly identifier. Must be unique within the collection, or "
            "unique among primary layers when no collection is set."
        )
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

    def clean(self):
        cleaned = super().clean()
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
                    message = "A layer with this slug already exists in this collection."
                else:
                    message = "A primary layer with this slug already exists."
                self.add_error("slug", message)
        return cleaned
