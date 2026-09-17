import json

from django.contrib.auth.models import User
from django.contrib.gis.geos import Polygon
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from images.context_processors import _build_map_layers_data

from .forms import MapLayerForm
from .models import LayerCollection, MapLayer

PMTILES_URL = "https://example.com/tiles/richmond-1876.pmtiles"
XYZ_URL = "https://example.com/tiles/{z}/{x}/{y}.png"
STYLE_URL = "https://example.com/styles/basemap.json"
MIN_ZOOM = 10


def example_polygon():
    polygon = Polygon.from_bbox((-77.48, 37.50, -77.40, 37.58))
    polygon.srid = 4326
    return polygon


class LayerExtentTileTests(TestCase):
    def setUp(self):
        self.collection = LayerCollection.objects.create(name="Local maps", slug="local")
        self.layer = MapLayer.objects.create(
            name="Local sheet", slug="local", url=PMTILES_URL,
            collection=self.collection, polygon=example_polygon(), min_zoom=10,
        )

    def tile(self, z=9, x=145, y=198, **headers):
        return self.client.get(
            reverse("maps:layer_extent_tile", args=[z, x, y]), **headers
        )

    def test_maxzoom_includes_layers_with_higher_minimum_zoom(self):
        response = self.tile()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/vnd.mapbox-vector-tile")
        # MVT properties are encoded in the tile's string dictionary.
        for value in (b"layer_extents", b"Local sheet", b"Local maps", b"min_zoom"):
            self.assertIn(value, response.content)
        self.layer.min_zoom = 24
        self.layer.save()
        self.assertIn(b"Local sheet", self.tile().content)

    def test_lower_zoom_tiles_filter_by_minimum_zoom(self):
        self.assertEqual(self.tile(z=8, x=72, y=99).content, b"")
        self.layer.min_zoom = 8
        self.layer.save()
        self.assertIn(b"Local sheet", self.tile(z=8, x=72, y=99).content)

    def test_only_intersecting_collection_layers_are_included(self):
        MapLayer.objects.create(name="Global basemap", slug="global", url=PMTILES_URL)
        self.assertNotIn(b"Global basemap", self.tile().content)
        self.assertEqual(self.tile(x=0, y=0).content, b"")
        self.layer.polygon = Polygon.from_bbox((10, 10, 11, 11))
        self.layer.save()
        self.assertEqual(self.tile().content, b"")

    def test_tiny_polygons_are_retained_for_discovery(self):
        self.layer.polygon = Polygon.from_bbox((-77.440001, 37.540001, -77.44, 37.540002))
        self.layer.save()
        self.assertIn(b"Local sheet", self.tile().content)

    @override_settings(MAP_LAYER_TILE_CACHE_SECONDS=600)
    def test_cache_headers_and_conditional_requests(self):
        response = self.tile()
        self.assertIn("public", response["Cache-Control"])
        self.assertIn("max-age=600", response["Cache-Control"])
        self.assertEqual(self.tile(HTTP_IF_NONE_MATCH=response["ETag"]).status_code, 304)
        self.layer.name = "Renamed sheet"
        self.layer.save()
        changed = self.tile(HTTP_IF_NONE_MATCH=response["ETag"])
        self.assertEqual(changed.status_code, 200)
        self.assertNotEqual(changed["ETag"], response["ETag"])
        empty = self.tile(x=0, y=0)
        self.assertIn("max-age=600", empty["Cache-Control"])

    def test_page_payload_contains_only_basemaps_and_coarse_tileset(self):
        with self.assertNumQueries(1):
            data = _build_map_layers_data()
        self.assertNotIn("collections", data)
        self.assertEqual(data["overlay_tiles"], {
            "url": "/layers/tiles/{z}/{x}/{y}.mvt", "maxzoom": 9,
        })
        self.assertNotIn(
            self.layer.name, [layer["name"] for layer in data["primary_layers"]]
        )

    def test_invalid_tile_coordinates_and_methods(self):
        for z, x, y in ((10, 0, 0), (0, 1, 0), (9, 512, 0), (9, 0, 512), (999, 0, 0)):
            with self.subTest(z=z, x=x, y=y):
                self.assertEqual(self.tile(z, x, y).status_code, 404)
        self.assertEqual(self.client.post(
            reverse("maps:layer_extent_tile", args=[9, 145, 198])
        ).status_code, 405)


