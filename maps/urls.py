from django.urls import path

from . import views

app_name = "maps"

urlpatterns = [
    path("", views.browse_maps, name="browse_maps"),
    path(
        "tiles/<int:z>/<int:x>/<int:y>.mvt",
        views.layer_extent_tile,
        name="layer_extent_tile",
    ),
    # Staff management pages sit ahead of the slug catch-all so the ordering
    # is deliberate rather than accidental.
    path("manage/", views.layer_manage, name="layer_manage"),
    path("manage/new/", views.layer_create, name="layer_create"),
    path("manage/<int:pk>/edit/", views.layer_edit, name="layer_edit"),
    path("manage/<int:pk>/delete/", views.layer_delete, name="layer_delete"),
    path(
        "<slug:collection_slug>/<slug:layer_slug>/",
        views.layer_detail,
        name="layer_detail",
    ),
]
