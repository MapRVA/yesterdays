from django.db import migrations


def seed_primary_layers(apps, schema_editor):
    MapLayer = apps.get_model("maps", "MapLayer")

    MapLayer.objects.create(
        name="OpenStreetMap",
        slug="osm",
        type="style",
        url="https://styles.maprva.org/openmaptiles-osm.json",
        is_default=True,
        order=0,
        collection=None,
    )

    MapLayer.objects.create(
        name="USGS Topo",
        slug="usgs-topo",
        type="xyz",
        url="https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}",
        attribution="USGS National Map",
        is_default=False,
        order=1,
        collection=None,
    )


def remove_primary_layers(apps, schema_editor):
    MapLayer = apps.get_model("maps", "MapLayer")
    MapLayer.objects.filter(
        collection__isnull=True, slug__in=["osm", "usgs-topo"]
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("maps", "0005_configurable_primary_layers"),
    ]

    operations = [
        migrations.RunPython(seed_primary_layers, remove_primary_layers),
    ]
