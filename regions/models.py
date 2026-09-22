from django.contrib.gis.db import models as gis_models
from django.db import models, transaction


class Region(models.Model):
    """Admin-curated geographic region backed by a Wikidata entity.

    Sources, collections, and images reference regions via nullable FKs;
    an image's effective region resolves image -> collection -> source
    (``Image.effective_region``). Regions are created only through the
    Django admin, which verifies the Wikidata item's class membership
    (see ``regions.wikidata_check``) before saving.
    """

    short_name = models.CharField(
        max_length=500,
        help_text="Compact name shown in the navbar selector (e.g. Richmond)",
    )
    long_name = models.CharField(
        max_length=500,
        help_text=(
            "Full, disambiguated name shown in region lists (e.g. Richmond, Virginia)"
        ),
    )
    subtitle = models.CharField(
        max_length=500,
        blank=True,
        help_text=(
            "Tagline shown under the region homepage title; falls back to "
            "a generic line naming the region when blank"
        ),
    )
    advertise = models.BooleanField(
        default=False,
        help_text=(
            "Show this region on selector maps, homepage region cards, the "
            "default region directory, and the navbar's Popular list. "
            "Regions remain available through search when this is off."
        ),
    )
    slug = models.SlugField(unique=True)
    wikidata_item = models.OneToOneField(
        "subjects.WikidataItem",
        on_delete=models.CASCADE,
        related_name="region",
        help_text="Linked Wikidata item",
    )
    representative_image = models.ForeignKey(
        "images.Image",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text=(
            "Hand-picked photograph shown for this region in region cards "
            "and map markers. Leave blank to show the placeholder."
        ),
    )
    wikidata_coordinate_location = gis_models.PointField(
        null=True,
        blank=True,
        help_text=(
            "Wikidata P625 (coordinate location) of the linked item, "
            "fetched by the admin on save (see regions.wikidata_check) "
            "and refreshed from each closure load, which never clears "
            "it. Used as the map centerpoint unless overridden below. "
            "Null for items Wikidata has no P625 for, which then have "
            "to carry a custom centerpoint."
        ),
    )
    custom_coordinate_location = gis_models.PointField(
        null=True,
        blank=True,
        help_text=(
            "Map centerpoint chosen by an admin, overriding Wikidata's. "
            "Blank follows P625, which marks a place's official point "
            "(a city hall, a centroid) and isn't always where its map "
            "should open. Never touched by the Wikidata refresh."
        ),
    )
    map_bounds = gis_models.PolygonField(
        null=True,
        blank=True,
        spatial_index=False,
        help_text=(
            "Initial map viewport for this region. Stored as a rectangular "
            "WGS84 polygon and fitted to the available screen size. Blank "
            "falls back to the region centerpoint and sitewide zoom."
        ),
    )
    geocoder_bounds = gis_models.PolygonField(
        null=True,
        blank=True,
        spatial_index=False,
        help_text=(
            "Optional Nominatim search area for this region. Blank uses the "
            "map viewport, then the sitewide search bounds if the viewport "
            "is also blank."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["short_name"]
        constraints = [
            # Either source will do, but a region with no centerpoint at
            # all has no map to open. Enforced in the database because
            # the two columns are written by different code paths (the
            # admin form and the Celery refresh), and neither one alone
            # can see whether the other left anything behind.
            models.CheckConstraint(
                condition=models.Q(wikidata_coordinate_location__isnull=False)
                | models.Q(custom_coordinate_location__isnull=False),
                name="region_has_a_coordinate",
            ),
        ]

    def __str__(self):
        return self.short_name

    @property
    def display_subtitle(self):
        """The homepage tagline: the admin's, else a generic line.

        Regions without a hand-written subtitle used to borrow the
        sitewide one, which reads as a non-sequitur once the page is
        clearly about a single place. A named fallback always says
        something true about the region in view.
        """
        if self.subtitle:
            return self.subtitle
        return f"Place historical images of {self.short_name} on the map!"

    @property
    def coordinate_location(self):
        """The map centerpoint: the admin's choice, else Wikidata's.

        Named for what callers want rather than where it comes from —
        templates and views read this and have no reason to care which
        of the two fields answered. Was a concrete field before the
        override existed, so those call sites didn't have to change.

        Never None for a saved region: the check constraint above rules
        out both columns being empty at once.

        Compared against None rather than truthiness: an empty Point is
        falsy, and silently treating one as "unset" would hide a bad
        write instead of surfacing it.
        """
        if self.custom_coordinate_location is not None:
            return self.custom_coordinate_location
        return self.wikidata_coordinate_location

    @property
    def map_bbox(self):
        """Map bounds as ``[west, south, east, north]``, or None."""
        if self.map_bounds is None:
            return None
        return list(self.map_bounds.extent)

    @property
    def search_bbox(self):
        """Geocoder bounds, falling back to the map bounds."""
        bounds = self.geocoder_bounds
        if bounds is None:
            bounds = self.map_bounds
        return list(bounds.extent) if bounds is not None else None

    def self_and_descendant_ids(self):
        """Primary keys of this region and every region transitively inside it.

        Python mirror of the region_expansion CTE in
        CollectionRegionStats.REFRESH_SQL: a region contains itself plus
        every region whose transitive P131 chain (RegionAncestor) reaches
        this region's wikidata item. One hop suffices because the closure
        is transitive.
        """
        ids = list(
            RegionAncestor.objects.filter(
                ancestor_id=self.wikidata_item_id
            ).values_list("region_id", flat=True)
        )
        ids.append(self.pk)
        return ids

    def save(self, *args, **kwargs):
        old_item_id = None
        if self.pk:
            old_item_id = (
                Region.objects.filter(pk=self.pk)
                .values_list("wikidata_item_id", flat=True)
                .first()
            )
        if self.wikidata_item_id:
            if not self.short_name:
                self.short_name = self.wikidata_item.title
            if not self.long_name:
                self.long_name = self.wikidata_item.title
        super().save(*args, **kwargs)

        # An already-hydrated item (previously a Subject or a closure
        # ancestor) was mirrored without the P131 containment chain, so
        # attaching a Region to it needs a closure re-pull. A never-
        # hydrated item is skipped: its own save() already queued
        # hydration, which runs post-commit and therefore sees this row.
        if (
            self.wikidata_item_id != old_item_id
            and self.wikidata_item.sparql_last_loaded_at is not None
        ):
            # Imported here, not at module level: subjects.tasks imports
            # this module (same cycle WikidataItem.save() dodges).
            from subjects.tasks import hydrate_wikidata_item

            qid = self.wikidata_item.wikidata_id
            transaction.on_commit(lambda: hydrate_wikidata_item.delay(qid))


class RegionAncestor(models.Model):
    """Materialized ``Region -> WikidataItem`` containment relation.

    A flat projection of each Region's transitive P131 (located in the
    administrative territorial entity) ancestors, refreshed from the
    Memgraph mirror after each closure load. Unlike ``SubjectAncestor``
    (which deliberately folds is-a and part-of together for category
    browse), this holds only administrative containment, so "regions
    transitively inside X" stays answerable as an indexed SQL query.
    """

    region = models.ForeignKey(
        "Region",
        on_delete=models.CASCADE,
        related_name="ancestors",
    )
    ancestor = models.ForeignKey(
        "subjects.WikidataItem",
        on_delete=models.CASCADE,
        related_name="+",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["region", "ancestor"],
                name="regionancestor_unique_pair",
            ),
        ]
        indexes = [
            models.Index(fields=["ancestor"]),
        ]

    def __str__(self):
        return f"{self.region} -> {self.ancestor}"
