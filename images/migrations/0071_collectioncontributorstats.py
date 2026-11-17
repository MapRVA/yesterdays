import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

# Frozen full-table copy of CollectionContributorStats.REFRESH_SQL. The table
# is empty when this runs, so the stale-row arm and the upsert are
# unnecessary — and ON CONFLICT would fail anyway: Django creates a
# NULLS NOT DISTINCT unique constraint only at the end of the migration, after
# this backfill.
BACKFILL_COLLECTION_CONTRIBUTOR_STATS_SQL = """
WITH region_expansion AS (
    SELECT r.id AS region_id, r.id AS counts_toward_id
    FROM regions_region r
    UNION
    SELECT ra.region_id, r2.id
    FROM regions_regionancestor ra
    JOIN regions_region r2 ON r2.wikidata_item_id = ra.ancestor_id
),
georefs AS MATERIALIZED (
    SELECT
        c.id AS collection_id,
        COALESCE(img.region_id, c.region_id, s.region_id) AS region_id,
        g.georeferenced_by_id AS user_id,
        g.georeferenced_at
    FROM images_collection c
    JOIN images_source s ON s.id = c.source_id
    JOIN images_image img
        ON img.collection_id = c.id AND img.duplicate_of_id IS NULL
    JOIN (
        SELECT image_id, georeferenced_by_id, georeferenced_at
        FROM images_georeference
        UNION ALL
        SELECT image_id, georeferenced_by_id, georeferenced_at
        FROM images_aerialgeoreference
    ) g ON g.image_id = img.id
    WHERE g.georeferenced_by_id IS NOT NULL
),
computed AS (
    SELECT
        collection_id, NULL::bigint AS region_id, user_id,
        COUNT(*) AS georeference_count,
        MIN(georeferenced_at) AS first_georeferenced_at
    FROM georefs
    GROUP BY collection_id, user_id
    UNION ALL
    SELECT
        gr.collection_id, exp.counts_toward_id, gr.user_id,
        COUNT(*), MIN(gr.georeferenced_at)
    FROM georefs gr
    JOIN region_expansion exp ON exp.region_id = gr.region_id
    GROUP BY gr.collection_id, exp.counts_toward_id, gr.user_id
)
INSERT INTO images_collectioncontributorstats (
    collection_id, region_id, user_id, georeference_count,
    first_georeferenced_at, updated_at
)
SELECT
    collection_id, region_id, user_id, georeference_count,
    first_georeferenced_at, STATEMENT_TIMESTAMP()
FROM computed
"""


def backfill_collection_contributor_stats(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(BACKFILL_COLLECTION_CONTRIBUTOR_STATS_SQL)


class Migration(migrations.Migration):
    dependencies = [
        ("images", "0070_remove_sitesettings_default_map_view_and_search_bbox"),
        ("regions", "0004_alter_region_bounds_help_text"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="CollectionContributorStats",
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
                ("georeference_count", models.PositiveIntegerField(default=0)),
                (
                    "first_georeferenced_at",
                    models.DateTimeField(blank=True, null=True),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "collection",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="contributor_stats",
                        to="images.collection",
                    ),
                ),
                (
                    "region",
                    models.ForeignKey(
                        blank=True,
                        help_text="Null for the collection-wide row",
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="collection_contributor_stats",
                        to="regions.region",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="collection_contributor_stats",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name_plural": "collection contributor stats",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("collection", "region", "user"),
                        name="collectioncontributorstats_unique",
                        nulls_distinct=False,
                    )
                ],
            },
        ),
        migrations.RunPython(
            backfill_collection_contributor_stats,
            migrations.RunPython.noop,
        ),
    ]
