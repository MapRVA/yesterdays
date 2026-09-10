import requests
from django import forms
from django.contrib import admin
from django.contrib.gis.geos import Point, Polygon
from django.utils.html import format_html

from subjects.models import WikidataItem
from subjects.sparql_safety import UnsafeSparqlInput, validate_qid

from .models import Region
from .wikidata_check import ALLOWED_ROOT_CLASSES, check_region


class RegionAdminForm(forms.ModelForm):
    """Create/edit a Region from a Wikidata Q-ID.

    The admin types a Q-ID rather than picking an existing
    ``WikidataItem`` — the item usually doesn't exist yet. Validation
    runs a synchronous WDQS query (see ``regions.wikidata_check``) so the
    admin learns immediately whether the entity qualifies; the row is
    then created or reused on save.

    That query also returns the item's P625, which ``save()`` writes to
    the new row. It happens here rather than in the closure refresh that
    keeps it up to date afterwards because the refresh runs
    asynchronously, well after this insert, and a region with no
    centerpoint at all is refused by the database in between.

    The map centerpoint is entered as a latitude/longitude pair rather
    than a geometry widget, matching ``GeoreferenceAdminForm`` in
    ``images.admin`` — and the pair is what you can paste straight out of
    Wikidata or a map. Leaving both blank clears the override, which puts
    the region back on P625; when the item has no P625 to fall back to,
    ``clean()`` insists on the pair instead.
    """

    wikidata_id = forms.CharField(
        label="Wikidata Q-ID",
        max_length=20,
        help_text="e.g. Q43421. Verified against Wikidata when you save.",
    )
    center_latitude = forms.FloatField(
        required=False,
        min_value=-90,
        max_value=90,
        label="Map centerpoint latitude",
        help_text=(
            "Leave this and the longitude blank to center the region's "
            "maps on the Wikidata coordinate (P625) shown below."
        ),
    )
    center_longitude = forms.FloatField(
        required=False,
        min_value=-180,
        max_value=180,
        label="Map centerpoint longitude",
    )
    map_west = forms.FloatField(
        required=False,
        min_value=-180,
        max_value=180,
        label="Map viewport west longitude",
    )
    map_south = forms.FloatField(
        required=False,
        min_value=-90,
        max_value=90,
        label="Map viewport south latitude",
    )
    map_east = forms.FloatField(
        required=False,
        min_value=-180,
        max_value=180,
        label="Map viewport east longitude",
    )
    map_north = forms.FloatField(
        required=False,
        min_value=-90,
        max_value=90,
        label="Map viewport north latitude",
        help_text=(
            "Enter all four viewport edges. Blank uses the centerpoint and "
            "sitewide zoom while existing regions are being configured."
        ),
    )
    geocoder_west = forms.FloatField(
        required=False,
        min_value=-180,
        max_value=180,
        label="Geocoder west longitude",
    )
    geocoder_south = forms.FloatField(
        required=False,
        min_value=-90,
        max_value=90,
        label="Geocoder south latitude",
    )
    geocoder_east = forms.FloatField(
        required=False,
        min_value=-180,
        max_value=180,
        label="Geocoder east longitude",
    )
    geocoder_north = forms.FloatField(
        required=False,
        min_value=-90,
        max_value=90,
        label="Geocoder north latitude",
        help_text=(
            "Optional. Enter all four edges to override the map viewport for "
            "Nominatim searches; leave all four blank to reuse it."
        ),
    )

    class Meta:
        model = Region
        fields = [
            "short_name",
            "long_name",
            "subtitle",
            "advertise",
            "slug",
            "representative_image",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Blank names fall back to the Wikidata item's label on save.
        self.fields["short_name"].required = False
        self.fields["long_name"].required = False
        # Set by clean_wikidata_id when it talks to WDQS. The flag is
        # separate because the coordinate itself is legitimately None for
        # an item with no P625, which mustn't look like "no round trip
        # was made" — on an edit that leaves the Q-ID alone there is no
        # round trip, and the stored coordinate has to survive untouched.
        self._checked_wikidata = False
        self._fetched_coordinate = None
        if self.instance.pk:
            self.fields["wikidata_id"].initial = self.instance.wikidata_item.wikidata_id
            override = self.instance.custom_coordinate_location
            if override is not None:
                self.fields["center_latitude"].initial = override.y
                self.fields["center_longitude"].initial = override.x
            self._set_bounds_initial("map", self.instance.map_bounds)
            self._set_bounds_initial("geocoder", self.instance.geocoder_bounds)

    def _set_bounds_initial(self, prefix, bounds):
        if bounds is None:
            return
        west, south, east, north = bounds.extent
        for edge, value in zip(
            ("west", "south", "east", "north"),
            (west, south, east, north),
        ):
            self.fields[f"{prefix}_{edge}"].initial = value

    def _clean_bounds(self, prefix, label):
        field_names = [
            f"{prefix}_west",
            f"{prefix}_south",
            f"{prefix}_east",
            f"{prefix}_north",
        ]
        values = [self.cleaned_data.get(name) for name in field_names]
        if all(value is None for value in values):
            return
        if any(value is None for value in values):
            for name, value in zip(field_names, values):
                if value is None:
                    self.add_error(
                        name,
                        f"Enter this edge too, or clear the entire {label}.",
                    )
            return

        west, south, east, north = values
        if west >= east:
            self.add_error(
                f"{prefix}_east",
                "East longitude must be greater than west longitude.",
            )
        if south >= north:
            self.add_error(
                f"{prefix}_north",
                "North latitude must be greater than south latitude.",
            )

    def _bounds_polygon(self, prefix):
        values = [
            self.cleaned_data.get(f"{prefix}_{edge}")
            for edge in ("west", "south", "east", "north")
        ]
        if any(value is None for value in values):
            return None
        polygon = Polygon.from_bbox(tuple(values))
        polygon.srid = 4326
        return polygon

    def clean_wikidata_id(self):
        qid = self.cleaned_data["wikidata_id"].strip().upper()
        try:
            validate_qid(qid)
        except UnsafeSparqlInput:
            raise forms.ValidationError("Enter a Wikidata Q-ID like Q43421.")

        clash = Region.objects.filter(wikidata_item__wikidata_id=qid)
        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)
        existing = clash.first()
        if existing is not None:
            raise forms.ValidationError(
                f"{qid} is already linked to region “{existing.short_name}”."
            )

        # Editing without changing the Q-ID skips the WDQS round trip.
        if self.instance.pk and qid == self.instance.wikidata_item.wikidata_id:
            return qid

        try:
            check = check_region(qid)
        except requests.RequestException:
            raise forms.ValidationError(
                "Could not reach the Wikidata Query Service to verify this "
                "item. Nothing was saved — please try again in a moment."
            )

        item = WikidataItem.objects.filter(wikidata_id=qid).first()
        label = f" ({item.title})" if item else ""
        if not check.eligible:
            roots = ", ".join(ALLOWED_ROOT_CLASSES)
            raise forms.ValidationError(
                f"{qid}{label} is not a territory or human settlement on "
                f"Wikidata (no instance-of/subclass-of path to any of: "
                f"{roots})."
            )
        # A missing P625 is not disqualifying — it just means the admin
        # has to say where the map should open (see clean()).
        self._checked_wikidata = True
        self._fetched_coordinate = check.coordinate
        return qid

    def _wikidata_coordinate_after_save(self):
        """P625 as it will stand once this form is saved, or None.

        A round trip's answer supersedes what's stored, including when
        the answer is "none" — repointing a region at an item without a
        P625 has to drop the old item's coordinate, not inherit it.
        """
        if self._checked_wikidata:
            return self._fetched_coordinate
        return self.instance.wikidata_coordinate_location

    def clean(self):
        cleaned = super().clean()
        self._clean_bounds("map", "map viewport")
        self._clean_bounds("geocoder", "geocoder bounds")

        latitude = cleaned.get("center_latitude")
        longitude = cleaned.get("center_longitude")
        # Half a coordinate is a typo, not an intention. Reported on the
        # empty half, which is the one that needs filling in.
        if latitude is None and longitude is not None:
            self.add_error("center_latitude", "Enter a latitude too, or clear both.")
            return cleaned
        if longitude is None and latitude is not None:
            self.add_error("center_longitude", "Enter a longitude too, or clear both.")
            return cleaned

        # Wikidata normally supplies the centerpoint, which is what makes
        # the pair above optional. With no P625 behind it there's nothing
        # to fall back on, and the region_has_a_coordinate constraint
        # would reject the row — so ask for it here, where the message
        # can say why. Skipped when the Q-ID itself failed to clean: the
        # admin has a real error to fix already, and this one may well go
        # away once they do.
        if "wikidata_id" not in cleaned or latitude is not None:
            return cleaned
        if self._wikidata_coordinate_after_save() is None:
            self.add_error(
                "center_latitude",
                "Wikidata has no coordinate location (P625) for this item, "
                "so there's nothing to center the region's maps on. Enter a "
                "latitude and longitude here instead.",
            )
        return cleaned

    def save(self, commit=True):
        region = super().save(commit=False)
        qid = self.cleaned_data["wikidata_id"]
        item = WikidataItem.objects.filter(wikidata_id=qid).first()
        if item is None:
            item = WikidataItem(wikidata_id=qid)
            # Synchronous entity-JSON fetch; queues closure hydration
            # post-commit, which will see the Region row saved below and
            # use the P131 query variant.
            item.save()
        region.wikidata_item = item
        if self._checked_wikidata:
            region.wikidata_coordinate_location = self._fetched_coordinate
        # Assigned every save, so blanking both inputs clears the
        # override rather than leaving a stale one behind. clean() has
        # already ruled out one-of-two.
        latitude = self.cleaned_data.get("center_latitude")
        longitude = self.cleaned_data.get("center_longitude")
        if latitude is None or longitude is None:
            region.custom_coordinate_location = None
        else:
            region.custom_coordinate_location = Point(longitude, latitude, srid=4326)
        region.map_bounds = self._bounds_polygon("map")
        region.geocoder_bounds = self._bounds_polygon("geocoder")
        # Blank names are backfilled from the item label in Region.save().
        if commit:
            region.save()
        return region


