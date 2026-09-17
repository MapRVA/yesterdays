from django import forms

from .admin import RegionAdminForm


class RegionForm(RegionAdminForm):
    """Staff-facing region form used by the first-class management pages."""

    FIELD_GROUPS = (
        (
            "Region",
            (
                "wikidata_id",
                "short_name",
                "long_name",
                "subtitle",
                "advertise",
                "slug",
                "representative_image",
            ),
        ),
    )
    SPATIAL_FIELDS = (
        "center_latitude",
        "center_longitude",
        "map_west",
        "map_south",
        "map_east",
        "map_north",
        "geocoder_west",
        "geocoder_south",
        "geocoder_east",
        "geocoder_north",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if name in self.SPATIAL_FIELDS:
                field.widget = forms.HiddenInput()
            elif name == "representative_image":
                # The admin uses Select2 for this potentially enormous relation.
                # A numeric field keeps the standalone page lightweight while the
                # ModelChoiceField still validates that the image exists.
                field.widget = forms.NumberInput(
                    attrs={"class": "form-control", "min": 1}
                )
                field.label = "Representative image ID"
            elif isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault("class", "form-check-input")
            elif isinstance(field.widget, forms.Select):
                field.widget.attrs.setdefault("class", "form-select")
            else:
                field.widget.attrs.setdefault("class", "form-control")

    def field_groups(self):
        for title, names in self.FIELD_GROUPS:
            yield title, [self[name] for name in names]

    def spatial_fields(self):
        return [self[name] for name in self.SPATIAL_FIELDS]

    def map_spatial_fields(self):
        return [
            self[name]
            for name in (
                "center_latitude",
                "center_longitude",
                "map_west",
                "map_south",
                "map_east",
                "map_north",
            )
        ]

    def geocoder_spatial_fields(self):
        return [
            self[name]
            for name in (
                "geocoder_west",
                "geocoder_south",
                "geocoder_east",
                "geocoder_north",
            )
        ]
