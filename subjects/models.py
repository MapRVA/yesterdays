import urllib
import urllib.parse
from datetime import datetime

import requests
from django.contrib.gis.db import models as gis_models
from django.contrib.postgres.indexes import GinIndex
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from images.policies import representable_images


def _first_time_claim(claims, pid):
    """Return the first CE date of entity-JSON claim ``pid`` as ``YYYY-MM-DD``.

    Wikidata time literals look like ``"+1895-01-01T00:00:00Z"``; the
    leading sign is a BCE marker, so ``-`` values are skipped rather than
    misparsed. Returns ``None`` when the property is absent or carries no
    usable value.
    """
    for claim in claims.get(pid, []):
        if claim.get("mainsnak", {}).get("snaktype") != "value":
            continue
        time_data = claim["mainsnak"]["datavalue"]["value"]
        if "time" in time_data and time_data["time"].startswith("+"):
            return time_data["time"][1:11]
    return None


def _preferred_wikidata_label(labels):
    """Return the English label, falling back to Wikidata's ``mul`` label."""
    for language in ("en", "mul"):
        value = labels.get(language, {}).get("value", "")
        if value:
            return value
    return ""


class WikidataItem(models.Model):
    """Wikidata item with cached metadata"""

    wikidata_id = models.CharField(
        max_length=20, unique=True, help_text="Wikidata ID (e.g., Q123456)"
    )
    title = models.CharField(max_length=500, help_text="Title from Wikidata")
    description = models.TextField(blank=True, help_text="Description from Wikidata")
    wikipedia_url = models.URLField(
        blank=True, help_text="URL to Wikipedia page (if available)"
    )
    va_landmark_id = models.CharField(
        max_length=30, blank=True, help_text="Virginia Landmarks Registry ID"
    )
    architect = models.TextField(
        blank=True, help_text="Architect(s) - multiple names can be separated by commas"
    )
    image_url = models.URLField(
        max_length=500,
        blank=True,
        help_text="URL to representative image from Wikidata",
    )
    inception = models.DateField(
        null=True, blank=True, help_text="Date of construction/inception"
    )
    demolished = models.DateField(
        null=True,
        blank=True,
        help_text="Date of demolition/dissolution (Wikidata P576)",
    )
    last_updated = models.DateTimeField(
        auto_now=True, help_text="When this row was last modified"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    # Metadata refresh tracking
    metadata_last_fetched = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When we last attempted to fetch metadata from Wikidata",
    )
    metadata_fetch_failures = models.PositiveIntegerField(
        default=0,
        help_text="Consecutive fetch failures (resets on success)",
    )

    # WDQS mirror tracking - separate cadence from the JSON metadata fetch
    # above. Populated when we load this entity's closure (fetched from
    # WDQS via SPARQL) into the Memgraph mirror.
    sparql_last_loaded_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When this entity's RDF was last loaded into Oxigraph",
    )
    sparql_fetch_failures = models.PositiveIntegerField(
        default=0,
        help_text="Consecutive SPARQL mirror load failures (resets on success)",
    )
    sparql_refresh_requested_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "Set when an admin manually queues this item, moving it to the "
            "front of the Beat-driven refresh rotation. Cleared when the "
            "refresh is picked up."
        ),
    )
    discovered_via = models.ForeignKey(
        "Subject",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text=(
            "For ancestor entities pulled in by the closure walk, the Subject "
            "whose graph first surfaced this entity. Null for entities that "
            "are themselves Subjects."
        ),
    )

    def __str__(self):
        return f"{self.wikidata_id}: {self.title}"

    @property
    def wikidata_url(self):
        """Generate Wikidata URL from ID"""
        return f"https://www.wikidata.org/wiki/{self.wikidata_id}"

    @property
    def date_range(self):
        """Render ``inception``/``demolished`` as a display year range.

        Yields ``"1901–1910"``, an open-ended ``"1950–"`` when the subject
        was never demolished (or the date is unknown), ``"?–1910"`` when
        only the end is known, and ``""`` when neither date is set.

        Years only: neither write path records Wikidata's
        ``timePrecision``, so a year-precision claim is indistinguishable
        from a real January 1st and anything finer would invent precision
        we don't have.
        """
        if not self.inception and not self.demolished:
            return ""
        start = self.inception.year if self.inception else "?"
        end = self.demolished.year if self.demolished else ""
        return f"{start}–{end}"

    def _fetch_wikidata_info(self):
        """Internal method to fetch and parse data from Wikidata API."""
        # Configure session with retry strategy
        session = requests.Session()
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("http://", adapter)
        session.mount("https://", adapter)

        try:
            url = f"https://www.wikidata.org/wiki/Special:EntityData/{self.wikidata_id}.json"
            headers = {
                "User-Agent": "GeoreferenceTool/1.0 (https://github.com/mapRVA/georeference-tool)"
            }
            response = session.get(url, headers=headers, timeout=15)
            response.raise_for_status()

            if not response.text.strip():
                return None

            data = response.json()
            entity = data.get("entities", {}).get(self.wikidata_id)

            if not entity or "missing" in entity:
                return None

            title = _preferred_wikidata_label(entity.get("labels", {}))
            description = entity.get("descriptions", {}).get("en", {}).get("value", "")
            wiki_title = entity.get("sitelinks", {}).get("enwiki", {}).get("title")
            wikipedia_url = (
                f"https://en.wikipedia.org/wiki/{urllib.parse.quote(wiki_title.replace(' ', '_'))}"
                if wiki_title
                else ""
            )

            architect = ""
            image_url = ""

            claims = entity.get("claims", {})
            if "P84" in claims:
                architects = []
                for claim in claims["P84"]:
                    if claim.get("mainsnak", {}).get("snaktype") == "value":
                        entity_id = claim["mainsnak"]["datavalue"]["value"]["id"]
                        architects.append(f"Wikidata:{entity_id}")
                architect = ", ".join(architects)

            if "P18" in claims:
                for claim in claims["P18"]:
                    if claim.get("mainsnak", {}).get("snaktype") == "value":
                        filename = claim["mainsnak"]["datavalue"]["value"]
                        filename_encoded = urllib.parse.quote(
                            filename.replace(" ", "_")
                        )
                        image_url = f"https://commons.wikimedia.org/w/index.php?title=Special:Redirect/file/{filename_encoded}&width=300"
                        break

            return {
                "title": title,
                "description": description,
                "wikipedia_url": wikipedia_url,
                "architect": architect,
                "image_url": image_url,
                "inception": _first_time_claim(claims, "P571"),
                "demolished": _first_time_claim(claims, "P576"),
            }

        except requests.RequestException as e:
            # Raise a validation error to be caught by the save method
            raise ValidationError(
                f"Network error fetching Wikidata info for {self.wikidata_id}: {e}"
            )
        except Exception as e:
            raise ValidationError(
                f"Unexpected error fetching Wikidata info for {self.wikidata_id}: {e}"
            )
        finally:
            session.close()

    def populate_from_wikidata(self):
        """Fetch and populate metadata from Wikidata API."""
        wikidata_info = self._fetch_wikidata_info()
        if wikidata_info and wikidata_info["title"]:
            self.title = wikidata_info["title"]
            self.description = wikidata_info["description"]
            self.wikipedia_url = wikidata_info["wikipedia_url"]
            self.architect = wikidata_info["architect"]
            self.image_url = wikidata_info["image_url"]

            for field in ("inception", "demolished"):
                if wikidata_info[field]:
                    try:
                        parsed = datetime.strptime(
                            wikidata_info[field], "%Y-%m-%d"
                        ).date()
                    except (ValueError, TypeError):
                        continue
                    setattr(self, field, parsed)
            return True
        return False

    def ensure_display_label(self):
        """Replace an empty/Q-ID placeholder with an English or ``mul`` label."""
        if self.title and self.title != self.wikidata_id:
            return
        if not self.populate_from_wikidata():
            raise ValidationError(
                f"No English or default label found for {self.wikidata_id}"
            )
        self.save()

    def _apply_seed_metadata(self, meta):
        """Apply a dict from extract_seed_metadata onto self in-place."""
        self.title = meta["title"]
        self.description = meta["description"]
        self.wikipedia_url = meta["wikipedia_url"]
        self.architect = meta["architect"]
        self.image_url = meta["image_url"]
        if meta["inception"]:
            self.inception = meta["inception"]
        if meta["demolished"]:
            self.demolished = meta["demolished"]

    def queue_sparql_refresh(self, *, reset_failures=True):
        """Move this item to the front of the WDQS refresh rotation.

        The Beat-driven refresher normally works through seed items
        oldest-first once they pass the staleness threshold; a queued item
        is picked up on the very next tick regardless of how fresh it is.
        ``sparql_fetch_failures`` is reset too, so an item that had
        exhausted its retries becomes eligible again — a manual queue is a
        deliberate "try this one again". Targeted UPDATE rather than
        ``save()`` to keep clear of the is_new WDQS fetch path.

        ``reset_failures=False`` is for automated callers (the region
        containment repair in ``subjects.tasks``): they queue an item
        because the mirror looks wrong, which is no evidence that a run
        of fetch failures has stopped, and clearing the counter on a
        schedule would keep any item they touch permanently below the
        retry cap.
        """
        now = timezone.now()
        fields = {"sparql_refresh_requested_at": now}
        if reset_failures:
            fields["sparql_fetch_failures"] = 0
        WikidataItem.objects.filter(pk=self.pk).update(**fields)
        self.sparql_refresh_requested_at = now
        if reset_failures:
            self.sparql_fetch_failures = 0

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        if is_new:
            # Sync path is only the cheap entity-JSON fetch: confirms the
            # Q-ID resolves and gives us a label/description to return to
            # the caller. The P31?/P279* ancestor walk + Memgraph load is
            # deferred to ``hydrate_wikidata_item`` on the urgent queue.
            # ``sparql_last_loaded_at`` stays NULL so the Beat refresher
            # picks the row up too if that task never runs.
            if not self.populate_from_wikidata():
                raise ValidationError(
                    f"No English or default label found for {self.wikidata_id}"
                )

        super().save(*args, **kwargs)

        if is_new:
            from .tasks import hydrate_wikidata_item

            qid = self.wikidata_id
            transaction.on_commit(lambda: hydrate_wikidata_item.delay(qid))

    class Meta:
        ordering = ["title"]
        indexes = [
            models.Index(fields=["sparql_last_loaded_at"]),
            # Partial index: the manual-queue lookup runs on every Beat
            # tick but only ever matches the handful of rows an admin has
            # queued, so indexing the (almost always empty) non-null set
            # keeps it off a scan of the 200k+ row table.
            models.Index(
                fields=["sparql_refresh_requested_at"],
                name="wikidataitem_refresh_queued",
                condition=models.Q(sparql_refresh_requested_at__isnull=False),
            ),
            # Trigram index on the preferred English/``mul`` label backs the
            # autocomplete
            # categories query, which uses ``title__icontains`` to find
            # matching ancestors. Django's ``__icontains`` translates to
            # ``ILIKE '%q%'`` on Postgres, which a GIN index with
            # ``gin_trgm_ops`` accelerates from a full scan to an indexed
            # lookup. Requires the ``pg_trgm`` extension (already enabled
            # by ``images/migrations/0035_enhance_search_vector.py``).
            GinIndex(
                fields=["title"],
                name="wikidataitem_title_trgm",
                opclasses=["gin_trgm_ops"],
            ),
        ]


