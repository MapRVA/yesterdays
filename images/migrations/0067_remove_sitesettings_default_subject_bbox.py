from django.db import migrations


class Migration(migrations.Migration):
    """Subject OSM geometry is now fetched worldwide, so the bounding box that
    used to limit Postpass queries has no readers left."""

    dependencies = [
        ("images", "0066_georeference_proximity_indexes"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="sitesettings",
            name="default_subject_bbox_west",
        ),
        migrations.RemoveField(
            model_name="sitesettings",
            name="default_subject_bbox_south",
        ),
        migrations.RemoveField(
            model_name="sitesettings",
            name="default_subject_bbox_east",
        ),
        migrations.RemoveField(
            model_name="sitesettings",
            name="default_subject_bbox_north",
        ),
    ]
