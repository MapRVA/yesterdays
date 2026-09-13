from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from .forms import MapLayerForm
from .models import LayerCollection, MapLayer

PMTILES_URL = "https://example.com/tiles/richmond-1876.pmtiles"
XYZ_URL = "https://example.com/tiles/{z}/{x}/{y}.png"
STYLE_URL = "https://example.com/styles/basemap.json"


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
            name="Streets", slug="streets", type="xyz", url="https://example.com/{z}/{x}"
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
            name="C", slug="c", type="pmtiles", url=PMTILES_URL, collection=self.collection
        ).full_clean()

    def test_style_in_collection_is_rejected(self):
        layer = MapLayer(
            name="Base", slug="base", type="style", url=STYLE_URL, collection=self.collection
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
            is_default=True,
        )
        with self.assertRaises(ValidationError) as ctx:
            layer.full_clean()
        self.assertIn("is_default", ctx.exception.message_dict)


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
        )

    def _data(self, **overrides):
        data = {
            "name": "Sheet",
            "slug": "1886",
            "collection": self.collection.pk,
            "order": 0,
            "type": "pmtiles",
            "url": PMTILES_URL,
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
        form = MapLayerForm(self._data(slug="base", collection="", type="xyz", url=XYZ_URL))
        self.assertFalse(form.is_valid())
        self.assertIn("slug", form.errors)

    def test_editing_keeps_own_slug(self):
        layer = MapLayer.objects.get(slug="1886")
        form = MapLayerForm(self._data(name="Renamed"), instance=layer)
        self.assertTrue(form.is_valid(), form.errors)


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
