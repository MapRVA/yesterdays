import uuid
from itertools import chain
from operator import attrgetter

from django.contrib.syndication.views import Feed
from django.urls import reverse
from django.utils.feedgenerator import Rss201rev2Feed


from activity.models import (
    GeoreferenceGroup,
    SitewideMilestone,
    SubjectIntroduction,
    UserMilestone,
    CollectionIntroduction,
)

MAX_ITEMS = 50


class SitewideActivityFeed(Feed):
    """
    Site-wide RSS feed showing the latest georeferences, subjects, and milestones.
    """
    feed_type = Rss201rev2Feed
    title = "Yesterdays - Site-wide Activity"
    link = "/activity/"  # Update with your main activity page URL
    description = "The latest georeferences subjects, and milestones in Yesterdays."

    def items(self):
        # Fetch the most recent items of each activity type.
        georefs = GeoreferenceGroup.objects.all().order_by('-ended_at')[:MAX_ITEMS]
        user_milestones = UserMilestone.objects.all().order_by('-reached_at')[:MAX_ITEMS]
        sitewide_milestones = SitewideMilestone.objects.all().order_by('-created_at')[:MAX_ITEMS]
        subjects = SubjectIntroduction.objects.all().order_by('-reached_at')[:MAX_ITEMS]
        collections = CollectionIntroduction.objects.all().order_by('-created_at')[:MAX_ITEMS]

        # Chain them together and sort chronologically
        combined = sorted(
            chain(georefs, subjects, user_milestones, sitewide_milestones, collections),
            key=attrgetter('created_at', 'reached_at', 'ended_at'),
            reverse=True,
        )
        return combined[:MAX_ITEMS]

    def item_title(self, item):
        if isinstance(item, GeoreferenceGroup):
            return f"Images georeferenced by {getattr(item, 'user', 'a user')}"
        elif isinstance(item, SubjectIntroduction):
            return f"New subject added: {item.subject.title}"
        elif isinstance(item, UserMilestone):
            return f"{item.user.username} reached {item.count} georeferences!"
        elif isinstance(item, SitewideMilestone):
            return f"Yesterdays reached {item.count} images georeferenced!"
        elif isinstance(item, CollectionIntroduction):
            return f"New collection added: {item.collection.name}"
        return str(item)

    def item_description(self, item):
        if isinstance(item, GeoreferenceGroup):
            count = getattr(item, 'count', 'Multiple')
            return f"{count} images were recently georeferenced."
        elif isinstance(item, SubjectIntroduction):
            return "A new subject was introduced to the catalog."
        elif isinstance(item, UserMilestone):
            return "A user has reached a new georeferencing milestone."
        elif isinstance(item, SitewideMilestone):
            return "The community has reached a new site-wide milestone."
        elif isinstance(item, CollectionIntroduction):
            return "A new collection was introduced to the catalog."
        return str(item)

    def item_link(self, item):
        # Default to method if the model has one. Covers Image, Subject, Collection
        if hasattr(item, 'get_absolute_url'):
            return item.get_absolute_url()
        elif isinstance(item, SubjectIntroduction):
            return item.subject.get_absolute_url()
        elif isinstance(item, UserMilestone):
            return reverse("user_profile", kwargs={"username": item.user.username})
        elif isinstance(item, CollectionIntroduction):
            return item.collection.get_absolute_url()

        # GeoreferenceGroup and SitewideMilestone go nowhere?
        return "/activity/"

    def item_pubdate(self, item):
        return getattr(item, 'created_at', None) or getattr(item, 'reached_at', None) or getattr(item, 'ended_at', None)

    def item_guid(self, item):
        """
        Set a random UUID for each item, so that the exact same session can appear in
        multiple feeds if necessary and won't be filtered by RSS clients.
        """
        return str(uuid.uuid4())
