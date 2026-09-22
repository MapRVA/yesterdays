from django.db import migrations


def delete_pre_tracking_introductions(apps, schema_editor):
    """Remove SubjectIntroduction rows for subjects that pre-dated tracking.

    Migration 0005 created the SubjectIntroduction model but did not backfill
    existing subjects. After that migration, the first time any pre-existing
    subject was added to a new image, _record_subject_activity created a
    bogus SubjectIntroduction — surfacing it as a "New Subject" card in the
    activity feed. A genuine introduction is created within the same request
    as the subject's first SubjectMapping; a bogus one points at a subject
    whose earliest mapping pre-dates the introduction.
    """
    SubjectIntroduction = apps.get_model("activity", "SubjectIntroduction")
    SubjectMapping = apps.get_model("images", "SubjectMapping")

    bogus_ids = []
    for intro in SubjectIntroduction.objects.iterator():
        earliest_mapping = (
            SubjectMapping.objects.filter(subject_id=intro.subject_id)
            .order_by("created_at")
            .first()
        )
        if earliest_mapping is None:
            continue
        if earliest_mapping.created_at < intro.created_at:
            bogus_ids.append(intro.pk)

    SubjectIntroduction.objects.filter(pk__in=bogus_ids).delete()


def noop_reverse(apps, schema_editor):
    """Reverse is a noop — we cannot reconstruct deleted bogus rows."""


class Migration(migrations.Migration):
    dependencies = [
        ("activity", "0005_subjectintroduction"),
        ("images", "0052_backfill_subject_mapping_activity_groups"),
    ]

    operations = [
        migrations.RunPython(delete_pre_tracking_introductions, noop_reverse),
    ]
