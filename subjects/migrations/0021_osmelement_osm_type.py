from django.db import migrations, models

# Postpass's point table holds nodes only, so a Point geometry is the one case
# the element type can be inferred. Lines and polygons may be ways or
# relations; they stay NULL until their subject is next refreshed.
INFER_NODE_TYPE_SQL = """
    UPDATE subjects_osmelement
    SET osm_type = 'N'
    WHERE osm_type IS NULL AND ST_GeometryType(geometry) = 'ST_Point'
"""

# Elements were only ever collected inside a bounding box, so every subject
# needs a worldwide re-fetch. An epoch timestamp puts them all at the front of
# the refresh rotation, which orders ascending and would sort NULL last.
RESET_OSM_LAST_CHECKED_SQL = """
    UPDATE subjects_subject
    SET osm_last_checked = '1970-01-01 00:00:00+00'
    WHERE wikidata_item_id IS NOT NULL
"""


class Migration(migrations.Migration):
    dependencies = [
        ("subjects", "0020_wikidataitem_sparql_refresh_requested_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="osmelement",
            name="osm_type",
            field=models.CharField(
                blank=True,
                choices=[("N", "node"), ("W", "way"), ("R", "relation")],
                help_text="OpenStreetMap element type",
                max_length=1,
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="osmelement",
            name="osm_id",
            field=models.BigIntegerField(help_text="OpenStreetMap element ID"),
        ),
        migrations.AlterModelOptions(
            name="osmelement",
            options={"ordering": ["osm_type", "osm_id"]},
        ),
        migrations.AddConstraint(
            model_name="osmelement",
            constraint=models.UniqueConstraint(
                fields=("osm_type", "osm_id"),
                name="subjects_osmelement_unique_type_id",
            ),
        ),
        migrations.AddConstraint(
            model_name="osmelement",
            constraint=models.UniqueConstraint(
                condition=models.Q(("osm_type__isnull", True)),
                fields=("osm_id",),
                name="subjects_osmelement_unique_untyped_id",
            ),
        ),
        migrations.RunSQL(INFER_NODE_TYPE_SQL, migrations.RunSQL.noop),
        migrations.RunSQL(RESET_OSM_LAST_CHECKED_SQL, migrations.RunSQL.noop),
    ]
