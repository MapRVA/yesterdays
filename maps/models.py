from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from django.utils.text import slugify


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
        help_text="Collection this layer belongs to (leave empty for primary/base layers)",
    )
    is_default = models.BooleanField(
        default=False,
        help_text="Whether this is the default base layer (only applies to primary layers)",
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
        return f"{self.name} (primary)"

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

    def clean(self):
        super().clean()
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
                    {"type": "MapLibre Style type is only valid for primary layers."}
                )
            if self.is_default:
                raise ValidationError(
                    {
                        "is_default": "Only primary layers (without a collection) can be the default."
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
