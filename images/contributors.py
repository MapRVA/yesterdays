"""Who georeferenced a page's images, for the avatar stack on browse pages."""

from django.db.models import F, Min, Sum
from django.urls import reverse

from .models import CollectionContributorStats


def get_top_georeferencers(collection_ids, region=None, limit=5):
    """Rank the users who georeferenced images in some collections.

    Reads the denormalized CollectionContributorStats table, so the cost is
    the number of contributors, not the number of georeferences. Point and
    aerial georeferences both count; anonymous ones don't (see the model).
    Up to ``limit`` users who have a profile picture and a public profile
    page are returned in ``shown``, most georeferences first, ties going to
    whoever started first. All other contributors are counted in
    ``more_count``.

    Args:
        collection_ids: the collections whose georeferences count.
        region: count only images in this region (and the regions inside
            it), matching Image.objects.in_region, or None for all of them.
        limit: the most avatars to show.

    Returns:
        dict with ``shown`` (list of dicts with ``name``, ``url``,
        ``picture_url``, and ``count``) and ``more_count`` (int).
    """
    region_filter = {"region": region} if region else {"region__isnull": True}
    contributors = (
        CollectionContributorStats.objects.filter(
            collection_id__in=collection_ids,
            georeference_count__gt=0,
            **region_filter,
        )
        .values(
            "user_id",
            name=F("user__first_name"),
            picture_url=F("user__profile__profile_picture_url"),
        )
        .annotate(
            count=Sum("georeference_count"),
            first=Min("first_georeferenced_at"),
        )
    )
    total = contributors.count()
    # Public profile URLs carry the OSM username (first_name), so a user
    # without one has no page to link to and joins the "+N more" instead
    shown = list(
        contributors.filter(name__gt="", picture_url__gt="").order_by(
            "-count", "first", "user_id"
        )[:limit]
    )
    for contributor in shown:
        contributor["url"] = reverse("user_profile", args=[contributor["name"]])
    return {"shown": shown, "more_count": total - len(shown)}
