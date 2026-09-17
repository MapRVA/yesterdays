import django.db.models.deletion
from django.db import migrations, models


RICHMOND_WIKIDATA_ID = "Q43421"


def assign_existing_collection_layers_to_richmond(apps, schema_editor):
    database = schema_editor.connection.alias
    map_layer = apps.get_model("maps", "MapLayer")
    region = apps.get_model("regions", "Region")
    richmond = (
        region.objects.using(database)
        .filter(wikidata_item__wikidata_id=RICHMOND_WIKIDATA_ID)
        .first()
    )
    if richmond is not None:
        (
            map_layer.objects.using(database)
            .filter(
                collection__isnull=False,
                region__isnull=True,
            )
            .update(region_id=richmond.pk)
        )


class Migration(migrations.Migration):
    dependencies = [
        ("maps", "0008_maplayer_min_zoom"),
        ("regions", "0002_region_advertise"),
    ]

    operations = [
        migrations.AddField(
            model_name="maplayer",
            name="region",
            field=models.ForeignKey(
                blank=True,
                help_text="Region this map layer depicts",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="map_layers",
                to="regions.region",
            ),
        ),
        migrations.RunPython(
            assign_existing_collection_layers_to_richmond,
            migrations.RunPython.noop,
        ),
    ]
