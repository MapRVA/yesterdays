import math

from django.core.validators import MaxValueValidator
from django.db import migrations, models


# The editor renders a 400x240 picker with 24px of padding. MapLibre's world
# size is 512px at zoom zero, so this calculation matches the browser default.
PICKER_WIDTH = 400
PICKER_HEIGHT = 240
PICKER_PADDING = 24
WORLD_SIZE = 512
MAX_MERCATOR_LATITUDE = 85.051129


def project(longitude, latitude):
    latitude = max(-MAX_MERCATOR_LATITUDE, min(MAX_MERCATOR_LATITUDE, latitude))
    latitude_radians = math.radians(latitude)
    x = (longitude + 180) / 360
    y = 0.5 - math.log(
        (1 + math.sin(latitude_radians)) / (1 - math.sin(latitude_radians))
    ) / (4 * math.pi)
    return x, y


def fitted_zoom(polygon):
    center_x, center_y = project(polygon.centroid.x, polygon.centroid.y)
    max_x_distance = 0
    max_y_distance = 0
    for ring in polygon.coords:
        for longitude, latitude, *_ in ring:
            x, y = project(longitude, latitude)
            x_distance = abs(x - center_x)
            max_x_distance = max(max_x_distance, min(x_distance, 1 - x_distance))
            max_y_distance = max(max_y_distance, abs(y - center_y))

    half_width = (PICKER_WIDTH - 2 * PICKER_PADDING) / 2
    half_height = (PICKER_HEIGHT - 2 * PICKER_PADDING) / 2
    limits = []
    if max_x_distance:
        limits.append(math.log2(half_width / (WORLD_SIZE * max_x_distance)))
    if max_y_distance:
        limits.append(math.log2(half_height / (WORLD_SIZE * max_y_distance)))
    zoom = math.floor(min(limits)) if limits else 24
    return max(0, min(24, zoom))


def backfill_min_zoom(apps, schema_editor):
    map_layer = apps.get_model("maps", "MapLayer")
    layers = map_layer.objects.using(schema_editor.connection.alias).filter(
        collection__isnull=False,
        polygon__isnull=False,
    )
    for layer in layers.iterator():
        map_layer.objects.using(schema_editor.connection.alias).filter(
            pk=layer.pk
        ).update(min_zoom=fitted_zoom(layer.polygon))


class Migration(migrations.Migration):
    dependencies = [("maps", "0007_maplayer_polygon")]

    operations = [
        migrations.AddField(
            model_name="maplayer",
            name="min_zoom",
            field=models.PositiveSmallIntegerField(
                blank=True,
                null=True,
                validators=[MaxValueValidator(24)],
                help_text="Lowest whole-number zoom for this collection layer (0–24).",
            ),
        ),
        migrations.RunPython(backfill_min_zoom, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="maplayer",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(collection__isnull=True, min_zoom__isnull=True)
                    | models.Q(
                        collection__isnull=False,
                        min_zoom__isnull=False,
                        min_zoom__gte=0,
                        min_zoom__lte=24,
                    )
                ),
                name="maplayer_min_zoom_matches_collection",
            ),
        ),
    ]
