import django.contrib.gis.db.models.fields
import django.db.models.deletion
from django.contrib.gis.geos import Polygon
from django.db import migrations, models

import maps.models


def backfill_extents(apps, schema_editor):
    layer = apps.get_model("maps", "MapLayer")
    polygon = Polygon.from_bbox((-77.60, 37.40, -77.30, 37.65))
    polygon.srid = 4326
    layer.objects.using(schema_editor.connection.alias).filter(
        collection__isnull=False
    ).update(polygon=polygon)


class Migration(migrations.Migration):
    dependencies = [("maps", "0006_seed_primary_layers")]

    operations = [
        migrations.AddField(
            model_name="maplayer",
            name="polygon",
            field=django.contrib.gis.db.models.fields.PolygonField(
                blank=True,
                null=True,
                srid=4326,
                validators=[maps.models.validate_layer_polygon],
                help_text="Geographic extent of this collection layer. Global layers have no extent.",
            ),
        ),
        migrations.RunPython(backfill_extents, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="maplayer",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(collection__isnull=True, polygon__isnull=True)
                    | models.Q(collection__isnull=False, polygon__isnull=False)
                ),
                name="maplayer_polygon_matches_collection",
            ),
        ),
        migrations.AlterField(
            model_name="maplayer",
            name="collection",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="layers",
                to="maps.layercollection",
                help_text="Collection this layer belongs to (leave empty for Global layers)",
            ),
        ),
        migrations.AlterField(
            model_name="maplayer",
            name="is_default",
            field=models.BooleanField(
                default=False,
                help_text="Whether this is the default base layer (only applies to Global layers)",
            ),
        ),
    ]
