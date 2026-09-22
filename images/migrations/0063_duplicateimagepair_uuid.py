import uuid

from django.db import migrations, models


def populate_uuids(apps, schema_editor):
    """Give each existing pair its own uuid before the unique index goes on."""
    DuplicateImagePair = apps.get_model("images", "DuplicateImagePair")
    for pair in DuplicateImagePair.objects.all().iterator():
        pair.uuid = uuid.uuid4()
        pair.save(update_fields=["uuid"])


class Migration(migrations.Migration):
    dependencies = [
        ("images", "0062_dismissedduplicatepair"),
    ]

    operations = [
        migrations.AddField(
            model_name="duplicateimagepair",
            name="uuid",
            field=models.UUIDField(editable=False, null=True),
        ),
        migrations.RunPython(populate_uuids, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="duplicateimagepair",
            name="uuid",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
    ]
