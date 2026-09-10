from django.db import migrations, models


class Migration(migrations.Migration):
    """Allow at most one anonymous point georeference per image.

    Anonymous callers may only place the *first* georeference on an image.
    ``images.views.georeference.georeference_image`` enforces that under a row
    lock; this partial unique index backs the rule at the database level so a
    future code path cannot reintroduce the race.

    This will fail on a database that already holds two or more anonymous
    georeferences for the same image. That is deliberate: those rows are real
    contributions and which one to keep is an editorial decision, not something
    a migration should make silently. Run
    ``manage.py audit_community_write_invariants`` before deploying to list any
    conflicts, and resolve them first.
    """

    dependencies = [
        ("images", "0064_regions"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="georeference",
            constraint=models.UniqueConstraint(
                condition=models.Q(("georeferenced_by__isnull", True)),
                fields=("image",),
                name="unique_anonymous_georeference_per_image",
            ),
        ),
    ]
