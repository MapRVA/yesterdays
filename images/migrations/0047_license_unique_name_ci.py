from django.db import migrations, models
from django.db.models.functions import Lower


def collapse_duplicate_licenses(apps, schema_editor):
    """Collapse case-insensitive duplicate License rows before the
    UniqueConstraint on Lower(name) is applied.

    For each group of Licenses with the same lowercased name, keep the
    oldest row as canonical, repoint Image.license references to it, then
    delete the rest.
    """
    License = apps.get_model("images", "License")
    Image = apps.get_model("images", "Image")

    canonical_by_key = {}
    for lic in License.objects.order_by("id"):
        key = lic.name.lower()
        canonical_id = canonical_by_key.get(key)
        if canonical_id is None:
            canonical_by_key[key] = lic.id
            continue
        Image.objects.filter(license_id=lic.id).update(license_id=canonical_id)
        lic.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("images", "0046_image_tile_generation"),
    ]

    operations = [
        migrations.RunPython(collapse_duplicate_licenses, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="license",
            constraint=models.UniqueConstraint(
                Lower("name"),
                name="unique_license_name_ci",
            ),
        ),
    ]