@admin.register(Region)
class RegionAdmin(admin.ModelAdmin):
    form = RegionAdminForm
    autocomplete_fields = ("representative_image",)
    prepopulated_fields = {"slug": ("short_name",)}
    list_display = (
        "short_name",
        "long_name",
        "advertise",
        "slug",
        "wikidata_item_link",
        "ancestor_count",
        "created_at",
    )
    list_filter = ("advertise",)
    search_fields = (
        "short_name",
        "long_name",
        "slug",
        "wikidata_item__wikidata_id",
        "wikidata_item__title",
    )
    readonly_fields = (
        "wikidata_coordinate_display",
        "map_center_display",
        "created_at",
        "updated_at",
    )
    fieldsets = (
        (
            "Region",
            {
                "fields": (
                    "wikidata_id",
                    "short_name",
                    "long_name",
                    "subtitle",
                    "advertise",
                    "slug",
                    "representative_image",
                )
            },
        ),
        (
            "Map centerpoint",
            {
                "fields": (
                    "wikidata_coordinate_display",
                    "center_latitude",
                    "center_longitude",
                    "map_center_display",
                )
            },
        ),
        (
            "Map viewport",
            {
                "fields": ("map_west", "map_south", "map_east", "map_north"),
                "description": (
                    "The map fits this rectangle to each visitor's screen, "
                    "so one region works at phone and desktop sizes."
                ),
            },
        ),
        (
            "Geocoder search bounds",
            {
                "fields": (
                    "geocoder_west",
                    "geocoder_south",
                    "geocoder_east",
                    "geocoder_north",
                ),
                "description": (
                    "Optional Nominatim restriction. Blank reuses the map "
                    "viewport, then the sitewide search bounds."
                ),
            },
        ),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )

    def wikidata_item_link(self, obj):
        if obj.wikidata_item:
            return format_html(
                '<a href="{}" target="_blank">{}</a>',
                obj.wikidata_item.wikidata_url,
                obj.wikidata_item.wikidata_id,
            )
        return "None"

    wikidata_item_link.short_description = "Wikidata"

    def ancestor_count(self, obj):
        return obj.ancestors.count()

    ancestor_count.short_description = "Ancestors"

    def wikidata_coordinate_display(self, obj):
        """Show P625 in the lat, long order Wikidata displays it."""
        point = obj.wikidata_coordinate_location
        if point is None:
            if obj.pk is None:
                return "Fetched from Wikidata when you save"
            return "None — Wikidata has no P625 for this item"
        return f"{point.y:.5f}, {point.x:.5f}"

    wikidata_coordinate_display.short_description = "Coordinate location (P625)"

    def map_center_display(self, obj):
        """Answer the question the two coordinate fields raise: which wins?"""
        point = obj.coordinate_location
        if point is None:
            return "Fetched from Wikidata when you save"
        source = (
            "custom" if obj.custom_coordinate_location is not None else "from Wikidata"
        )
        return f"{point.y:.5f}, {point.x:.5f} ({source})"

    map_center_display.short_description = "Map centerpoint in use"