class MapLayerExtentMigrationTests(TransactionTestCase):
    def test_backfill_only_collection_layers(self):
        before = [("maps", "0006_seed_primary_layers")]
        after = [("maps", "0007_maplayer_polygon")]
        executor = MigrationExecutor(connection)
        executor.migrate(before)
        try:
            old_apps = executor.loader.project_state(before).apps
            collection = old_apps.get_model("maps", "LayerCollection").objects.create(
                name="Migration collection", slug="migration-collection"
            )
            layer_model = old_apps.get_model("maps", "MapLayer")
            local = layer_model.objects.create(
                name="Local",
                slug="migration-local",
                url=PMTILES_URL,
                collection=collection,
            )
            global_layer = layer_model.objects.create(
                name="Global",
                slug="migration-global",
                url=PMTILES_URL,
            )
            executor = MigrationExecutor(connection)
            executor.migrate(after)
            new_model = executor.loader.project_state(after).apps.get_model(
                "maps", "MapLayer"
            )
            self.assertEqual(
                new_model.objects.get(pk=local.pk).polygon.extent,
                (-77.60, 37.40, -77.30, 37.65),
            )
            self.assertIsNone(new_model.objects.get(pk=global_layer.pk).polygon)
        finally:
            MigrationExecutor(connection).migrate(
                [("maps", "0008_maplayer_min_zoom")]
            )


class MapLayerMinZoomMigrationTests(TransactionTestCase):
    def test_backfills_collection_layers_from_extent(self):
        before = [("maps", "0007_maplayer_polygon")]
        after = [("maps", "0008_maplayer_min_zoom")]
        executor = MigrationExecutor(connection)
        executor.migrate(before)
        try:
            old_apps = executor.loader.project_state(before).apps
            collection = old_apps.get_model("maps", "LayerCollection").objects.create(
                name="Migration collection", slug="zoom-migration-collection"
            )
            layer_model = old_apps.get_model("maps", "MapLayer")
            local = layer_model.objects.create(
                name="Local",
                slug="zoom-migration-local",
                url=PMTILES_URL,
                collection=collection,
                polygon=example_polygon(),
            )
            global_layer = layer_model.objects.create(
                name="Global",
                slug="zoom-migration-global",
                url=PMTILES_URL,
            )
            executor = MigrationExecutor(connection)
            executor.migrate(after)
            new_model = executor.loader.project_state(after).apps.get_model(
                "maps", "MapLayer"
            )
            self.assertEqual(new_model.objects.get(pk=local.pk).min_zoom, MIN_ZOOM)
            self.assertIsNone(new_model.objects.get(pk=global_layer.pk).min_zoom)
        finally:
            MigrationExecutor(connection).migrate(after)


