from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("images", "0047_license_unique_name_ci"),
    ]

    operations = [
        migrations.RenameField(
            model_name="image",
            old_name="tile_generation",
            new_name="asset_generation",
        ),
        migrations.AlterField(
            model_name="image",
            name="asset_generation",
            field=models.PositiveIntegerField(
                default=0,
                help_text=(
                    "Incremented each time generated assets (transformed image, "
                    "thumbnail, IIIF tiles) are regenerated; used as a path "
                    "segment to bypass CDN caching."
                ),
            ),
        ),
    ]
