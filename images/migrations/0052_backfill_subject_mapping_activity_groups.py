from django.db import migrations


def backfill_groups(apps, schema_editor):
    """Create one count=1 group per existing SubjectMappingActivity row.

    Pre-grouping data won't get bunched retroactively — every existing
    activity becomes its own group so the feed query (which now reads
    SubjectMappingActivityGroup) has something to return.
    """
    SubjectMappingActivity = apps.get_model("images", "SubjectMappingActivity")
    SubjectMappingActivityGroup = apps.get_model(
        "activity", "SubjectMappingActivityGroup"
    )

    for activity in SubjectMappingActivity.objects.filter(
        group__isnull=True
    ).iterator():
        group = SubjectMappingActivityGroup.objects.create(
            user=activity.user,
            subject=activity.subject,
            action=activity.action,
            started_at=activity.created_at,
            ended_at=activity.created_at,
            count=1,
        )
        activity.group = group
        activity.save(update_fields=["group"])


def noop_reverse(apps, schema_editor):
    """Reverse: delete all auto-created groups.

    Safe because pre-migration there were no groups; everything we delete
    here was created by the forward migration.
    """
    SubjectMappingActivityGroup = apps.get_model(
        "activity", "SubjectMappingActivityGroup"
    )
    SubjectMappingActivityGroup.objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("images", "0051_subjectmappingactivity_group"),
        ("activity", "0004_subjectmappingactivitygroup"),
    ]

    operations = [
        migrations.RunPython(backfill_groups, noop_reverse),
    ]