class MapLayerCleanTests(TestCase):
    def setUp(self):
        # A data migration seeds a default primary layer; the single-default
        # rule is easier to exercise from a clean slate.
        MapLayer.objects.filter(collection__isnull=True).delete()
        self.collection = LayerCollection.objects.create(name="Sanborn", slug="sanborn")

    def test_second_default_primary_is_rejected(self):
        MapLayer.objects.create(
            name="Streets", slug="streets", type="xyz", url=XYZ_URL, is_default=True
        )
        other = MapLayer(
            name="Satellite", slug="satellite", type="xyz", url=XYZ_URL, is_default=True
        )
        with self.assertRaises(ValidationError) as ctx:
            other.full_clean()
        self.assertIn("is_default", ctx.exception.message_dict)
        self.assertIn("Streets", ctx.exception.message_dict["is_default"][0])

    def test_existing_default_can_be_resaved(self):
        layer = MapLayer.objects.create(
            name="Streets", slug="streets", type="xyz", url=XYZ_URL, is_default=True
        )
        layer.full_clean()

    def test_zero_defaults_is_legal(self):
        MapLayer(name="Streets", slug="streets", type="xyz", url=XYZ_URL).full_clean()

    def test_xyz_url_requires_placeholders(self):
        layer = MapLayer(
            name="Streets",
            slug="streets",
            type="xyz",
            url="https://example.com/{z}/{x}",
        )
        with self.assertRaises(ValidationError) as ctx:
            layer.full_clean()
        self.assertIn("url", ctx.exception.message_dict)

    def test_pmtiles_url_must_end_in_pmtiles(self):
        layer = MapLayer(
            name="Map",
            slug="map",
            type="pmtiles",
            url=f"{PMTILES_URL}/{{z}}/{{x}}/{{y}}",
            collection=self.collection,
            polygon=example_polygon(),
            min_zoom=MIN_ZOOM,
        )
        with self.assertRaises(ValidationError) as ctx:
            layer.full_clean()
        self.assertIn("url", ctx.exception.message_dict)

    def test_style_url_must_not_be_a_tile_template(self):
        layer = MapLayer(name="Base", slug="base", type="style", url=XYZ_URL)
        with self.assertRaises(ValidationError) as ctx:
            layer.full_clean()
        self.assertIn("url", ctx.exception.message_dict)

    def test_well_formed_urls_validate(self):
        MapLayer(name="A", slug="a", type="xyz", url=XYZ_URL).full_clean()
        MapLayer(name="B", slug="b", type="style", url=STYLE_URL).full_clean()
        MapLayer(
            name="C",
            slug="c",
            type="pmtiles",
            url=PMTILES_URL,
            collection=self.collection,
            polygon=example_polygon(),
            min_zoom=MIN_ZOOM,
        ).full_clean()

    def test_style_in_collection_is_rejected(self):
        layer = MapLayer(
            name="Base",
            slug="base",
            type="style",
            url=STYLE_URL,
            collection=self.collection,
            polygon=example_polygon(),
            min_zoom=MIN_ZOOM,
        )
        with self.assertRaises(ValidationError) as ctx:
            layer.full_clean()
        self.assertIn("type", ctx.exception.message_dict)

    def test_non_primary_default_is_rejected(self):
        layer = MapLayer(
            name="Map",
            slug="map",
            type="pmtiles",
            url=PMTILES_URL,
            collection=self.collection,
            polygon=example_polygon(),
            min_zoom=MIN_ZOOM,
            is_default=True,
        )
        with self.assertRaises(ValidationError) as ctx:
            layer.full_clean()
        self.assertIn("is_default", ctx.exception.message_dict)

    def test_collection_polygon_and_global_conversion(self):
        layer = MapLayer.objects.create(
            name="Map",
            slug="map",
            url=PMTILES_URL,
            collection=self.collection,
            polygon=example_polygon(),
            min_zoom=MIN_ZOOM,
        )
        layer.refresh_from_db()
        self.assertEqual(layer.polygon.extent, example_polygon().extent)
        self.assertEqual(layer.polygon.srid, 4326)
        self.assertEqual(layer.min_zoom, MIN_ZOOM)
        layer.collection = None
        layer.save(update_fields=["collection"])
        layer.refresh_from_db()
        self.assertIsNone(layer.polygon)
        self.assertIsNone(layer.min_zoom)
        layer.collection = self.collection
        with self.assertRaises(ValidationError) as ctx:
            layer.full_clean()
        self.assertIn("polygon", ctx.exception.message_dict)
        self.assertIn("min_zoom", ctx.exception.message_dict)
        layer.polygon = example_polygon()
        layer.min_zoom = MIN_ZOOM
        layer.save(update_fields=["collection"])
        layer.refresh_from_db()
        self.assertEqual(layer.polygon, example_polygon())

    def test_global_discards_polygon_on_validation_and_save(self):
        layer = MapLayer(
            name="Base",
            slug="base",
            url=PMTILES_URL,
            polygon=example_polygon(),
            min_zoom=MIN_ZOOM,
        )
        layer.full_clean()
        self.assertIsNone(layer.polygon)
        self.assertIsNone(layer.min_zoom)
        layer.polygon = example_polygon()
        layer.min_zoom = MIN_ZOOM
        layer.save()
        layer.refresh_from_db()
        self.assertIsNone(layer.polygon)
        self.assertIsNone(layer.min_zoom)

    def test_new_collection_layer_requires_user_polygon(self):
        layer = MapLayer(
            name="New", slug="new", url=PMTILES_URL, collection=self.collection
        )
        self.assertIsNone(layer.polygon)
        with self.assertRaises(ValidationError) as ctx:
            layer.full_clean()
        self.assertIn("polygon", ctx.exception.message_dict)
        self.assertIn("min_zoom", ctx.exception.message_dict)
        self.assertIsNone(layer.polygon)
        with self.assertRaises(IntegrityError), transaction.atomic():
            layer.save()

    def test_collection_layer_requires_valid_min_zoom(self):
        missing = MapLayer(
            name="Missing zoom",
            slug="missing-zoom",
            url=PMTILES_URL,
            collection=self.collection,
            polygon=example_polygon(),
        )
        with self.assertRaises(ValidationError) as ctx:
            missing.full_clean()
        self.assertEqual(set(ctx.exception.message_dict), {"min_zoom"})

        for min_zoom in (-1, 25):
            with self.subTest(min_zoom=min_zoom):
                invalid = MapLayer(
                    name="Invalid zoom",
                    slug=f"invalid-zoom-{min_zoom}",
                    url=PMTILES_URL,
                    collection=self.collection,
                    polygon=example_polygon(),
                    min_zoom=min_zoom,
                )
                with self.assertRaises(ValidationError) as ctx:
                    invalid.full_clean()
                self.assertIn("min_zoom", ctx.exception.message_dict)

    def test_database_enforces_extent_role(self):
        layer = MapLayer.objects.create(
            name="Map",
            slug="map",
            url=PMTILES_URL,
            collection=self.collection,
            polygon=example_polygon(),
            min_zoom=MIN_ZOOM,
        )
        for update in (
            {"polygon": None},
            {"min_zoom": None},
            {"min_zoom": 25},
            {"collection": None},
        ):
            with (
                self.subTest(update=update),
                self.assertRaises(IntegrityError),
                transaction.atomic(),
            ):
                MapLayer.objects.filter(pk=layer.pk).update(**update)

    def test_invalid_polygon_is_field_error(self):
        layer = MapLayer(
            name="Map",
            slug="map",
            url=PMTILES_URL,
            collection=self.collection,
            polygon=Polygon(((0, 0), (1, 1), (0, 1), (1, 0), (0, 0))),
            min_zoom=MIN_ZOOM,
        )
        with self.assertRaises(ValidationError) as ctx:
            layer.full_clean()
        self.assertIn("polygon", ctx.exception.message_dict)


