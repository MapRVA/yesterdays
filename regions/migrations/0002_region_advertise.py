from django.db import migrations, models


def advertise_existing_destinations(apps, schema_editor):
    Region = apps.get_model("regions", "Region")
    RegionAncestor = apps.get_model("regions", "RegionAncestor")

    grouping_item_ids = RegionAncestor.objects.values("ancestor_id")
    Region.objects.exclude(wikidata_item_id__in=grouping_item_ids).update(
        advertise=True
    )


class Migration(migrations.Migration):
    dependencies = [
        ("regions", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="region",
            name="advertise",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Show this region on selector maps, homepage region cards, "
                    "the default region directory, and the navbar's Popular "
                    "list. Regions remain available through search when this "
                    "is off."
                ),
            ),
        ),
        migrations.RunPython(
            advertise_existing_destinations,
            migrations.RunPython.noop,
        ),
    ]
