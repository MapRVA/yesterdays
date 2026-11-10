from django.db import migrations

# The removed fields and their old defaults, which the reverse migration
# backfills before restoring NOT NULL.
REMOVED_FIELDS = {
    "default_map_longitude": -77.43916,
    "default_map_latitude": 37.54376,
    "default_map_zoom": 10.0,
    "default_search_bbox_west": -77.61976,
    "default_search_bbox_south": 37.44393,
    "default_search_bbox_east": -77.36673,
    "default_search_bbox_north": 37.60954,
}


class Migration(migrations.Migration):
    """Maps and geocoders now follow the selected region or show the whole
    world, so the sitewide default view and search bounding box have no
    readers left. Per-region bounds live on regions.Region.

    The fields leave Django's state but their columns stay, made nullable so
    the new model can still insert a SiteSettings row. The pre-upgrade job
    runs this before the old pods are replaced, and the old site_settings
    context processor reads these columns on every request, so dropping them
    here would fail every page until the rollout finished. A later release
    drops the columns once no old code is left running."""

    dependencies = [
        ("images", "0069_pe_core_embeddings"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=f"ALTER TABLE images_sitesettings ALTER COLUMN {name} "
                    "DROP NOT NULL",
                    reverse_sql=(
                        f"UPDATE images_sitesettings SET {name} = {default} "
                        f"WHERE {name} IS NULL; "
                        f"ALTER TABLE images_sitesettings ALTER COLUMN {name} "
                        "SET NOT NULL"
                    ),
                )
                for name, default in REMOVED_FIELDS.items()
            ],
            state_operations=[
                migrations.RemoveField(model_name="sitesettings", name=name)
                for name in REMOVED_FIELDS
            ],
        ),
    ]