class OsmElement(models.Model):
    """OpenStreetMap element with cached geometry"""

    class OsmType(models.TextChoices):
        NODE = "N", "node"
        WAY = "W", "way"
        RELATION = "R", "relation"

    # Nodes, ways and relations each number from 1, so the id alone is
    # ambiguous. Rows imported before the type was recorded carry NULL until
    # the refresh rotation next sees their subject; new rows are always typed.
    osm_type = models.CharField(
        max_length=1,
        choices=OsmType.choices,
        null=True,
        blank=True,
        help_text="OpenStreetMap element type",
    )
    osm_id = models.BigIntegerField(help_text="OpenStreetMap element ID")
    subject = models.ForeignKey(
        "Subject",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="osm_elements",
        help_text="Subject this OSM element belongs to",
    )
    geometry = gis_models.GeometryField(
        spatial_index=True,
        help_text="Geometry of the OSM element (point, polygon, multipolygon, etc.)",
    )
    centroid = gis_models.PointField(
        null=True,
        blank=True,
        spatial_index=True,
        help_text="Centroid of the geometry (auto-populated on save)",
    )
    geometry_area = models.FloatField(
        default=0,
        help_text="Cached area of the geometry in square degrees (used for render ordering)",
    )
    updated_at = models.DateTimeField(
        auto_now=True, help_text="When this row was last updated"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"OSM Element {self.osm_ref or self.osm_id}"

    @property
    def osm_ref(self):
        """``way/113023444``-style reference, or None until the type is known."""
        if not self.osm_type:
            return None
        return f"{self.OsmType(self.osm_type).label}/{self.osm_id}"

    @property
    def osm_url(self):
        ref = self.osm_ref
        return f"https://www.openstreetmap.org/{ref}" if ref else None

    def save(self, *args, **kwargs):
        """Calculate geometry area and centroid before saving"""
        if self.geometry:
            self.geometry_area = self.geometry.area
            self.centroid = self.geometry.centroid
        super().save(*args, **kwargs)

    class Meta:
        ordering = ["osm_type", "osm_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["osm_type", "osm_id"],
                name="subjects_osmelement_unique_type_id",
            ),
            # Untyped rows keep the bare-id uniqueness they were imported
            # under, so they can't multiply while they wait to be retyped.
            models.UniqueConstraint(
                fields=["osm_id"],
                condition=models.Q(osm_type__isnull=True),
                name="subjects_osmelement_unique_untyped_id",
            ),
        ]


