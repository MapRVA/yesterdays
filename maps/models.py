from math import isfinite

from django.contrib.gis.db import models as gis_models
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator
from django.db import models
from django.urls import reverse
from django.utils.text import slugify


def validate_layer_polygon(polygon):
    if polygon is None:
        return
    if polygon.geom_type != "Polygon" or polygon.empty or not polygon.valid:
        raise ValidationError(
            "Draw a valid, non-empty polygon without self-intersections."
        )
    if any(
        not isfinite(value) or not -limit <= value <= limit
        for ring in polygon.coords
        for coordinate in ring
        for value, limit in zip(coordinate, (180, 90))
    ):
        raise ValidationError(
            "Polygon coordinates must be valid WGS84 longitude and latitude."
        )


class LayerCollection(models.Model):
    """Collection of map layers that can be toggled together"""

    name = models.CharField(
        max_length=200, help_text="Display name for this collection"
    )
    slug = models.SlugField(unique=True)

    description = models.TextField(
        blank=True, help_text="Optional description of this collection"
    )
    order = models.PositiveIntegerField(
        default=0, help_text="Display order (lower numbers first)"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("maps:browse_maps")

    class Meta:
        ordering = ["order", "name"]


class MapLayer(models.Model):
    """Individual map layer with pmtiles URL and metadata"""

    TYPE_CHOICES = [
        ("pmtiles", "PMTiles"),
        ("xyz", "XYZ Tiles"),
        ("style", "MapLibre Style"),
    ]

    name = models.CharField(max_length=200, help_text="Display name for this layer")
    slug = models.SlugField(help_text="URL-friendly identifier for this layer")

    type = models.CharField(
        max_length=10,
        choices=TYPE_CHOICES,
        default="pmtiles",
        help_text="Type of map layer (PMTiles or XYZ)",
    )
    url = models.URLField(
        help_text="URL to the tile source (PMTiles file or XYZ endpoint)"
    )
    source_link = models.URLField(
        blank=True, help_text="Optional URL to the source of this map layer"
    )
    iiif_link = models.URLField(
        blank=True, help_text="Optional URL to the IIIF manifest"
    )
    oim_link = models.URLField(
        blank=True, help_text="Optional URL to the OIM (OldInsuranceMaps.net) entry"
    )
    attribution = models.TextField(blank=True, help_text="Optional attribution text")
    collection = models.ForeignKey(
        LayerCollection,
        on_delete=models.CASCADE,
        related_name="layers",
        null=True,
        blank=True,
        help_text="Collection this layer belongs to (leave empty for Global layers)",
    )
    is_default = models.BooleanField(
        default=False,
        help_text="Whether this is the default base layer (only applies to Global layers)",
    )
    polygon = gis_models.PolygonField(
        srid=4326,
        null=True,
        blank=True,
        validators=[validate_layer_polygon],
        help_text="Geographic extent of this collection layer. Global layers have no extent.",
    )
    min_zoom = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MaxValueValidator(24)],
        help_text="Lowest whole-number zoom for this collection layer (0–24).",
    )
    order = models.PositiveIntegerField(
        default=0, help_text="Display order (lower numbers first)"
    )

    # Optional metadata
    description = models.TextField(
        blank=True, help_text="Optional description of this layer"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def is_primary(self):
        return self.collection is None

    def __str__(self):
        if self.collection:
            return f"{self.name} ({self.collection.name})"
        return f"{self.name} (Global)"

    def get_absolute_url(self):
        if self.collection:
            return reverse(
                "maps:layer_detail",
                kwargs={
                    "collection_slug": self.collection.slug,
                    "layer_slug": self.slug,
                },
            )
        return reverse("maps:browse_maps")

    def _normalize_collection_metadata(self):
        if self.collection_id is None:
            self.polygon = None
            self.min_zoom = None

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        if update_fields is None or {
            "collection",
            "collection_id",
            "polygon",
            "min_zoom",
        }.intersection(update_fields):
            self._normalize_collection_metadata()
            if update_fields is not None:
                kwargs["update_fields"] = set(update_fields) | {
                    "polygon",
                    "min_zoom",
                }
        super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        self._normalize_collection_metadata()
        collection_errors = {}
        if self.collection_id is not None and self.polygon is None:
            collection_errors["polygon"] = (
                "Draw a polygon for this collection layer."
            )
        if self.collection_id is not None and self.min_zoom is None:
            collection_errors["min_zoom"] = (
                "Choose a minimum zoom for this collection layer."
            )
        if collection_errors:
            raise ValidationError(collection_errors)
        try:
            validate_layer_polygon(self.polygon)
        except ValidationError as error:
            raise ValidationError({"polygon": error.messages}) from error
        if self.is_primary:
            if self.is_default:
                # Only one primary layer may be the default basemap. Moving
                # the default is a two-step edit: unset the old one first.
                current_default = (
                    MapLayer.objects.filter(collection__isnull=True, is_default=True)
                    .exclude(pk=self.pk)
                    .first()
                )
                if current_default is not None:
                    raise ValidationError(
                        {
                            "is_default": (
                                f'"{current_default.name}" is already the default '
                                "base layer. Unset it first."
                            )
                        }
                    )
        else:
            if self.type == "style":
                raise ValidationError(
                    {"type": "MapLibre Style type is only valid for Global layers."}
                )
            if self.is_default:
                raise ValidationError(
                    {
                        "is_default": "Only Global layers (without a collection) can be the default."
                    }
                )

        url_error = self.url_shape_error(self.type, self.url)
        if url_error:
            raise ValidationError({"url": url_error})

    @staticmethod
    def url_shape_error(layer_type, url):
        """Return a message if ``url`` is the wrong shape for ``layer_type``.

        The frontend builds tile requests differently per type, so a URL of
        the wrong shape fails silently on the public map rather than here.
        """
        if not url:
            return None
        if layer_type == "xyz":
            missing = [p for p in ("{z}", "{x}", "{y}") if p not in url]
            if missing:
                return (
                    "XYZ tile URLs must contain the {z}, {x} and {y} placeholders "
                    f"(missing {', '.join(missing)})."
                )
        elif layer_type == "pmtiles":
            if not url.endswith(".pmtiles"):
                return (
                    "PMTiles URLs must point at a .pmtiles file; tile placeholders "
                    "are added automatically."
                )
        elif layer_type == "style":
            if "{z}" in url:
                return (
                    "MapLibre Style URLs must point at a style document, "
                    "not a tile template."
                )
        return None

    class Meta:
        ordering = ["order", "name"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(collection__isnull=True, polygon__isnull=True)
                    | models.Q(collection__isnull=False, polygon__isnull=False)
                ),
                name="maplayer_polygon_matches_collection",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(collection__isnull=True, min_zoom__isnull=True)
                    | models.Q(
                        collection__isnull=False,
                        min_zoom__isnull=False,
                        min_zoom__gte=0,
                        min_zoom__lte=24,
                    )
                ),
                name="maplayer_min_zoom_matches_collection",
            ),
            models.UniqueConstraint(
                fields=["collection", "slug"],
                name="unique_layer_slug_per_collection",
                condition=models.Q(collection__isnull=False),
            ),
            models.UniqueConstraint(
                fields=["slug"],
                name="unique_primary_layer_slug",
                condition=models.Q(collection__isnull=True),
            ),
        ]
