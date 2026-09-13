from django.urls import path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("users", views.UserViewSet, basename="user")
router.register("sources", views.SourceViewSet, basename="source")
router.register("collections", views.CollectionViewSet, basename="collection")
router.register("images", views.ImageViewSet, basename="image")
router.register("subjects", views.SubjectViewSet, basename="subject")
router.register("licenses", views.LicenseViewSet, basename="license")
router.register("georeferences", views.GeoreferenceViewSet, basename="georeference")
router.register(
    "from-above-georeferences",
    views.FromAboveGeoreferenceViewSet,
    basename="from-above-georeference",
)

urlpatterns = [
    # Nested route: collections scoped to a specific source
    path(
        "sources/<int:source_pk>/collections/",
        views.CollectionViewSet.as_view({"get": "list"}),
        name="source-collections",
    ),
    path("auth/me/", views.me_view, name="api-auth-me"),
    path("apps/", views.register_app_view, name="api-apps-register"),
    path(
        "import/upload-url/", views.import_upload_url_view, name="api-import-upload-url"
    ),
    path("import/commit/", views.import_commit_view, name="api-import-commit"),
    path("import/cancel/", views.import_cancel_view, name="api-import-cancel"),
    path(
        "images/<int:id>/replace/",
        views.image_replace_view,
        name="api-image-replace",
    ),
    path("stats/", views.stats_view, name="api-stats"),
    path("activity/", views.activity_view, name="api-activity"),
    path("search/semantic/", views.semantic_search_view, name="api-semantic-search"),
    path("search/text/", views.text_search_view, name="api-text-search"),
    path("search/in-view/", views.in_view_search_view, name="api-in-view-search"),
] + router.urls