class Person(models.Model):
    first_name = models.CharField(max_length=200, blank=True)
    middle_name = models.CharField(max_length=200, blank=True)
    last_name = models.CharField(max_length=200, blank=True)
    suffix = models.CharField(max_length=50, blank=True, help_text="e.g. Jr., Sr., III")
    birth_date = models.CharField(
        max_length=50, blank=True, help_text="Birth date as EDTF string"
    )
    merged_into = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="merged_from",
        help_text="If set, this person has been merged into another record",
    )

    def __str__(self):
        parts = filter(
            None, [self.first_name, self.middle_name, self.last_name, self.suffix]
        )
        return " ".join(parts) or "Unknown Person"

    class Meta:
        verbose_name_plural = "people"
        ordering = ["last_name", "first_name"]


class Business(models.Model):
    """Can represent any sort of business or similar entity"""

    name = models.CharField(max_length=500)

    def __str__(self):
        return self.name

    class Meta:
        verbose_name_plural = "businesses"
        ordering = ["name"]


class Occupation(models.Model):
    name = models.CharField(max_length=500)

    def __str__(self):
        return self.name

    class Meta:
        ordering = ["name"]


class Subject(models.Model):
    """Subject that can appear in images (buildings, people, monuments, etc.)"""

    title = models.CharField(max_length=500, help_text="Name/title of the subject")
    slug = models.SlugField(unique=True)
    wikidata_item = models.OneToOneField(
        WikidataItem,
        on_delete=models.CASCADE,
        related_name="subject",
        help_text="Linked Wikidata item",
    )
    representative_image = models.ForeignKey(
        "images.Image",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="User-chosen representative image for this subject",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # OSM population tracking (for subjects without an osm_element yet)
    osm_last_checked = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When we last attempted to find an OSM element for this subject",
    )

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.title[:50])
        super().save(*args, **kwargs)

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse("subjects:subject_detail", kwargs={"subject_slug": self.slug})

    def get_description(self):
        """Return a description for display, sourced from the Wikidata item.

        Falls back to a placeholder referencing the Wikidata ID when the
        linked item has no description. Computed on demand, never stored.
        """
        if self.wikidata_item_id:
            return self.wikidata_item.description or (
                f"Subject from Wikidata: {self.wikidata_item.wikidata_id}"
            )
        return ""

    def get_representative_image(self):
        """Return the representative image for this subject, or None.

        ``representative_image`` is kept populated by signals on
        ``SubjectMapping`` (set on first mapping, re-elected on delete), so
        the field is the source of truth. The mapping fallback handles the
        edge case of a Subject with mappings but a null FK (e.g., a row
        predating the backfill).

        The fallback is restricted to publishable images for the same reason
        the signals are: this value is rendered on a public subject page, so a
        Subject whose only mappings are private or duplicate images has no
        representative rather than an undisclosable one.
        """
        if self.representative_image_id is not None:
            return self.representative_image

        first_mapping = (
            self.image_mappings.filter(image__in=representable_images())
            .select_related("image")
            .order_by("order", "id")
            .first()
        )
        return first_mapping.image if first_mapping else None

    class Meta:
        ordering = ["title"]
        indexes = [
            # Trigram index backing the tagging autocomplete
            # (``title__icontains``). Only the ILIKE branch of that query
            # is index-assisted; the ``word_similarity`` annotation is
            # computed per row regardless. See the migration docstring.
            GinIndex(
                fields=["title"],
                name="subject_title_trgm",
                opclasses=["gin_trgm_ops"],
            ),
        ]


class SubjectAncestor(models.Model):
    """Materialized ``Subject -> WikidataItem`` ancestor relation.

    A flat projection of each Subject's category-relevant ancestors, derived
    from the same graph paths the autocomplete used to traverse on the fly
    (``P31?/P279*``, ``P1716``, ``P361``, and ``P361`` as a qualifier on any
    of the Subject's statements). Refreshed per-subject after each Memgraph
    closure load; query-time aggregation/substring-match runs against this
    table with proper indexes instead of via graph traversal.
    """

    subject = models.ForeignKey(
        "Subject",
        on_delete=models.CASCADE,
        related_name="ancestors",
    )
    ancestor = models.ForeignKey(
        "WikidataItem",
        on_delete=models.CASCADE,
        related_name="+",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["subject", "ancestor"],
                name="subjectancestor_unique_pair",
            ),
        ]
        indexes = [
            models.Index(fields=["ancestor"]),
        ]

    def __str__(self):
        return f"{self.subject_id} -> {self.ancestor_id}"
