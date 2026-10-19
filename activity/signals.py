from django.db import connection, transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from images.models import AerialGeoreference, Georeference

from .models import (
    GROUPING_WINDOW,
    MILESTONE_THRESHOLDS,
    SITEWIDE_MILESTONE_THRESHOLDS,
    GeoreferenceGroup,
    GeoreferenceGroupMember,
    SitewideMilestone,
    UserMilestone,
)


@receiver(post_save, sender=Georeference)
def handle_georeference_created(sender, instance, created, **kwargs):
    """Create or update GeoreferenceGroup when a point georeference is created."""
    if not created:
        return

    # Always check sitewide milestones, even for anonymous submissions
    transaction.on_commit(lambda: _check_sitewide_milestone(instance.georeferenced_at))

    transaction.on_commit(
        lambda: _process_georeference(
            user=instance.georeferenced_by,  # May be None for anonymous
            timestamp=instance.georeferenced_at,
            georeference=instance,
            aerial_georeference=None,
        )
    )


@receiver(post_save, sender=AerialGeoreference)
def handle_aerial_georeference_created(sender, instance, created, **kwargs):
    """Create or update GeoreferenceGroup when an aerial georeference is created."""
    if not created:
        return

    # Always check sitewide milestones, even for anonymous submissions
    transaction.on_commit(lambda: _check_sitewide_milestone(instance.georeferenced_at))

    transaction.on_commit(
        lambda: _process_georeference(
            user=instance.georeferenced_by,  # May be None for anonymous
            timestamp=instance.georeferenced_at,
            georeference=None,
            aerial_georeference=instance,
        )
    )


def _process_georeference(user, timestamp, georeference, aerial_georeference):
    """
    Process a new georeference: add to existing group or create new one,
    and check for milestones.

    For anonymous submissions (user=None), all anonymous georeferences within
    the 3-hour window are grouped together.
    """
    # Find the most recent group for this user (or anonymous group if user is None)
    if user:
        latest_group = (
            GeoreferenceGroup.objects.filter(user=user).order_by("-ended_at").first()
        )
    else:
        latest_group = (
            GeoreferenceGroup.objects.filter(user__isnull=True)
            .order_by("-ended_at")
            .first()
        )

    # If within 3 hours of last activity, add to existing group
    if latest_group and (timestamp - latest_group.ended_at) < GROUPING_WINDOW:
        # A late-arriving or backdated georeference mustn't pull ended_at
        # below a newer member's: the feed treats it as the latest one's.
        latest_group.ended_at = max(latest_group.ended_at, timestamp)
        latest_group.count += 1
        latest_group.save(update_fields=["ended_at", "count"])
        group = latest_group
    else:
        # Start new group
        group = GeoreferenceGroup.objects.create(
            user=user,
            started_at=timestamp,
            ended_at=timestamp,
            count=1,
        )

    # Create the member link
    GeoreferenceGroupMember.objects.create(
        group=group,
        georeference=georeference,
        aerial_georeference=aerial_georeference,
        added_at=timestamp,
    )

    # Check for user milestones (only for logged-in users)
    if user:
        _check_milestone(user, timestamp)


def _check_milestone(user, timestamp):
    """Check if the user has crossed a milestone threshold and record it."""
    total = (
        Georeference.objects.filter(georeferenced_by=user).count()
        + AerialGeoreference.objects.filter(georeferenced_by=user).count()
    )

    for milestone in MILESTONE_THRESHOLDS:
        if total >= milestone:
            UserMilestone.objects.get_or_create(
                user=user,
                count=milestone,
                defaults={"reached_at": timestamp},
            )


def _check_sitewide_milestone(timestamp):
    """Check if the site has crossed a sitewide milestone threshold and record it."""
    query = """
        SELECT COUNT(*) FROM (
            SELECT g.image_id
            FROM images_georeference g
            JOIN images_image i ON g.image_id = i.id
            WHERE i.duplicate_of_id IS NULL AND i.will_not_georef = FALSE
              AND i.aerial = FALSE
            UNION
            SELECT ag.image_id
            FROM images_aerialgeoreference ag
            JOIN images_image i ON ag.image_id = i.id
            WHERE i.duplicate_of_id IS NULL AND i.will_not_georef = FALSE
              AND i.aerial = TRUE
        ) AS all_georeferenced
    """
    with connection.cursor() as cursor:
        cursor.execute(query)
        total = cursor.fetchone()[0]

    for milestone in SITEWIDE_MILESTONE_THRESHOLDS:
        if total >= milestone:
            SitewideMilestone.objects.get_or_create(
                count=milestone,
                defaults={"reached_at": timestamp},
            )
