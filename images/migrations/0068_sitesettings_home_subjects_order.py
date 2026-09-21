from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("images", "0067_remove_sitesettings_default_subject_bbox"),
    ]

    operations = [
        migrations.AddField(
            model_name="sitesettings",
            name="home_subjects_order",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text=(
                    "Subject ids in the order the homepage's subjects band "
                    "reads its labels. Empty means it follows the order "
                    "curators gave the subjects on the image page. Cleared "
                    "whenever the photograph above changes, since an order is "
                    "only ever curated against one photograph's subjects."
                ),
            ),
        ),
    ]
