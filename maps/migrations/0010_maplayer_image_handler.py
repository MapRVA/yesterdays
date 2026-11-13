from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("maps", "0009_maplayer_region"),
    ]

    operations = [
        migrations.AddField(
            model_name="maplayer",
            name="image_handler",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "None"),
                    (
                        "americana",
                        "OpenStreetMap Americana (route shields and POI icons)",
                    ),
                ],
                default="",
                help_text=(
                    "Draws images the style generates at runtime instead of "
                    "loading them from its sprite, such as route shields. Only "
                    "applies to MapLibre Style layers."
                ),
                max_length=32,
            ),
        ),
    ]
