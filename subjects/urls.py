from django.urls import path, register_converter

from . import views
from .converters import SubjectKeyConverter

from subjects import views
from subjects.feeds import SubjectActivityFeed

register_converter(SubjectKeyConverter, "subject")

app_name = "subjects"

urlpatterns = [
    # Subject browsing
    path("", views.browse_subjects, name="browse_subjects"),
    path("map/", views.subjects_map, name="subjects_map"),
    path("<subject:qid>/", views.subject_detail, name="subject_detail"),
    path("<subject:qid>/edit/", views.subject_edit, name="subject_edit"),
    path("<slug:subject_slug>/feed/", SubjectActivityFeed(), name="subject_feed"),
    path(
        "<subject:qid>/similar/",
        views.find_similar_images_to_subject,
        name="subject_similar_images",
    ),
    # Pre-Q-ID URLs (/subjects/<slug>/). These must stay below the routes
    # above: slugs were always lowercase, so they never match the converter's
    # uppercase "Q", and a malformed key that falls through to here 404s.
    path(
        "<slug:legacy_slug>/",
        views.legacy_subject_redirect,
        {"view_name": "subjects:subject_detail"},
        name="legacy_subject_detail",
    ),
    path(
        "<slug:legacy_slug>/similar/",
        views.legacy_subject_redirect,
        {"view_name": "subjects:subject_similar_images"},
        name="legacy_subject_similar_images",
    ),
    # API endpoints
    path(
        "api/autocomplete/",
        views.subject_autocomplete,
        name="subject_autocomplete",
    ),
    path(
        "api/browse-autocomplete/",
        views.browse_autocomplete,
        name="browse_autocomplete",
    ),
    path(
        "api/wikidata-lookup/",
        views.wikidata_lookup,
        name="wikidata_lookup",
    ),
    path("api/all/", views.all_subjects_api, name="all_subjects_api"),
    path(
        "api/map-info/<subject:qid>/",
        views.subject_map_info,
        name="subject_map_info",
    ),
    path(
        "api/bulk-add/",
        views.bulk_add_subject_to_images,
        name="bulk_add_subject_to_images",
    ),
    path(
        "api/image/<int:image_id>/add/",
        views.add_subject_to_image,
        name="add_subject_to_image",
    ),
    path(
        "api/mapping/<int:subject_mapping_id>/remove/",
        views.remove_subject_from_image,
        name="remove_subject_from_image",
    ),
    path(
        "api/subject/<int:subject_id>/representative/<int:image_id>/",
        views.set_representative_image,
        name="set_representative_image",
    ),
    path(
        "api/image/<int:image_id>/reorder/",
        views.reorder_subjects,
        name="reorder_subjects",
    ),
]
