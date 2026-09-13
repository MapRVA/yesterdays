from django.urls import path

from . import views

app_name = "images"

urlpatterns = [
    # Browse interface
    path("browse/", views.browse_sources, name="browse_sources"),
    path("browse/<slug:slug>/", views.source_detail, name="source_detail"),
    path(
        "browse/<slug:source_slug>/<slug:collection_slug>/",
        views.collection_detail,
        name="collection_detail",
    ),
    path("from-above/", views.browse_aerials, name="browse_aerials"),
    # Community favorites
    path("favorites/", views.top_rated_images, name="favorites"),
    # Georeferencing interface
    path("georeference/", views.georeference_interface, name="georeference_interface"),
    path(
        "polygonal-georeference/<int:image_id>/",
        views.aerial_georeference_interface,
        name="aerial_georeference_interface",
    ),
    # Image of the Day queue (staff)
    path(
        "featured-image/queue/", views.featured_image_queue, name="featured_image_queue"
    ),
    path(
        "featured-image/<int:pk>/edit/",
        views.featured_image_edit,
        name="featured_image_edit",
    ),
    path(
        "featured-image/<int:pk>/delete/",
        views.featured_image_delete,
        name="featured_image_delete",
    ),
    path(
        "featured-image/users/autocomplete/",
        views.user_autocomplete,
        name="featured_image_user_autocomplete",
    ),
    # Validation queue (staff)
    path("validation-queue/", views.validation_queue, name="validation_queue"),
    # Duplicate image pair review (staff)
    path(
        "duplicate-pairs/",
        views.duplicate_image_pairs,
        name="duplicate_image_pairs",
    ),
    path(
        "duplicate-pairs/<uuid:pair_uuid>/",
        views.duplicate_pair_detail,
        name="duplicate_pair_detail",
    ),
    path(
        "duplicate-pairs/<uuid:pair_uuid>/resolve/",
        views.resolve_duplicate_pair,
        name="resolve_duplicate_pair",
    ),
    path(
        "duplicate-pairs/<uuid:pair_uuid>/dismiss/",
        views.dismiss_duplicate_pair,
        name="dismiss_duplicate_pair",
    ),
    path(
        "duplicate-pairs/<uuid:pair_uuid>/restore/",
        views.restore_duplicate_pair,
        name="restore_duplicate_pair",
    ),
    # List and detail views
    path("", views.image_list, name="image_list"),
    path("stats/", views.image_stats, name="image_stats"),
    path("search/", views.search_page, name="search_page"),
    path("random/", views.get_random_image, name="random_image"),
    # API endpoints for georeferencing (must come before generic <int:image_id>/)
    path(
        "<int:image_id>/georeference/",
        views.georeference_image,
        name="georeference_image",
    ),
    path(
        "<int:image_id>/georeference-polygonal/",
        views.aerial_georeference_image,
        name="aerial_georeference_image",
    ),
    path(
        "<int:image_id>/similar/", views.find_similar_images, name="find_similar_images"
    ),
    path(
        "<int:image_id>/add-comment/",
        views.add_comment,
        name="add_comment",
    ),
    path(
        "<int:image_id>/rate/",
        views.submit_rating,
        name="submit_rating",
    ),
    path("<int:image_id>/skip/", views.skip_image, name="skip_image"),
    path(
        "georeference/<int:georeference_id>/validate/",
        views.validate_georeference,
        name="validate_georeference",
    ),
    # Generic image detail view (must come last)
    path("<int:image_id>/", views.image_detail, name="image_detail"),
    # Image management endpoints
    path("<int:image_id>/difficulty/", views.mark_difficulty, name="mark_difficulty"),
    path("<int:image_id>/scale/", views.mark_scale, name="mark_scale"),
    path(
        "<int:image_id>/will-not-georef/",
        views.mark_will_not_georef,
        name="mark_will_not_georef",
    ),
    path(
        "<int:image_id>/from-above/",
        views.mark_aerial,
        name="mark_aerial",
    ),
    # Bulk image management (staff only)
    path(
        "api/v1/bulk/from-above/",
        views.bulk_mark_aerial,
        name="bulk_mark_aerial",
    ),
    path(
        "api/v1/bulk/will-not-georef/",
        views.bulk_mark_will_not_georef,
        name="bulk_mark_will_not_georef",
    ),
    path(
        "<int:image_id>/queue-featured-image/",
        views.queue_featured_image,
        name="queue_featured_image",
    ),
    path("admin/label-scales/", views.label_scales, name="label_scales"),
    path("admin/update-scale/", views.update_image_scale, name="update_image_scale"),
    # Public API endpoints
    path("api/v1/geojson/", views.geojson_endpoint, name="geojson"),
    path("api/v1/geojson/above/", views.aerial_geojson_endpoint, name="aerial_geojson"),
    path(
        "api/v1/above/at-point/",
        views.polygonal_georeferences_at_point,
        name="aerial_at_point",
    ),
    path(
        "api/v1/osm_element_tiles/<int:z>/<int:x>/<int:y>.pbf",
        views.osm_elements_vector_tiles_endpoint,
        name="osm_elements_tiles",
    ),
    path(
        "api/v1/tiles/v<int:v>/<int:z>/<int:x>/<int:y>.mvt",
        views.vector_tiles_endpoint,
        name="vector_tiles",
    ),
    # Third-party tile consumers likely won't use our cache-busting versioning:
    path(
        "api/v1/tiles/<int:z>/<int:x>/<int:y>.mvt",
        views.vector_tiles_endpoint,
        name="vector_tiles_unversioned",
    ),
    path("api/v1/search/", views.semantic_search, name="semantic_search"),
    path("api/v1/search/text/", views.text_search, name="text_search"),
    path("api/v1/search/in-view/", views.in_view_search, name="in_view_search"),
    path(
        "api/v1/search/reverse/",
        views.reverse_image_search,
        name="reverse_image_search",
    ),
    # Embeddable map URL
    path("map/embed/", views.map_embed, name="map_embed"),
    # Album management API endpoints
    path("api/v1/user-albums/", views.user_albums_api, name="user_albums_api"),
    path("api/v1/add-to-album/", views.add_image_to_album, name="add_image_to_album"),
    path(
        "api/v1/create-and-add-to-album/",
        views.create_and_add_to_album,
        name="create_and_add_to_album",
    ),
    path(
        "api/v1/remove-from-album/",
        views.remove_image_from_album,
        name="remove_image_from_album",
    ),
    # Bulk album operations
    path(
        "api/v1/albums/bulk-add/",
        views.bulk_add_to_album,
        name="bulk_add_to_album",
    ),
    path(
        "api/v1/albums/bulk-create-and-add/",
        views.bulk_create_and_add_to_album,
        name="bulk_create_and_add_to_album",
    ),
    path(
        "album/<uuid:album_id>/toggle-public/",
        views.toggle_album_public,
        name="toggle_album_public",
    ),
    path("album/<uuid:album_id>/edit/", views.edit_album, name="edit_album"),
    path("album/<uuid:album_id>/delete/", views.delete_album, name="delete_album"),
    # Album detail view
    path(
        "album/<uuid:album_id>/",
        views.album_detail,
        name="album_detail",
    ),
]
