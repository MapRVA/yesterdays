from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("subjects", "0019_wikidataitem_demolished"),
    ]

    operations = [
        migrations.AddField(
            model_name="wikidataitem",
            name="sparql_refresh_requested_at",
            field=models.DateTimeField(
                blank=True,
                help_text="Set when an admin manually queues this item, moving it to the front of the Beat-driven refresh rotation. Cleared when the refresh is picked up.",
                null=True,
            ),
        ),
        migrations.AddIndex(
            model_name="wikidataitem",
            index=models.Index(
                condition=models.Q(("sparql_refresh_requested_at__isnull", False)),
                fields=["sparql_refresh_requested_at"],
                name="wikidataitem_refresh_queued",
            ),
        ),
    ]
