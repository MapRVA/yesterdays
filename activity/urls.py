from django.urls import path

from activity import views
from activity.feeds import SitewideActivityFeed

app_name = "activity"

urlpatterns = [
    path("", views.activity_feed, name="feed"),
    path("feed/",SitewideActivityFeed(), name="site-feed")
]
