import django.contrib.gis.db.models.fields
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("regions", "0003_alter_region_subtitle"),
    ]

    operations = [
        migrations.AlterField(
            model_name="region",
            name="map_bounds",
            field=django.contrib.gis.db.models.fields.PolygonField(
                blank=True,
                help_text=(
                    "Initial map viewport for this region. Stored as a "
                    "rectangular WGS84 polygon and fitted to the available "
                    "screen size. Blank falls back to the region centerpoint "
                    "at a fixed zoom."
                ),
                null=True,
                spatial_index=False,
                srid=4326,
            ),
        ),
        migrations.AlterField(
            model_name="region",
            name="geocoder_bounds",
            field=django.contrib.gis.db.models.fields.PolygonField(
                blank=True,
                help_text=(
                    "Optional Nominatim search area for this region. Blank "
                    "uses the map viewport; if that is also blank, searches "
                    "are unbounded and detect_addresses skips this region's "
                    "images."
                ),
                null=True,
                spatial_index=False,
                srid=4326,
            ),
        ),
    ]