class MapLayerFormTests(TestCase):
    def setUp(self):
        self.collection = LayerCollection.objects.create(name="Sanborn", slug="sanborn")
        self.other_collection = LayerCollection.objects.create(name="USGS", slug="usgs")
        MapLayer.objects.create(
            name="1886",
            slug="1886",
            type="pmtiles",
            url=PMTILES_URL,
            collection=self.collection,
            polygon=example_polygon(),
            min_zoom=MIN_ZOOM,
        )

    def _data(self, **overrides):
        data = {
            "name": "Sheet",
            "slug": "1886",
            "collection": self.collection.pk,
            "order": 0,
            "type": "pmtiles",
            "url": PMTILES_URL,
            "polygon": example_polygon().geojson,
            "min_zoom": MIN_ZOOM,
        }
        data.update(overrides)
        return data

    def test_duplicate_slug_in_collection_is_a_field_error(self):
        form = MapLayerForm(self._data())
        self.assertFalse(form.is_valid())
        self.assertIn("slug", form.errors)

    def test_same_slug_in_other_collection_is_fine(self):
        form = MapLayerForm(self._data(collection=self.other_collection.pk))
        self.assertTrue(form.is_valid(), form.errors)

    def test_duplicate_primary_slug_is_a_field_error(self):
        MapLayer.objects.create(name="Base", slug="base", type="xyz", url=XYZ_URL)
        form = MapLayerForm(
            self._data(slug="base", collection="", type="xyz", url=XYZ_URL)
        )
        self.assertFalse(form.is_valid())
        self.assertIn("slug", form.errors)

    def test_editing_keeps_own_slug(self):
        layer = MapLayer.objects.get(slug="1886")
        form = MapLayerForm(self._data(name="Renamed"), instance=layer)
        self.assertTrue(form.is_valid(), form.errors)

    def test_custom_polygon_round_trip(self):
        polygon = Polygon.from_bbox((-77.48, 37.50, -77.40, 37.58))
        form = MapLayerForm(self._data(slug="custom", polygon=polygon.geojson))
        self.assertTrue(form.is_valid(), form.errors)
        layer = form.save()
        layer.refresh_from_db()
        self.assertEqual(layer.polygon.coords, polygon.coords)
        self.assertEqual(layer.min_zoom, MIN_ZOOM)

    def test_new_collection_editor_has_no_default_polygon(self):
        form = MapLayerForm(instance=MapLayer(collection=self.collection))
        self.assertEqual(form.polygon_editor_data(), {"polygon": None})
        self.assertEqual(form["polygon"].value(), "")
        self.assertIsNone(form["min_zoom"].value())

    def test_collection_rejects_missing_or_invalid_min_zoom(self):
        for min_zoom in ("", "1.5", "-1", "25"):
            with self.subTest(min_zoom=min_zoom):
                form = MapLayerForm(
                    self._data(slug=f"zoom-{min_zoom}", min_zoom=min_zoom)
                )
                self.assertFalse(form.is_valid())
                self.assertIn("min_zoom", form.errors)

    def test_collection_rejects_missing_or_invalid_polygon(self):
        for polygon in (
            "",
            "not JSON",
            '{"type":"Point","coordinates":[0,0]}',
            '{"type":"Polygon","coordinates":[]}',
            '{"type":"Polygon","coordinates":[null]}',
            Polygon.from_bbox((-181, 37, -77, 38)).geojson,
            Polygon(((0, 0), (1, 1), (0, 1), (1, 0), (0, 0))).geojson,
        ):
            with self.subTest(polygon=polygon):
                form = MapLayerForm(self._data(slug="new", polygon=polygon))
                self.assertFalse(form.is_valid())
                self.assertIn("polygon", form.errors)

    def test_global_ignores_submitted_polygon(self):
        form = MapLayerForm(
            self._data(
                slug="global",
                collection="",
                polygon="invalid",
                min_zoom="invalid",
            )
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNone(form.save().polygon)
        self.assertIsNone(form.instance.min_zoom)

    def test_bound_polygon_survives_other_errors(self):
        polygon = Polygon.from_bbox((-77.48, 37.50, -77.40, 37.58))
        form = MapLayerForm(self._data(name="", polygon=polygon.geojson))
        self.assertFalse(form.is_valid())
        self.assertEqual(
            form.polygon_editor_data()["polygon"], json.loads(polygon.geojson)
        )
        self.assertEqual(form["min_zoom"].value(), MIN_ZOOM)
        deleted = MapLayerForm(self._data(name="", polygon=""))
        self.assertFalse(deleted.is_valid())
        self.assertIsNone(deleted.polygon_editor_data()["polygon"])

    def test_edit_form_contains_saved_geometry(self):
        layer = MapLayer.objects.get(slug="1886")
        form = MapLayerForm(instance=layer)
        self.assertEqual(
            form.polygon_editor_data()["polygon"], json.loads(layer.polygon.geojson)
        )
        self.assertEqual(form["min_zoom"].value(), MIN_ZOOM)


class LayerManageViewTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("staff", password="pw", is_staff=True)
        self.member = User.objects.create_user("member", password="pw")
        self.collection = LayerCollection.objects.create(name="Sanborn", slug="sanborn")
        self.layer = MapLayer.objects.create(
            name="1886",
            slug="1886",
            type="pmtiles",
            url=PMTILES_URL,
            collection=self.collection,
            polygon=example_polygon(),
            min_zoom=MIN_ZOOM,
        )

    def _urls(self):
        return {
            "get": [
                reverse("maps:layer_manage"),
                reverse("maps:layer_create"),
                reverse("maps:layer_edit", args=[self.layer.pk]),
            ],
            "post": [reverse("maps:layer_delete", args=[self.layer.pk])],
        }

    def assert_redirected_away(self):
        urls = self._urls()
        for url in urls["get"]:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertIn("/admin/login/", response["Location"])
        for url in urls["post"]:
            response = self.client.post(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertIn("/admin/login/", response["Location"])
        self.assertTrue(MapLayer.objects.filter(pk=self.layer.pk).exists())

    def test_anonymous_is_redirected(self):
        self.assert_redirected_away()

    def test_non_staff_is_redirected(self):
        self.client.login(username="member", password="pw")
        self.assert_redirected_away()

    def test_staff_can_view_pages(self):
        self.client.login(username="staff", password="pw")
        for url in self._urls()["get"]:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
        self.assertContains(
            self.client.get(reverse("maps:layer_manage")), self.layer.name
        )

    def test_delete_requires_post(self):
        self.client.login(username="staff", password="pw")
        response = self.client.get(reverse("maps:layer_delete", args=[self.layer.pk]))
        self.assertEqual(response.status_code, 405)

    def test_create_and_delete_round_trip(self):
        self.client.login(username="staff", password="pw")
        response = self.client.post(
            reverse("maps:layer_create"),
            {
                "name": "Streets",
                "slug": "streets",
                "collection": "",
                "order": 1,
                "type": "xyz",
                "url": XYZ_URL,
            },
        )
        layer = MapLayer.objects.get(slug="streets")
        self.assertRedirects(response, reverse("maps:layer_edit", args=[layer.pk]))
        self.assertTrue(layer.is_primary)

        response = self.client.post(reverse("maps:layer_delete", args=[layer.pk]))
        self.assertRedirects(response, reverse("maps:layer_manage"))
        self.assertFalse(MapLayer.objects.filter(pk=layer.pk).exists())

    def test_invalid_post_shows_field_error(self):
        self.client.login(username="staff", password="pw")
        response = self.client.post(
            reverse("maps:layer_edit", args=[self.layer.pk]),
            {
                "name": "1886",
                "slug": "1886",
                "collection": self.collection.pk,
                "order": 0,
                "type": "xyz",
                "url": "https://example.com/tiles.png",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("url", response.context["form"].errors)

    def test_detail_page_links_to_edit_for_staff(self):
        self.client.login(username="staff", password="pw")
        response = self.client.get(self.layer.get_absolute_url())
        self.assertContains(response, reverse("maps:layer_edit", args=[self.layer.pk]))
        self.client.logout()
        response = self.client.get(self.layer.get_absolute_url())
        self.assertNotContains(
            response, reverse("maps:layer_edit", args=[self.layer.pk])
        )

    def test_extent_edit_and_validation_round_trip(self):
        self.client.force_login(self.staff)
        url = reverse("maps:layer_edit", args=[self.layer.pk])
        polygon = Polygon.from_bbox((-77.48, 37.50, -77.40, 37.58))
        data = {
            "name": self.layer.name,
            "slug": self.layer.slug,
            "collection": self.collection.pk,
            "order": 0,
            "type": "pmtiles",
            "url": PMTILES_URL,
            "polygon": polygon.geojson,
            "min_zoom": 12,
        }
        self.assertRedirects(self.client.post(url, data), url)
        self.layer.refresh_from_db()
        self.assertEqual(self.layer.polygon.coords, polygon.coords)
        self.assertEqual(self.layer.min_zoom, 12)
        response = self.client.post(url, {**data, "name": ""})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["form"].polygon_editor_data()["polygon"],
            json.loads(polygon.geojson),
        )
        response = self.client.post(url, {**data, "polygon": ""})
        self.assertIn("polygon", response.context["form"].errors)
        self.layer.refresh_from_db()
        self.assertEqual(self.layer.polygon.coords, polygon.coords)
        self.assertRedirects(self.client.post(url, {**data, "collection": ""}), url)
        self.layer.refresh_from_db()
        self.assertIsNone(self.layer.polygon)
