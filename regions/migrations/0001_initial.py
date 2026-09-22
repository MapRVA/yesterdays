import django.contrib.gis.db.models.fields
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("images", "0063_duplicateimagepair_uuid"),
        ("subjects", "0019_wikidataitem_demolished"),
    ]

    operations = [
        migrations.CreateModel(
            name="Region",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "short_name",
                    models.CharField(
                        help_text=(
                            "Compact name shown in the navbar selector (e.g. Richmond)"
                        ),
                        max_length=500,
                    ),
                ),
                (
                    "long_name",
                    models.CharField(
                        help_text=(
                            "Full, disambiguated name shown in region lists "
                            "(e.g. Richmond, Virginia)"
                        ),
                        max_length=500,
                    ),
                ),
                (
                    "subtitle",
                    models.CharField(
                        blank=True,
                        help_text=(
                            "Tagline shown under the region homepage title; falls "
                            "back to the sitewide subtitle when blank"
                        ),
                        max_length=500,
                    ),
                ),
                ("slug", models.SlugField(unique=True)),
                (
                    "wikidata_coordinate_location",
                    django.contrib.gis.db.models.fields.PointField(
                        blank=True,
                        help_text=(
                            "Wikidata P625 (coordinate location) of the linked "
                            "item, fetched by the admin on save (see "
                            "regions.wikidata_check) and refreshed from each "
                            "closure load, which never clears it. Used as the map "
                            "centerpoint unless overridden below. Null for items "
                            "Wikidata has no P625 for, which then have to carry a "
                            "custom centerpoint."
                        ),
                        null=True,
                        srid=4326,
                    ),
                ),
                (
                    "custom_coordinate_location",
                    django.contrib.gis.db.models.fields.PointField(
                        blank=True,
                        help_text=(
                            "Map centerpoint chosen by an admin, overriding "
                            "Wikidata's. Blank follows P625, which marks a place's "
                            "official point (a city hall, a centroid) and isn't "
                            "always where its map should open. Never touched by "
                            "the Wikidata refresh."
                        ),
                        null=True,
                        srid=4326,
                    ),
                ),
                (
                    "map_bounds",
                    django.contrib.gis.db.models.fields.PolygonField(
                        blank=True,
                        help_text=(
                            "Initial map viewport for this region. Stored as a "
                            "rectangular WGS84 polygon and fitted to the available "
                            "screen size. Blank falls back to the region "
                            "centerpoint and sitewide zoom."
                        ),
                        null=True,
                        spatial_index=False,
                        srid=4326,
                    ),
                ),
                (
                    "geocoder_bounds",
                    django.contrib.gis.db.models.fields.PolygonField(
                        blank=True,
                        help_text=(
                            "Optional Nominatim search area for this region. Blank "
                            "uses the map viewport, then the sitewide search bounds "
                            "if the viewport is also blank."
                        ),
                        null=True,
                        spatial_index=False,
                        srid=4326,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "representative_image",
                    models.ForeignKey(
                        blank=True,
                        help_text=(
                            "Hand-picked photograph shown for this region in "
                            "region cards and map markers. Leave blank to show the "
                            "placeholder."
                        ),
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="images.image",
                    ),
                ),
                (
                    "wikidata_item",
                    models.OneToOneField(
                        help_text="Linked Wikidata item",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="region",
                        to="subjects.wikidataitem",
                    ),
                ),
            ],
            options={
                "ordering": ["short_name"],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(wikidata_coordinate_location__isnull=False)
                        | models.Q(custom_coordinate_location__isnull=False),
                        name="region_has_a_coordinate",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="RegionAncestor",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "ancestor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="subjects.wikidataitem",
                    ),
                ),
                (
                    "region",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ancestors",
                        to="regions.region",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["ancestor"], name="regions_reg_ancesto_d70e0a_idx"
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("region", "ancestor"),
                        name="regionancestor_unique_pair",
                    )
                ],
            },
        ),
    ]
