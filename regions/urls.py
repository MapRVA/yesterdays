from django.urls import path

from . import views

app_name = "regions"

urlpatterns = [
    path("", views.region_index, name="region_index"),
    path("manage/", views.region_manage, name="region_manage"),
    path("manage/new/", views.region_create, name="region_create"),
    path("manage/<int:pk>/edit/", views.region_edit, name="region_edit"),
    path(
        "api/autocomplete/",
        views.region_autocomplete,
        name="region_autocomplete",
    ),
]
