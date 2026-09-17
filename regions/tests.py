import json
import re
from unittest import mock

import pyoxigraph
import requests
from django.contrib.auth.models import User
from django.contrib.gis.geos import Point, Polygon
from django.core.cache import cache
from django.db import IntegrityError
from django.db.models import ProtectedError
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from images.models import (
    Collection,
    CollectionRegionStats,
    Georeference,
    Image,
    ImageOfTheDay,
    SiteSettings,
    Source,
    SubjectMapping,
)
from subjects.models import Subject, WikidataItem
from subjects.sparql_safety import UnsafeSparqlInput
from subjects.tasks import _do_refresh_wikidata_item, get_next_stale_wikidata_item
from yesterdays.views import HOME_SUBJECT_LABEL_LIMIT

from .admin import RegionAdminForm
from .context_processors import REGION_COOKIE_NAME, current_region
from .forms import RegionForm
from .models import Region, RegionAncestor
from .region_ancestors import update_region_ancestors
from .views import _AUTOCOMPLETE_LIMIT
from .wikidata_check import (
    ALLOWED_ROOT_CLASSES,
    RegionCheck,
    check_region,
    region_class_query,
)

# Richmond's P625, 37°32'27"N 77°26'12"W. Regions require a coordinate,
# which in production comes from WDQS during admin validation; fixtures
# below don't go through the admin, so they share this one.
TEST_POINT = Point(-77.4366667, 37.5408333, srid=4326)
ELIGIBLE = RegionCheck(eligible=True, coordinate=TEST_POINT)


def make_region(**kwargs):
    """Create a Region, defaulting the required Wikidata coordinate."""
    kwargs.setdefault("wikidata_coordinate_location", TEST_POINT)
    kwargs.setdefault("advertise", True)
    return Region.objects.create(**kwargs)


class EffectiveRegionTests(SimpleTestCase):
    """The image -> collection -> source resolution chain.

    Pure in-memory: the properties only read FK attributes, so unsaved
    instances exercise them without a database.
    """

    def setUp(self):
        self.r_source = Region(short_name="Source Region", slug="source-region")
        self.r_coll = Region(short_name="Collection Region", slug="collection-region")
        self.r_image = Region(short_name="Image Region", slug="image-region")
        self.source = Source(name="Src", slug="src")
        self.collection = Collection(name="Coll", slug="coll", source=self.source)
        self.image = Image(title="Img", collection=self.collection)

    def test_image_region_wins(self):
        self.source.region = self.r_source
        self.collection.region = self.r_coll
        self.image.region = self.r_image
        self.assertIs(self.image.effective_region, self.r_image)

    def test_collection_region_when_image_none(self):
        self.source.region = self.r_source
        self.collection.region = self.r_coll
        self.assertIs(self.image.effective_region, self.r_coll)

    def test_source_region_when_collection_and_image_none(self):
        self.source.region = self.r_source
        self.assertIs(self.image.effective_region, self.r_source)

    def test_none_when_no_region_anywhere(self):
        self.assertIsNone(self.image.effective_region)

    def test_collection_effective_region_falls_back_to_source(self):
        self.source.region = self.r_source
        self.assertIs(self.collection.effective_region, self.r_source)
        self.collection.region = self.r_coll
        self.assertIs(self.collection.effective_region, self.r_coll)


class RegionProtectTests(TestCase):
    """Deleting a referenced Region must be refused, not cascaded."""

    def test_delete_refused_while_referenced(self):
        item = WikidataItem.objects.bulk_create(
            [WikidataItem(wikidata_id="Q950", title="Test Region")]
        )[0]
        region = make_region(
            short_name="Test Region", slug="test", wikidata_item=item
        )
        Source.objects.create(
            name="Src", slug="src", url="https://example.com", description="",
            region=region,
        )
        with self.assertRaises(ProtectedError):
            region.delete()


class RegionManageViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("staff", password="pw", is_staff=True)
        cls.member = User.objects.create_user("member", password="pw")
        cls.item = WikidataItem.objects.bulk_create(
            [WikidataItem(wikidata_id="Q1370", title="Virginia")]
        )[0]
        cls.region = make_region(
            short_name="Virginia",
            long_name="Virginia, United States",
            slug="virginia",
            wikidata_item=cls.item,
        )

    def _urls(self):
        return (
            reverse("regions:region_manage"),
            reverse("regions:region_create"),
            reverse("regions:region_edit", args=[self.region.pk]),
        )

    def _edit_data(self, **overrides):
        data = {
            "wikidata_id": self.item.wikidata_id,
            "short_name": self.region.short_name,
            "long_name": self.region.long_name,
            "slug": self.region.slug,
            "advertise": "on",
            "representative_image": "",
        }
        data.update(overrides)
        return data

    def test_management_pages_require_staff(self):
        for url in self._urls():
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/admin/login/", response["Location"])

        self.client.force_login(self.member)
        for url in self._urls():
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/admin/login/", response["Location"])

    def test_staff_can_open_manage_create_and_edit_pages(self):
        self.client.force_login(self.staff)
        for url in self._urls():
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.get(reverse("regions:region_edit", args=[self.region.pk]))
        self.assertContains(response, "region-map-bounds-map")
        self.assertContains(response, "region-geocoder-bounds-map")
        self.assertContains(response, "Use the rectangle tool")
        self.assertContains(response, self.region.long_name)

    def test_spatial_metadata_edit_round_trip(self):
        self.client.force_login(self.staff)
        url = reverse("regions:region_edit", args=[self.region.pk])
        response = self.client.post(
            url,
            self._edit_data(
                center_latitude="37.53",
                center_longitude="-77.44",
                map_west="-79",
                map_south="36",
                map_east="-75",
                map_north="40",
                geocoder_west="-78.8",
                geocoder_south="36.2",
                geocoder_east="-75.2",
                geocoder_north="39.8",
            ),
        )
        self.assertRedirects(response, url)
        self.region.refresh_from_db()
        self.assertEqual(self.region.map_bbox, [-79.0, 36.0, -75.0, 40.0])
        self.assertEqual(self.region.search_bbox, [-78.8, 36.2, -75.2, 39.8])
        self.assertAlmostEqual(self.region.coordinate_location.x, -77.44)
        self.assertAlmostEqual(self.region.coordinate_location.y, 37.53)

    def test_invalid_rectangle_returns_field_error_without_saving(self):
        self.client.force_login(self.staff)
        url = reverse("regions:region_edit", args=[self.region.pk])
        response = self.client.post(
            url,
            self._edit_data(
                map_west="-75",
                map_south="40",
                map_east="-79",
                map_north="36",
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("map_east", response.context["form"].errors)
        self.assertIn("map_north", response.context["form"].errors)
        self.region.refresh_from_db()
        self.assertIsNone(self.region.map_bounds)

    def test_standalone_form_hides_raw_spatial_fields(self):
        form = RegionForm(instance=self.region)
        for field in form.spatial_fields():
            self.assertEqual(field.field.widget.input_type, "hidden")


@mock.patch("regions.admin.check_region")
class RegionAdminFormTests(TestCase):
    """Q-ID validation and WikidataItem wiring in the admin form."""

    @classmethod
    def setUpTestData(cls):
        # bulk_create bypasses WikidataItem.save(), which fetches live
        # Wikidata metadata on insert.
        cls.existing_item = WikidataItem.objects.bulk_create(
            [WikidataItem(wikidata_id="Q1370", title="Virginia")]
        )[0]

    def _form(self, instance=None, **data):
        payload = {
            "short_name": "Somewhere",
            "long_name": "Somewhere, Testland",
            "slug": "somewhere",
            "wikidata_id": "Q43421",
        }
        payload.update(data)
        return RegionAdminForm(payload, instance=instance)

    def test_rejects_bad_qid_grammar(self, check):
        for bad in ("", "P31", "Q042", "Q42 x", "43421"):
            form = self._form(wikidata_id=bad)
            self.assertFalse(form.is_valid(), bad)
            self.assertIn("wikidata_id", form.errors)
        check.assert_not_called()

    def test_normalizes_lowercase_qid(self, check):
        check.return_value = ELIGIBLE
        form = self._form(wikidata_id="q43421")
        self.assertTrue(form.is_valid(), form.errors)
        check.assert_called_once_with("Q43421")
        with mock.patch.object(
            WikidataItem, "populate_from_wikidata", return_value=True
        ):
            region = form.save()
        self.assertEqual(region.wikidata_item.wikidata_id, "Q43421")

    def test_rejects_qid_used_by_another_region(self, check):
        make_region(
            short_name="Virginia", slug="virginia", wikidata_item=self.existing_item
        )
        form = self._form(wikidata_id="Q1370")
        self.assertFalse(form.is_valid())
        self.assertIn("Virginia", str(form.errors["wikidata_id"]))
        check.assert_not_called()

    def test_failed_check_blocks_with_root_classes_in_message(self, check):
        check.return_value = RegionCheck(eligible=False, coordinate=None)
        form = self._form(wikidata_id="Q1370")
        self.assertFalse(form.is_valid())
        message = str(form.errors["wikidata_id"])
        self.assertIn("Virginia", message)  # the cached item's label
        for root in ALLOWED_ROOT_CLASSES:
            self.assertIn(root, message)

    def test_network_failure_blocks_with_retry_message(self, check):
        check.side_effect = requests.ConnectionError("boom")
        form = self._form()
        self.assertFalse(form.is_valid())
        self.assertIn("try again", str(form.errors["wikidata_id"]))

    def test_item_without_coordinate_needs_a_centerpoint(self, check):
        check.return_value = RegionCheck(eligible=True, coordinate=None)
        form = self._form(wikidata_id="Q1370")
        self.assertFalse(form.is_valid())
        # Reported on the field that fixes it, not on the Q-ID, which is
        # fine — the item qualifies, it just can't say where it is.
        self.assertNotIn("wikidata_id", form.errors)
        self.assertIn("P625", str(form.errors["center_latitude"]))

    def test_item_without_coordinate_accepted_with_a_centerpoint(self, check):
        check.return_value = RegionCheck(eligible=True, coordinate=None)
        form = self._form(
            wikidata_id="Q1370", center_latitude="37.53", center_longitude="-77.44"
        )
        self.assertTrue(form.is_valid(), form.errors)
        region = form.save()
        region.refresh_from_db()
        self.assertIsNone(region.wikidata_coordinate_location)
        self.assertAlmostEqual(region.coordinate_location.x, -77.44)

    def test_repointing_to_a_coordinateless_item_drops_the_old_coordinate(self, check):
        # The stored P625 described the previous item. Keeping it would
        # quietly center the region on somewhere it no longer is.
        region = make_region(
            short_name="Virginia", slug="virginia", wikidata_item=self.existing_item
        )
        check.return_value = RegionCheck(eligible=True, coordinate=None)
        form = self._form(
            instance=region,
            wikidata_id="Q43421",
            slug="virginia",
            center_latitude="37.53",
            center_longitude="-77.44",
        )
        self.assertTrue(form.is_valid(), form.errors)
        with mock.patch.object(
            WikidataItem, "populate_from_wikidata", return_value=True
        ):
            saved = form.save()
        saved.refresh_from_db()
        self.assertIsNone(saved.wikidata_coordinate_location)
        self.assertAlmostEqual(saved.coordinate_location.x, -77.44)

    def test_cannot_clear_the_only_centerpoint(self, check):
        region = make_region(
            short_name="Virginia",
            slug="virginia",
            wikidata_item=self.existing_item,
            wikidata_coordinate_location=None,
            custom_coordinate_location=Point(-77.44, 37.53, srid=4326),
        )
        form = self._form(instance=region, wikidata_id="Q1370", slug="virginia")
        self.assertFalse(form.is_valid())
        self.assertIn("P625", str(form.errors["center_latitude"]))
        check.assert_not_called()

    def test_no_coordinate_complaint_while_the_qid_is_broken(self, check):
        # One error at a time: fixing the Q-ID may well supply a P625.
        form = self._form(wikidata_id="not-a-qid")
        self.assertFalse(form.is_valid())
        self.assertIn("wikidata_id", form.errors)
        self.assertNotIn("center_latitude", form.errors)

    def test_save_stores_the_fetched_coordinate(self, check):
        check.return_value = ELIGIBLE
        form = self._form(wikidata_id="Q1370")
        self.assertTrue(form.is_valid(), form.errors)
        region = form.save()
        region.refresh_from_db()
        self.assertAlmostEqual(region.wikidata_coordinate_location.x, TEST_POINT.x)
        self.assertAlmostEqual(region.wikidata_coordinate_location.y, TEST_POINT.y)
        self.assertIsNone(region.custom_coordinate_location)

    def test_edit_with_unchanged_qid_keeps_existing_coordinate(self, check):
        # No round trip means no fetched coordinate; the stored one must
        # survive the save rather than being overwritten with NULL.
        region = make_region(
            short_name="Virginia", slug="virginia", wikidata_item=self.existing_item
        )
        form = self._form(
            instance=region, wikidata_id="Q1370", slug="virginia"
        )
        self.assertTrue(form.is_valid(), form.errors)
        check.assert_not_called()
        saved = form.save()
        saved.refresh_from_db()
        self.assertAlmostEqual(saved.wikidata_coordinate_location.x, TEST_POINT.x)

    def test_reuses_existing_wikidata_item(self, check):
        check.return_value = ELIGIBLE
        form = self._form(wikidata_id="Q1370")
        self.assertTrue(form.is_valid(), form.errors)
        with mock.patch.object(WikidataItem, "populate_from_wikidata") as populate:
            region = form.save()
        populate.assert_not_called()
        self.assertEqual(region.wikidata_item, self.existing_item)
        self.assertEqual(WikidataItem.objects.count(), 1)

    def test_creates_missing_wikidata_item(self, check):
        check.return_value = ELIGIBLE
        form = self._form(wikidata_id="Q43421")
        self.assertTrue(form.is_valid(), form.errors)
        with mock.patch.object(
            WikidataItem, "populate_from_wikidata", return_value=True
        ):
            region = form.save()
        self.assertTrue(WikidataItem.objects.filter(wikidata_id="Q43421").exists())
        self.assertEqual(region.wikidata_item.wikidata_id, "Q43421")

    def test_names_default_to_item_label(self, check):
        check.return_value = ELIGIBLE
        form = self._form(
            wikidata_id="Q1370", short_name="", long_name="", slug="virginia"
        )
        self.assertTrue(form.is_valid(), form.errors)
        region = form.save()
        self.assertEqual(region.short_name, "Virginia")
        self.assertEqual(region.long_name, "Virginia")

    def test_missing_slug_rejected(self, check):
        check.return_value = ELIGIBLE
        form = self._form(slug="")
        self.assertFalse(form.is_valid())
        self.assertIn("slug", form.errors)

    def test_duplicate_slug_rejected(self, check):
        check.return_value = ELIGIBLE
        make_region(
            short_name="Virginia", slug="somewhere", wikidata_item=self.existing_item
        )
        form = self._form()
        self.assertFalse(form.is_valid())
        self.assertIn("slug", form.errors)

    def test_edit_with_unchanged_qid_skips_check(self, check):
        region = make_region(
            short_name="Virginia", slug="virginia", wikidata_item=self.existing_item
        )
        form = self._form(
            instance=region,
            wikidata_id="Q1370",
            short_name="Old Virginia",
            slug="virginia",
        )
        self.assertTrue(form.is_valid(), form.errors)
        check.assert_not_called()
        region = form.save()
        self.assertEqual(region.short_name, "Old Virginia")

    def test_center_pair_stored_as_the_override(self, check):
        check.return_value = ELIGIBLE
        form = self._form(center_latitude="37.53", center_longitude="-77.44")
        self.assertTrue(form.is_valid(), form.errors)
        with mock.patch.object(
            WikidataItem, "populate_from_wikidata", return_value=True
        ):
            region = form.save()
        region.refresh_from_db()
        self.assertAlmostEqual(region.custom_coordinate_location.x, -77.44)
        self.assertAlmostEqual(region.custom_coordinate_location.y, 37.53)
        # Wikidata's is still recorded underneath, unharmed.
        self.assertAlmostEqual(region.wikidata_coordinate_location.x, TEST_POINT.x)
        self.assertAlmostEqual(region.coordinate_location.x, -77.44)

    def test_blank_center_leaves_no_override(self, check):
        check.return_value = ELIGIBLE
        form = self._form()
        self.assertTrue(form.is_valid(), form.errors)
        with mock.patch.object(
            WikidataItem, "populate_from_wikidata", return_value=True
        ):
            region = form.save()
        self.assertIsNone(region.custom_coordinate_location)
        self.assertAlmostEqual(region.coordinate_location.x, TEST_POINT.x)

    def test_half_a_center_is_rejected(self, check):
        check.return_value = ELIGIBLE
        cases = {
            "center_latitude": {"center_longitude": "-77.44"},
            "center_longitude": {"center_latitude": "37.53"},
        }
        for missing, payload in cases.items():
            with self.subTest(missing=missing):
                form = self._form(**payload)
                self.assertFalse(form.is_valid())
                self.assertIn(missing, form.errors)

    def test_out_of_range_center_is_rejected(self, check):
        check.return_value = ELIGIBLE
        form = self._form(center_latitude="91", center_longitude="-181")
        self.assertFalse(form.is_valid())
        self.assertIn("center_latitude", form.errors)
        self.assertIn("center_longitude", form.errors)

    def test_editing_prefills_and_can_clear_the_override(self, check):
        region = make_region(
            short_name="Virginia",
            slug="virginia",
            wikidata_item=self.existing_item,
            custom_coordinate_location=Point(-77.44, 37.53, srid=4326),
        )
        prefilled = RegionAdminForm(instance=region)
        self.assertAlmostEqual(prefilled.fields["center_latitude"].initial, 37.53)
        self.assertAlmostEqual(prefilled.fields["center_longitude"].initial, -77.44)

        # Submitting the pair empty is how an admin reverts to Wikidata.
        form = self._form(instance=region, wikidata_id="Q1370", slug="virginia")
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        saved.refresh_from_db()
        self.assertIsNone(saved.custom_coordinate_location)
        self.assertAlmostEqual(saved.coordinate_location.x, TEST_POINT.x)

    def test_map_and_geocoder_bounds_are_stored_as_rectangles(self, check):
        check.return_value = ELIGIBLE
        form = self._form(
            wikidata_id="Q1370",
            map_west="-79",
            map_south="36",
            map_east="-75",
            map_north="40",
            geocoder_west="-78.8",
            geocoder_south="36.2",
            geocoder_east="-75.2",
            geocoder_north="39.8",
        )
        self.assertTrue(form.is_valid(), form.errors)
        region = form.save()
        region.refresh_from_db()
        self.assertEqual(region.map_bbox, [-79.0, 36.0, -75.0, 40.0])
        self.assertEqual(region.search_bbox, [-78.8, 36.2, -75.2, 39.8])

    def test_each_bounds_group_is_all_or_nothing(self, check):
        check.return_value = ELIGIBLE
        form = self._form(
            map_west="-79",
            map_south="36",
            map_east="-75",
            geocoder_west="-78.8",
        )
        self.assertFalse(form.is_valid())
        self.assertIn("map_north", form.errors)
        self.assertIn("geocoder_south", form.errors)
        self.assertIn("geocoder_east", form.errors)
        self.assertIn("geocoder_north", form.errors)

    def test_bounds_edges_must_be_ordered(self, check):
        check.return_value = ELIGIBLE
        form = self._form(
            map_west="-75",
            map_south="40",
            map_east="-79",
            map_north="36",
        )
        self.assertFalse(form.is_valid())
        self.assertIn("map_east", form.errors)
        self.assertIn("map_north", form.errors)

    def test_editing_prefills_and_can_clear_bounds(self, check):
        map_bounds = Polygon.from_bbox((-79, 36, -75, 40))
        map_bounds.srid = 4326
        geocoder_bounds = Polygon.from_bbox((-78.8, 36.2, -75.2, 39.8))
        geocoder_bounds.srid = 4326
        region = make_region(
            short_name="Virginia",
            slug="virginia",
            wikidata_item=self.existing_item,
            map_bounds=map_bounds,
            geocoder_bounds=geocoder_bounds,
        )
        prefilled = RegionAdminForm(instance=region)
        self.assertEqual(prefilled.fields["map_west"].initial, -79.0)
        self.assertEqual(prefilled.fields["map_north"].initial, 40.0)
        self.assertEqual(prefilled.fields["geocoder_west"].initial, -78.8)
        self.assertEqual(prefilled.fields["geocoder_north"].initial, 39.8)

        form = self._form(instance=region, wikidata_id="Q1370", slug="virginia")
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        saved.refresh_from_db()
        self.assertIsNone(saved.map_bounds)
        self.assertIsNone(saved.geocoder_bounds)
        check.assert_not_called()


class RegionCoordinateConstraintTests(TestCase):
    """region_has_a_coordinate: either source will do, but not neither."""

    @classmethod
    def setUpTestData(cls):
        cls.item = WikidataItem.objects.bulk_create(
            [WikidataItem(wikidata_id="Q1370", title="Virginia")]
        )[0]

    def test_custom_alone_is_enough(self):
        region = make_region(
            short_name="Virginia",
            slug="virginia",
            wikidata_item=self.item,
            wikidata_coordinate_location=None,
            custom_coordinate_location=Point(-77.44, 37.53, srid=4326),
        )
        self.assertAlmostEqual(region.coordinate_location.x, -77.44)

    def test_neither_is_refused_by_the_database(self):
        with self.assertRaises(IntegrityError):
            make_region(
                short_name="Virginia",
                slug="virginia",
                wikidata_item=self.item,
                wikidata_coordinate_location=None,
            )


class RegionCoordinateResolutionTests(SimpleTestCase):
    """Region.coordinate_location picks the override over Wikidata's."""

    def test_wikidata_coordinate_used_without_an_override(self):
        region = Region(wikidata_coordinate_location=TEST_POINT)
        self.assertIs(region.coordinate_location, TEST_POINT)

    def test_override_wins(self):
        custom = Point(-1.5, 52.5, srid=4326)
        region = Region(
            wikidata_coordinate_location=TEST_POINT,
            custom_coordinate_location=custom,
        )
        self.assertIs(region.coordinate_location, custom)

    def test_null_island_override_is_honoured(self):
        # Point(0, 0) is falsy-adjacent enough to be worth pinning down:
        # an `or` chain here would silently fall back to Wikidata.
        custom = Point(0, 0, srid=4326)
        region = Region(
            wikidata_coordinate_location=TEST_POINT,
            custom_coordinate_location=custom,
        )
        self.assertIs(region.coordinate_location, custom)


class RegionBoundsResolutionTests(SimpleTestCase):
    def setUp(self):
        self.map_bounds = Polygon.from_bbox((-79, 36, -75, 40))
        self.map_bounds.srid = 4326
        self.geocoder_bounds = Polygon.from_bbox((-78.8, 36.2, -75.2, 39.8))
        self.geocoder_bounds.srid = 4326

    def test_map_bbox_is_serialized_west_south_east_north(self):
        region = Region(map_bounds=self.map_bounds)
        self.assertEqual(region.map_bbox, [-79.0, 36.0, -75.0, 40.0])

    def test_search_bbox_uses_map_bounds_by_default(self):
        region = Region(map_bounds=self.map_bounds)
        self.assertEqual(region.search_bbox, [-79.0, 36.0, -75.0, 40.0])

    def test_geocoder_bounds_override_map_bounds(self):
        region = Region(
            map_bounds=self.map_bounds,
            geocoder_bounds=self.geocoder_bounds,
        )
        self.assertEqual(region.search_bbox, [-78.8, 36.2, -75.2, 39.8])

    def test_missing_bounds_return_none(self):
        region = Region()
        self.assertIsNone(region.map_bbox)
        self.assertIsNone(region.search_bbox)

class RegionSaveHydrationTests(TestCase):
    """Region.save() re-hydrates already-mirrored items (for P131)."""

    @classmethod
    def setUpTestData(cls):
        cls.hydrated, cls.fresh = WikidataItem.objects.bulk_create(
            [
                WikidataItem(
                    wikidata_id="Q9001",
                    title="Hydrated",
                    sparql_last_loaded_at=timezone.now(),
                ),
                WikidataItem(wikidata_id="Q9002", title="Fresh"),
            ]
        )

    def test_attach_to_hydrated_item_enqueues_rehydration(self):
        with mock.patch("subjects.tasks.hydrate_wikidata_item.delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                make_region(
                    short_name="R", slug="r1", wikidata_item=self.hydrated
                )
        delay.assert_called_once_with("Q9001")

    def test_attach_to_unhydrated_item_does_not_enqueue(self):
        # The item's own save() already queued hydration, which runs
        # post-commit and therefore sees the Region row.
        with mock.patch("subjects.tasks.hydrate_wikidata_item.delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                make_region(short_name="R", slug="r2", wikidata_item=self.fresh)
        delay.assert_not_called()

    def test_qid_change_on_edit_enqueues_rehydration(self):
        region = make_region(
            short_name="R", slug="r3", wikidata_item=self.fresh
        )
        with mock.patch("subjects.tasks.hydrate_wikidata_item.delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                region.wikidata_item = self.hydrated
                region.save()
        delay.assert_called_once_with("Q9001")

    def test_name_only_edit_does_not_enqueue(self):
        # Created outside captureOnCommitCallbacks, so the creation's own
        # on_commit callback is never executed.
        region = make_region(
            short_name="R", slug="r4", wikidata_item=self.hydrated
        )
        with mock.patch("subjects.tasks.hydrate_wikidata_item.delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                region.short_name = "Renamed"
                region.save()
        delay.assert_not_called()


class _RecordingClient:
    """Stands in for MemgraphClient: canned rows, recorded queries."""

    def __init__(self, rows):
        self._rows = rows
        self.queries = []

    def read(self, query, **params):
        self.queries.append((query, params))
        return self._rows


class RegionAncestorUpdateTests(TestCase):
    """The Memgraph -> Postgres containment projection."""

    @classmethod
    def setUpTestData(cls):
        cls.seed, cls.county, cls.state = WikidataItem.objects.bulk_create(
            [
                WikidataItem(wikidata_id="Q800", title="Test Town"),
                WikidataItem(wikidata_id="Q810", title="Test County"),
                WikidataItem(wikidata_id="Q820", title="Test State"),
            ]
        )
        cls.region = make_region(
            short_name="Test Town", slug="test-town", wikidata_item=cls.seed
        )

    def _ancestor_qids(self):
        return set(
            self.region.ancestors.values_list("ancestor__wikidata_id", flat=True)
        )

    def test_update_replaces_rows(self):
        RegionAncestor.objects.create(region=self.region, ancestor=self.state)
        count = update_region_ancestors(
            self.region, _RecordingClient([{"ancestor": "Q810"}])
        )
        self.assertEqual(count, 1)
        self.assertEqual(self._ancestor_qids(), {"Q810"})

    def test_ancestors_without_wikidataitem_rows_dropped(self):
        count = update_region_ancestors(
            self.region,
            _RecordingClient([{"ancestor": "Q810"}, {"ancestor": "Q999"}]),
        )
        self.assertEqual(count, 1)
        self.assertEqual(self._ancestor_qids(), {"Q810"})

    def test_query_is_containment_only(self):
        client = _RecordingClient([])
        update_region_ancestors(self.region, client)
        query, params = client.queries[0]
        self.assertEqual(params, {"qid": "Q800"})
        self.assertIn("P131", query)
        for excluded in (":P31", "P279", "P361", "P1716", "QUALIFIER"):
            self.assertNotIn(excluded, query)


# A miniature P31/P279 neighbourhood for evaluating the ASK in-memory.
_ASK_FIXTURE_TTL = """\
@prefix wd: <http://www.wikidata.org/entity/> .
@prefix wdt: <http://www.wikidata.org/prop/direct/> .
@prefix geo: <http://www.opengis.net/ont/geosparql#> .

# Q1 -P31-> Q2 -P279-> Q3 -P279-> territory: qualifies, and has a P625.
wd:Q1 wdt:P31 wd:Q2 ;
    wdt:P625 "Point(-77.4366667 37.5408333)"^^geo:wktLiteral .
wd:Q2 wdt:P279 wd:Q3 .
wd:Q3 wdt:P279 wd:Q4835091 .

# Q7 is an instance of an unrelated class: does not qualify.
wd:Q7 wdt:P31 wd:Q5 .

# Q8 qualifies but carries no coordinate.
wd:Q8 wdt:P31 wd:Q4835091 .
"""


class RegionClassQueryTests(SimpleTestCase):
    """Structure and semantics of the eligibility + coordinate SELECT."""

    def _solutions(self, qid):
        store = pyoxigraph.Store()
        store.load(_ASK_FIXTURE_TTL.encode(), format=pyoxigraph.RdfFormat.TURTLE)
        return list(store.query(region_class_query(qid)))

    def test_query_contains_all_allowed_roots(self):
        query = region_class_query("Q42")
        for root in ALLOWED_ROOT_CLASSES:
            self.assertIn(f"wd:{root}", query)
        self.assertNotIn("{qid}", query)
        self.assertNotIn("{roots}", query)

    def test_rejects_invalid_qids(self):
        for bad in ("Q42; DROP ALL", "P31", "", "Q042", None, "Q42 "):
            with self.assertRaises(UnsafeSparqlInput):
                region_class_query(bad)

    def test_query_is_valid_sparql(self):
        # pyoxigraph parses the query eagerly - a syntax error raises here.
        pyoxigraph.Store().query(region_class_query("Q42"))

    def test_eligible_item_returns_its_coordinate(self):
        solutions = self._solutions("Q1")
        self.assertEqual(len(solutions), 1)
        self.assertIn("Point(-77.4366667 37.5408333)", solutions[0]["coord"].value)

    def test_ineligible_item_returns_no_rows(self):
        self.assertEqual(self._solutions("Q7"), [])

    def test_eligible_item_without_coordinate_returns_unbound_row(self):
        # The distinction the admin relies on: a row came back (so the
        # class test passed) but ?coord is unbound.
        solutions = self._solutions("Q8")
        self.assertEqual(len(solutions), 1)
        self.assertIsNone(solutions[0]["coord"])


class CheckRegionResponseTests(SimpleTestCase):
    """Translation of the WDQS JSON response into a RegionCheck."""

    def _check(self, bindings):
        session = mock.Mock()
        session.post.return_value.json.return_value = {
            "results": {"bindings": bindings}
        }
        return check_region("Q42", session=session)

    def test_no_rows_means_ineligible(self):
        result = self._check([])
        self.assertFalse(result.eligible)
        self.assertIsNone(result.coordinate)

    def test_row_with_coordinate(self):
        result = self._check(
            [{"coord": {"value": "Point(-77.4366667 37.5408333)"}}]
        )
        self.assertTrue(result.eligible)
        self.assertAlmostEqual(result.coordinate.x, -77.4366667)
        self.assertAlmostEqual(result.coordinate.y, 37.5408333)

    def test_row_without_coordinate_is_eligible_but_uncoordinated(self):
        result = self._check([{}])
        self.assertTrue(result.eligible)
        self.assertIsNone(result.coordinate)

    def test_unusable_coordinate_treated_as_missing(self):
        result = self._check(
            [{"coord": {"value": "<http://www.wikidata.org/entity/Q111> Point(1 2)"}}]
        )
        self.assertTrue(result.eligible)
        self.assertIsNone(result.coordinate)


@mock.patch("subjects.tasks.update_region_ancestors")
@mock.patch("subjects.tasks.update_subject_ancestors")
@mock.patch("subjects.tasks.MemgraphClient")
@mock.patch("subjects.tasks.commit_closure_to_memgraph")
@mock.patch.object(WikidataItem, "_apply_seed_metadata")
@mock.patch("subjects.tasks.fetch_seed_data")
class RefreshTaskRegionTests(TestCase):
    """Region awareness of the WikidataItem refresh machinery."""

    _SEED_DATA = {
        "metadata": {"coordinate_location": None},
        "groups": {"stub": []},
        "labels": {},
    }

    @classmethod
    def setUpTestData(cls):
        cls.region_item, cls.subject_item, cls.bare_item = (
            WikidataItem.objects.bulk_create(
                [
                    WikidataItem(wikidata_id="Q860", title="Region Item"),
                    WikidataItem(wikidata_id="Q861", title="Subject Item"),
                    WikidataItem(wikidata_id="Q862", title="Bare Ancestor"),
                ]
            )
        )
        cls.region = make_region(
            short_name="Region Item", slug="region-item", wikidata_item=cls.region_item
        )
        cls.subject = Subject.objects.create(
            title="Subject Item", wikidata_item=cls.subject_item
        )

    def test_region_item_refresh_uses_p131_variant(
        self, fetch, apply_meta, commit, client, upd_subject, upd_region
    ):
        fetch.return_value = self._SEED_DATA
        result = _do_refresh_wikidata_item(self.region_item)
        self.assertEqual(result["status"], "success")
        fetch.assert_called_once_with("Q860", include_p131=True)
        upd_region.assert_called_once()
        self.assertEqual(upd_region.call_args.args[0], self.region)
        upd_subject.assert_not_called()

    def test_subject_item_refresh_uses_default_variant(
        self, fetch, apply_meta, commit, client, upd_subject, upd_region
    ):
        fetch.return_value = self._SEED_DATA
        result = _do_refresh_wikidata_item(self.subject_item)
        self.assertEqual(result["status"], "success")
        fetch.assert_called_once_with("Q861", include_p131=False)
        upd_subject.assert_called_once()
        upd_region.assert_not_called()

    def test_region_refresh_stores_coordinate_location(
        self, fetch, apply_meta, commit, client, upd_subject, upd_region
    ):
        fetch.return_value = {
            **self._SEED_DATA,
            "metadata": {"coordinate_location": Point(-77.436111, 37.540833, srid=4326)},
        }
        result = _do_refresh_wikidata_item(self.region_item)
        self.assertEqual(result["status"], "success")
        self.region.refresh_from_db()
        self.assertAlmostEqual(self.region.wikidata_coordinate_location.x, -77.436111)
        self.assertAlmostEqual(self.region.wikidata_coordinate_location.y, 37.540833)

    def test_region_refresh_keeps_coordinate_when_claim_disappears(
        self, fetch, apply_meta, commit, client, upd_subject, upd_region
    ):
        # A P625 that vanishes upstream is far likelier to be vandalism
        # or a bad edit than a real correction, and the column is NOT
        # NULL, so the last known coordinate has to stand.
        Region.objects.filter(pk=self.region.pk).update(
            wikidata_coordinate_location=Point(-77.4, 37.5, srid=4326)
        )
        fetch.return_value = self._SEED_DATA  # metadata carries no coordinate
        result = _do_refresh_wikidata_item(self.region_item)
        self.assertEqual(result["status"], "success")
        self.region.refresh_from_db()
        self.assertAlmostEqual(self.region.wikidata_coordinate_location.x, -77.4)
        self.assertAlmostEqual(self.region.wikidata_coordinate_location.y, 37.5)

    def test_region_refresh_leaves_a_custom_center_alone(
        self, fetch, apply_meta, commit, client, upd_subject, upd_region
    ):
        # The whole point of the override: an admin placed the map center
        # by hand, and an upstream P625 edit must not walk it.
        Region.objects.filter(pk=self.region.pk).update(
            custom_coordinate_location=Point(-77.44, 37.53, srid=4326)
        )
        fetch.return_value = {
            **self._SEED_DATA,
            "metadata": {"coordinate_location": Point(1, 2, srid=4326)},
        }
        _do_refresh_wikidata_item(self.region_item)
        self.region.refresh_from_db()
        self.assertAlmostEqual(self.region.wikidata_coordinate_location.x, 1)
        self.assertAlmostEqual(self.region.custom_coordinate_location.x, -77.44)
        self.assertAlmostEqual(self.region.coordinate_location.x, -77.44)

    def test_subject_refresh_does_not_touch_regions(
        self, fetch, apply_meta, commit, client, upd_subject, upd_region
    ):
        fetch.return_value = {
            **self._SEED_DATA,
            "metadata": {"coordinate_location": Point(1, 2, srid=4326)},
        }
        _do_refresh_wikidata_item(self.subject_item)
        self.region.refresh_from_db()
        self.assertAlmostEqual(self.region.wikidata_coordinate_location.x, TEST_POINT.x)

    def test_stale_rotation_includes_regions_but_not_bare_ancestors(
        self, fetch, apply_meta, commit, client, upd_subject, upd_region
    ):
        # All three fixture items are unhydrated; only the subject-linked
        # and region-linked ones may enter the Beat rotation.
        candidates = set()
        for _ in range(2):
            item = get_next_stale_wikidata_item()
            self.assertIsNotNone(item)
            candidates.add(item.wikidata_id)
            WikidataItem.objects.filter(pk=item.pk).update(
                sparql_last_loaded_at=timezone.now()
            )
        self.assertEqual(candidates, {"Q860", "Q861"})
        self.assertIsNone(get_next_stale_wikidata_item())


class RegionAutocompleteTests(TestCase):
    """The public navbar-selector endpoint at regions:region_autocomplete."""

    @classmethod
    def setUpTestData(cls):
        items = WikidataItem.objects.bulk_create(
            [
                WikidataItem(wikidata_id="Q901", title="Church Hill"),
                WikidataItem(wikidata_id="Q902", title="Manchester"),
                WikidataItem(wikidata_id="Q903", title="Shockoe Bottom"),
            ]
        )
        cls.regions = [
            make_region(
                short_name=item.title,
                long_name=f"{item.title}, Richmond",
                slug=item.title.lower().replace(" ", "-"),
                wikidata_item=item,
            )
            for item in items
        ]
        cls.url = reverse("regions:region_autocomplete")

    def _short_names(self, response):
        return [entry["short_name"] for entry in response.json()]

    def _add_holdings(self, region, *, total, georeferenced):
        """Give a region a public collection with these counts."""
        source = Source.objects.create(
            name=f"Src {region.slug}", slug=f"src-{region.slug}", public=True
        )
        CollectionRegionStats.objects.create(
            collection=Collection.objects.create(
                name=f"Coll {region.slug}", slug=region.slug, source=source, public=True
            ),
            region=region,
            total_images=total,
            georeferenced_high=georeferenced,
        )

    def test_missing_q_returns_popular_regions(self):
        # Every fixture region sits at zero georeferences, so the ranking
        # falls back to long_name.
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self._short_names(response),
            ["Church Hill", "Manchester", "Shockoe Bottom"],
        )

    def test_empty_q_returns_popular_regions(self):
        response = self.client.get(self.url, {"q": ""})
        self.assertEqual(
            self._short_names(response),
            ["Church Hill", "Manchester", "Shockoe Bottom"],
        )

    def test_popular_ranks_by_georeferences_not_library_size(self):
        # Manchester holds the fewest photographs but has the most of them
        # on the map, so it leads; Church Hill, still at zero, trails.
        self._add_holdings(self.regions[1], total=4, georeferenced=3)
        self._add_holdings(self.regions[2], total=90, georeferenced=2)
        self.assertEqual(
            self._short_names(self.client.get(self.url)),
            ["Manchester", "Shockoe Bottom", "Church Hill"],
        )

    def test_popular_skips_unadvertised_regions_that_search_still_finds(self):
        item = WikidataItem.objects.create(wikidata_id="Q904", title="Richmond")
        richmond = make_region(
            short_name="Richmond",
            long_name="Richmond, Virginia",
            slug="richmond",
            wikidata_item=item,
            advertise=False,
        )
        RegionAncestor.objects.create(region=self.regions[0], ancestor=item)
        # Richmond outranks every fixture region, but the admin has not
        # advertised it, so only a search offers it.
        self._add_holdings(richmond, total=50, georeferenced=40)
        self.assertNotIn("Richmond", self._short_names(self.client.get(self.url)))
        self.assertIn(
            "Richmond",
            self._short_names(self.client.get(self.url, {"q": "richmond, v"})),
        )

    def test_popular_ignores_non_public_holdings(self):
        self._add_holdings(self.regions[2], total=9, georeferenced=9)
        Source.objects.update(public=False)
        self.assertEqual(
            self._short_names(self.client.get(self.url)),
            ["Church Hill", "Manchester", "Shockoe Bottom"],
        )

    def test_substring_filter_case_insensitive(self):
        for query in ("shock", "SHOCK"):
            response = self.client.get(self.url, {"q": query})
            self.assertEqual(self._short_names(response), ["Shockoe Bottom"])

    def test_long_name_match(self):
        # "Richmond" appears only in the long names.
        response = self.client.get(self.url, {"q": "richmond"})
        self.assertEqual(len(response.json()), 3)

    def test_no_match_returns_empty(self):
        response = self.client.get(self.url, {"q": "zzzz"})
        self.assertEqual(response.json(), [])

    def test_overlong_query_returns_empty(self):
        response = self.client.get(self.url, {"q": "x" * 101})
        self.assertEqual(response.json(), [])

    def test_response_shape(self):
        response = self.client.get(self.url, {"q": "church"})
        self.assertEqual(
            response.json(),
            [
                {
                    "short_name": "Church Hill",
                    "long_name": "Church Hill, Richmond",
                    "wikidata_id": "Q901",
                }
            ],
        )

    def test_limit_cap(self):
        extra = _AUTOCOMPLETE_LIMIT  # 3 existing + 3 more = 6 regions
        items = WikidataItem.objects.bulk_create(
            [
                WikidataItem(wikidata_id=f"Q91{i:03d}", title=f"Ward {i:03d}")
                for i in range(extra)
            ]
        )
        Region.objects.bulk_create(
            [
                Region(
                    short_name=item.title,
                    slug=f"ward-{i:03d}",
                    wikidata_item=item,
                    wikidata_coordinate_location=TEST_POINT,
                    advertise=True,
                )
                for i, item in enumerate(items)
            ]
        )
        response = self.client.get(self.url)
        self.assertEqual(len(response.json()), _AUTOCOMPLETE_LIMIT)

    def test_single_query(self):
        with self.assertNumQueries(1):
            self.client.get(self.url, {"q": "man"})

    def test_popular_costs_two_queries(self):
        # The summaries behind the popular list: one for the per-region
        # stats, one for the regions themselves.
        with self.assertNumQueries(2):
            self.client.get(self.url)


class CurrentRegionContextProcessorTests(TestCase):
    """Cookie -> current_region resolution on every template render."""

    @classmethod
    def setUpTestData(cls):
        item = WikidataItem.objects.bulk_create(
            [WikidataItem(wikidata_id="Q920", title="Richmond")]
        )[0]
        cls.region = make_region(
            short_name="Richmond", slug="richmond", wikidata_item=item
        )

    def _context(self, cookie=None):
        request = RequestFactory().get("/")
        if cookie is not None:
            request.COOKIES[REGION_COOKIE_NAME] = cookie
        return current_region(request)

    def test_no_cookie_returns_none(self):
        self.assertIsNone(self._context()["current_region"])

    def test_valid_qid_resolves_region(self):
        self.assertEqual(
            self._context("Q920")["current_region"], self.region
        )

    def test_legacy_slug_still_resolves_region(self):
        self.assertEqual(
            self._context("richmond")["current_region"], self.region
        )

    def test_unknown_identifier_returns_none_without_error(self):
        self.assertIsNone(self._context("Q999999999")["current_region"])

    def test_cookie_name_exposed_in_context(self):
        self.assertEqual(
            self._context()["region_cookie_name"], REGION_COOKIE_NAME
        )

    def test_region_map_configuration_is_exposed(self):
        map_bounds = Polygon.from_bbox((-77.7, 37.3, -77.2, 37.8))
        map_bounds.srid = 4326
        Region.objects.filter(pk=self.region.pk).update(map_bounds=map_bounds)
        context = self._context("Q920")
        self.assertEqual(
            context["region_map_center"],
            [TEST_POINT.x, TEST_POINT.y],
        )
        self.assertEqual(
            context["region_map_bounds"],
            [-77.7, 37.3, -77.2, 37.8],
        )
        self.assertEqual(
            context["region_search_bbox"],
            [-77.7, 37.3, -77.2, 37.8],
        )

    def test_no_region_has_no_region_map_configuration(self):
        context = self._context()
        self.assertIsNone(context["region_map_center"])
        self.assertIsNone(context["region_map_bounds"])
        self.assertIsNone(context["region_search_bbox"])


class HomeRegionBranchTests(TestCase):
    """The homepage branches on the navbar region selection."""

    @classmethod
    def setUpTestData(cls):
        item = WikidataItem.objects.bulk_create(
            [WikidataItem(wikidata_id="Q930", title="Richmond")]
        )[0]
        cls.region = make_region(
            short_name="Richmond",
            long_name="Richmond, Virginia",
            slug="richmond",
            wikidata_item=item,
        )

    def test_region_homepage_titled_after_long_name(self):
        self.client.cookies[REGION_COOKIE_NAME] = "richmond"
        response = self.client.get(reverse("home"))
        self.assertTemplateUsed(response, "home.html")
        self.assertContains(response, "Yesterdays of Richmond, Virginia")

    def test_no_region_renders_global_homepage(self):
        response = self.client.get(reverse("home"))
        self.assertTemplateUsed(response, "home_no_region.html")
        self.assertTemplateNotUsed(response, "home.html")

    def test_stale_cookie_renders_global_homepage(self):
        self.client.cookies[REGION_COOKIE_NAME] = "gone"
        response = self.client.get(reverse("home"))
        self.assertTemplateUsed(response, "home_no_region.html")

    def test_region_subtitle_shown(self):
        Region.objects.filter(pk=self.region.pk).update(
            subtitle="Old Richmond, mapped"
        )
        self.client.cookies[REGION_COOKIE_NAME] = "richmond"
        response = self.client.get(reverse("home"))
        self.assertContains(response, "Old Richmond, mapped")

    def test_blank_region_subtitle_falls_back_to_site_subtitle(self):
        site_settings = SiteSettings.load()
        site_settings.site_subtitle = "Sitewide tagline"
        site_settings.save()
        self.client.cookies[REGION_COOKIE_NAME] = "richmond"
        response = self.client.get(reverse("home"))
        self.assertContains(response, "Sitewide tagline")


class HomeFeaturedImageRegionTests(TestCase):
    """The homepage's featured image is drawn from the selected region's queue."""

    @classmethod
    def setUpTestData(cls):
        items = WikidataItem.objects.bulk_create(
            [
                WikidataItem(wikidata_id="Q43421", title="Richmond"),
                WikidataItem(wikidata_id="Q49231", title="Norfolk"),
            ]
        )
        cls.richmond = make_region(
            short_name="Richmond",
            long_name="Richmond, Virginia",
            slug="richmond",
            wikidata_item=items[0],
        )
        cls.norfolk = make_region(
            short_name="Norfolk",
            long_name="Norfolk, Virginia",
            slug="norfolk",
            wikidata_item=items[1],
        )
        source = Source.objects.create(
            name="Src", slug="src", url="https://example.com", description=""
        )
        collection = Collection.objects.create(
            source=source, name="Col", slug="col", url="https://example.com"
        )
        today = timezone.localdate()
        cls.entries = {}
        for region, title in ((cls.richmond, "Broad Street"), (cls.norfolk, "Granby")):
            image = Image.objects.create(
                collection=collection,
                title=title,
                permalink=f"https://img.example.com/{region.slug}.jpg",
            )
            cls.entries[region.slug] = ImageOfTheDay.objects.create(
                image=image, region=region, day=today
            )

    def _featured(self, slug):
        self.client.cookies[REGION_COOKIE_NAME] = slug
        return self.client.get(reverse("home")).context["featured_entry"]

    def test_each_region_features_its_own_queue(self):
        self.assertEqual(self._featured("richmond"), self.entries["richmond"])
        self.assertEqual(self._featured("norfolk"), self.entries["norfolk"])

    def test_region_with_an_empty_queue_features_nothing(self):
        # Bypasses the model's delete(), which is queue reflow, not teardown.
        ImageOfTheDay.objects.filter(region=self.norfolk).delete()
        self.assertIsNone(self._featured("norfolk"))
        self.assertEqual(self._featured("richmond"), self.entries["richmond"])


class RegionSummaryTests(TestCase):
    """Region summaries on the global homepage.

    The same dicts render the server-side picker cards and are serialized
    into the map's json_script (see regions.summaries.get_region_summaries),
    so most assertions read the JSON payload.
    """

    @classmethod
    def setUpTestData(cls):
        items = WikidataItem.objects.bulk_create(
            [
                WikidataItem(wikidata_id="Q1370", title="Virginia"),
                WikidataItem(wikidata_id="Q1439", title="Texas"),
            ]
        )
        cls.virginia = make_region(
            short_name="Virginia",
            long_name="Virginia, United States",
            slug="virginia",
            wikidata_item=items[0],
            wikidata_coordinate_location=Point(-78.6569, 37.4316, srid=4326),
        )
        cls.texas = make_region(
            short_name="Texas",
            long_name="Texas, United States",
            slug="texas",
            wikidata_item=items[1],
            wikidata_coordinate_location=Point(-99.9018, 31.9686, srid=4326),
        )

    def _summaries(self):
        response = self.client.get(reverse("home"))
        self.assertTemplateUsed(response, "home_no_region.html")
        return json.loads(
            re.search(
                rb'<script id="region-summaries-data" type="application/json">(.*?)</script>',
                response.content,
                re.DOTALL,
            ).group(1)
        )

    def _summary(self, qid):
        return next(s for s in self._summaries() if s["wikidata_id"] == qid)

    def _make_collection(self, slug, *, source_public=True, collection_public=True):
        source = Source.objects.create(
            name=f"Src {slug}", slug=f"src-{slug}", public=source_public
        )
        return Collection.objects.create(
            name=f"Coll {slug}", slug=slug, source=source, public=collection_public
        )

    def test_every_region_becomes_a_summary(self):
        # Both regions are at zero images, so the busiest-first ordering
        # falls back to long_name.
        self.assertEqual(
            [s["wikidata_id"] for s in self._summaries()], ["Q1439", "Q1370"]
        )

    def test_advertise_controls_summary_regardless_of_hierarchy(self):
        richmond_item = WikidataItem.objects.create(
            wikidata_id="Q43421", title="Richmond"
        )
        richmond = make_region(
            short_name="Richmond",
            long_name="Richmond, Virginia",
            slug="richmond",
            wikidata_item=richmond_item,
            advertise=False,
        )
        RegionAncestor.objects.create(
            region=richmond, ancestor=self.virginia.wikidata_item
        )

        self.assertEqual(
            [s["wikidata_id"] for s in self._summaries()], ["Q1439", "Q1370"]
        )

        Region.objects.filter(pk=self.virginia.pk).update(advertise=False)
        Region.objects.filter(pk=richmond.pk).update(advertise=True)
        self.assertEqual(
            [s["wikidata_id"] for s in self._summaries()], ["Q43421", "Q1439"]
        )

    def test_summary_carries_names_and_coordinate(self):
        summary = self._summary("Q1370")
        self.assertEqual(summary["short_name"], "Virginia")
        self.assertEqual(summary["long_name"], "Virginia, United States")
        # Carried for the autocomplete endpoint, which builds its popular
        # list out of these dicts (regions.views._popular_regions).
        self.assertEqual(summary["wikidata_id"], "Q1370")
        self.assertEqual(summary["lng"], -78.6569)
        self.assertEqual(summary["lat"], 37.4316)

    def test_region_without_holdings_summarizes_to_zero(self):
        summary = self._summary("Q1370")
        self.assertEqual(summary["image_count"], 0)
        self.assertEqual(summary["georeferenced_count"], 0)
        self.assertIsNone(summary["thumbnail"])

    def test_counts_sum_public_collections(self):
        CollectionRegionStats.objects.create(
            collection=self._make_collection("first"),
            region=self.virginia,
            total_images=5,
            georeferenced_low=1,
            georeferenced_medium=2,
        )
        CollectionRegionStats.objects.create(
            collection=self._make_collection("second"),
            region=self.virginia,
            total_images=7,
            georeferenced_high=3,
        )
        summary = self._summary("Q1370")
        self.assertEqual(summary["image_count"], 12)
        self.assertEqual(summary["georeferenced_count"], 6)

    def test_non_public_holdings_are_invisible(self):
        CollectionRegionStats.objects.create(
            collection=self._make_collection("private", collection_public=False),
            region=self.virginia,
            total_images=5,
        )
        CollectionRegionStats.objects.create(
            collection=self._make_collection("shrouded", source_public=False),
            region=self.virginia,
            total_images=7,
        )
        self.assertEqual(self._summary("Q1370")["image_count"], 0)

    def test_busiest_region_comes_first(self):
        CollectionRegionStats.objects.create(
            collection=self._make_collection("busy"),
            region=self.virginia,
            total_images=5,
        )
        self.assertEqual(
            [s["wikidata_id"] for s in self._summaries()], ["Q1370", "Q1439"]
        )

    def test_thumbnail_follows_the_representative_image(self):
        collection = self._make_collection("representative")
        with_thumb = Image.objects.create(
            collection=collection,
            title="Virginia's pick",
            permalink="https://img.example.com/va.jpg",
            thumbnail="https://img.example.com/va-thumb.jpg",
        )
        without_thumb = Image.objects.create(
            collection=collection,
            title="Texas's pick",
            permalink="https://img.example.com/tx.jpg",
        )
        Region.objects.filter(pk=self.virginia.pk).update(
            representative_image=with_thumb
        )
        Region.objects.filter(pk=self.texas.pk).update(
            representative_image=without_thumb
        )
        self.assertEqual(
            self._summary("Q1370")["thumbnail"],
            "https://img.example.com/va-thumb.jpg",
        )
        # A representative image that hasn't got a thumbnail yet reads as
        # "no photo", not as an empty URL.
        self.assertIsNone(self._summary("Q1439")["thumbnail"])

    def test_cards_render_alongside_the_map(self):
        response = self.client.get(reverse("home"))
        self.assertContains(response, 'data-region-qid="Q1370"')
        self.assertContains(response, 'data-region-qid="Q1439"')

    def test_global_homepage_carries_the_sitewide_feed(self):
        # The feed itself is exercised by the activity app's tests; here
        # it only matters that the no-region branch supplies the context
        # the embed renders from.
        response = self.client.get(reverse("home"))
        self.assertIn("activity_events", response.context)

    def test_summary_follows_a_custom_centerpoint(self):
        Region.objects.filter(pk=self.virginia.pk).update(
            custom_coordinate_location=Point(-77.44, 37.53, srid=4326)
        )
        summary = self._summary("Q1370")
        self.assertEqual([summary["lng"], summary["lat"]], [-77.44, 37.53])

    def test_cookie_name_reaches_the_map_container(self):
        response = self.client.get(reverse("home"))
        self.assertContains(
            response,
            f'id="global-home-map" data-cookie-name="{REGION_COOKIE_NAME}"',
        )

    def test_names_are_escaped_not_interpolated(self):
        # Names are admin-entered free text landing inside a <script>;
        # json_script has to neutralize a closing tag rather than emit it.
        Region.objects.filter(pk=self.texas.pk).update(
            long_name="Texas</script><script>alert(1)</script>"
        )
        response = self.client.get(reverse("home"))
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertEqual(
            self._summary("Q1439")["long_name"],
            "Texas</script><script>alert(1)</script>",
        )


class RegionDirectoryTests(TestCase):
    """The browsable directory at /regions/.

    It shows the global homepage's picker map over a card per region, so
    the assertions below are mostly about which regions reach which of the
    two: every region gets a card, but unadvertised regions' cards start
    hidden (search surfaces them client-side), and only advertised regions
    get a pin.
    """

    @classmethod
    def setUpTestData(cls):
        items = WikidataItem.objects.bulk_create(
            [
                WikidataItem(wikidata_id="Q1370", title="Virginia"),
                WikidataItem(wikidata_id="Q43421", title="Richmond"),
            ]
        )
        cls.virginia = make_region(
            short_name="Virginia",
            long_name="Virginia, United States",
            slug="virginia",
            wikidata_item=items[0],
        )
        cls.richmond = make_region(
            short_name="Richmond",
            long_name="Richmond, Virginia",
            slug="richmond",
            wikidata_item=items[1],
            advertise=False,
        )
        RegionAncestor.objects.create(
            region=cls.richmond, ancestor=cls.virginia.wikidata_item
        )

    def _get(self):
        response = self.client.get(reverse("regions:region_index"))
        self.assertTemplateUsed(response, "regions/browse_regions.html")
        return response

    def _card_qids(self, response):
        return [
            qid.decode()
            for qid in re.findall(rb'data-region-qid="([^"]+)"', response.content)
        ]

    def _pin_qids(self, response):
        payload = json.loads(
            re.search(
                rb'<script id="region-summaries-data" type="application/json">(.*?)</script>',
                response.content,
                re.DOTALL,
            ).group(1)
        )
        return [summary["wikidata_id"] for summary in payload]

    def _stats(self, region, total):
        source = Source.objects.create(
            name=f"Src {region.slug}", slug=f"src-{region.slug}"
        )
        CollectionRegionStats.objects.create(
            collection=Collection.objects.create(
                name=f"Coll {region.slug}", slug=region.slug, source=source
            ),
            region=region,
            total_images=total,
        )

    def _card_tag(self, response, qid):
        return re.search(
            rb'<article[^>]*data-region-qid="%s"[^>]*>' % qid.encode(),
            response.content,
        ).group(0)

    def test_unadvertised_regions_get_a_card_but_no_pin(self):
        response = self._get()
        self.assertEqual(sorted(self._card_qids(response)), ["Q1370", "Q43421"])
        self.assertEqual(self._pin_qids(response), ["Q1370"])

    def test_unadvertised_cards_start_hidden_until_a_search_surfaces_them(self):
        response = self._get()
        virginia = self._card_tag(response, "Q1370")
        self.assertNotIn(b"data-region-unadvertised", virginia)
        self.assertNotIn(b"d-none", virginia)
        richmond = self._card_tag(response, "Q43421")
        self.assertIn(b"data-region-unadvertised", richmond)
        self.assertIn(b"d-none", richmond)
        # The visible count matches: one advertised region, not two regions.
        self.assertContains(response, "1 region<")

    def test_page_exposes_the_protomaps_key_for_the_picker_map(self):
        response = self._get()
        self.assertContains(response, "window.PROTOMAPS_API_KEY")

    def test_cards_run_busiest_first(self):
        self._stats(self.richmond, 9)
        response = self._get()
        self.assertEqual(self._card_qids(response), ["Q43421", "Q1370"])

        self._stats(self.virginia, 40)
        self.assertEqual(self._card_qids(self._get()), ["Q1370", "Q43421"])

    def test_card_leads_with_the_representative_photograph(self):
        source = Source.objects.create(name="Src", slug="src")
        collection = Collection.objects.create(
            name="Coll", slug="coll", source=source
        )
        self.richmond.representative_image = Image.objects.create(
            collection=collection,
            title="Richmond's pick",
            permalink="https://img.example.com/rva.jpg",
            thumbnail="https://img.example.com/rva-thumb.jpg",
        )
        self.richmond.save(update_fields=["representative_image"])
        response = self._get()
        self.assertContains(response, "https://img.example.com/rva-thumb.jpg")
        # Virginia has no representative image, so its card falls back to the
        # placeholder rather than a broken image.
        self.assertContains(response, "region-summary-thumb-empty")

    def test_counts_reach_the_cards(self):
        self._stats(self.richmond, 1200)
        self.assertContains(self._get(), "1,200 photos")


class GlobalHomeHeroTests(TestCase):
    """The global homepage when no photograph can lead the hero."""

    @classmethod
    def setUpTestData(cls):
        source = Source.objects.create(name="Src", slug="src", public=True)
        cls.collection = Collection.objects.create(
            name="Coll", slug="coll", source=source, public=True
        )

    def setUp(self):
        # The hero pool is cached outside the test transaction.
        cache.clear()

    def test_site_without_georeferences_has_no_hero_and_still_renders(self):
        Image.objects.create(
            collection=self.collection,
            title="Unplaced",
            permalink="https://img.example.com/unplaced.jpg",
            thumbnail="https://img.example.com/unplaced-thumb.jpg",
        )
        response = self.client.get(reverse("home"))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["hero_feature"])


class GlobalHomeSubjectsTests(TestCase):
    """The subjects band on the global homepage.

    yesterdays.views.get_subjects_feature reads the one photograph an admin
    chose (SiteSettings.home_subjects_image) and the subjects tagged in it.
    The band renders only when that photograph is still showable and has
    something to label it with.
    """

    @classmethod
    def setUpTestData(cls):
        cls.source = Source.objects.create(name="Src", slug="src", public=True)
        cls.collection = Collection.objects.create(
            name="Coll", slug="coll", source=cls.source, public=True
        )

    def setUp(self):
        # SiteSettings.load memoizes the settings row in the process-local
        # cache, which outlives any one test and is not rolled back with the
        # database. Clearing afterwards as well as before matters here
        # because these tests point the settings at an image: a later test
        # that saved the cached row back would be writing a foreign key to
        # an image this one's rollback has already taken away.
        cache.clear()
        self.addCleanup(cache.clear)

    def _image(self, title="Broad Street", *, collection=None, **kwargs):
        kwargs.setdefault("permalink", "https://img.example.com/broad.jpg")
        kwargs.setdefault("thumbnail", "https://img.example.com/broad-thumb.jpg")
        return Image.objects.create(
            collection=collection or self.collection, title=title, **kwargs
        )

    def _tag(self, image, title, wikidata_id, order=0):
        # bulk_create, because WikidataItem.save fetches the item's metadata
        # from Wikidata and a test has no business reaching the network.
        item = WikidataItem.objects.bulk_create(
            [WikidataItem(wikidata_id=wikidata_id, title=title)]
        )[0]
        subject = Subject.objects.create(title=title, wikidata_item=item)
        return SubjectMapping.objects.create(image=image, subject=subject, order=order)

    @staticmethod
    def _titles(feature):
        """Every label the band shows, in the order they read."""
        return [
            subject["title"]
            for subject in feature["subjects_before"] + feature["subjects_after"]
        ]

    def _feature(self, image):
        SiteSettings.objects.update_or_create(
            pk=1, defaults={"home_subjects_image": image}
        )
        response = self.client.get(reverse("home"))
        self.assertTemplateUsed(response, "home_no_region.html")
        return response.context["subjects_feature"]

    def test_labels_follow_the_curated_order(self):
        # The order curators gave them on the image page, not alphabetical
        # and not by id.
        image = self._image()
        self._tag(image, "Main Street Station", "Q1", order=2)
        self._tag(image, "Old City Hall", "Q2", order=1)
        feature = self._feature(image)
        self.assertEqual(
            self._titles(feature), ["Old City Hall", "Main Street Station"]
        )
        self.assertEqual(feature["extra_subject_count"], 0)

    def test_labels_are_halved_for_the_columns_flanking_the_photograph(self):
        # The odd one out goes to the first half, because the second also
        # carries the overflow link when there is one.
        image = self._image()
        for index in range(3):
            self._tag(image, f"Subject {index}", f"Q{index + 1}", order=index)
        feature = self._feature(image)
        self.assertEqual(
            [len(feature["subjects_before"]), len(feature["subjects_after"])], [2, 1]
        )
        self.assertEqual(
            self._titles(feature), ["Subject 0", "Subject 1", "Subject 2"]
        )

    def test_band_renders_the_labels_as_links(self):
        image = self._image()
        mapping = self._tag(image, "Main Street Station", "Q1")
        self.assertIsNotNone(self._feature(image))
        response = self.client.get(reverse("home"))
        self.assertContains(response, "The Rabbithole Goes Deep")
        self.assertContains(response, mapping.subject.get_absolute_url())

    def test_no_chosen_photograph_hides_the_band(self):
        image = self._image()
        self._tag(image, "Main Street Station", "Q1")
        response = self.client.get(reverse("home"))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["subjects_feature"])
        self.assertNotContains(response, "The Rabbithole Goes Deep")

    def test_untagged_photograph_hides_the_band(self):
        # Nothing to label it with, so the band would be a picture under a
        # heading about subjects.
        self.assertIsNone(self._feature(self._image()))

    def test_non_public_holdings_are_never_labelled(self):
        hidden_source = Source.objects.create(
            name="Hidden", slug="hidden-src", public=False
        )
        for collection in (
            Collection.objects.create(
                name="Hidden", slug="hidden", source=hidden_source, public=True
            ),
            Collection.objects.create(
                name="Private", slug="private", source=self.source, public=False
            ),
        ):
            image = self._image(f"In {collection.slug}", collection=collection)
            self._tag(image, f"Subject in {collection.slug}", f"Q{collection.pk}")
            self.assertIsNone(self._feature(image), collection.slug)

    def test_photograph_without_a_thumbnail_is_never_labelled(self):
        image = self._image("No thumbnail", thumbnail="")
        self._tag(image, "Main Street Station", "Q1")
        self.assertIsNone(self._feature(image))

    def test_duplicate_photograph_is_never_labelled(self):
        original = self._image("Original")
        duplicate = self._image("Duplicate", duplicate_of=original)
        self._tag(duplicate, "Main Street Station", "Q1")
        self.assertIsNone(self._feature(duplicate))

    def test_surplus_labels_are_counted_rather_than_dropped(self):
        image = self._image()
        for index in range(HOME_SUBJECT_LABEL_LIMIT + 3):
            self._tag(image, f"Subject {index:02d}", f"Q{index}", order=index)
        feature = self._feature(image)
        self.assertEqual(len(self._titles(feature)), HOME_SUBJECT_LABEL_LIMIT)
        self.assertEqual(feature["extra_subject_count"], 3)
        response = self.client.get(reverse("home"))
        self.assertContains(response, "+3 more in this photograph")


class MapDisplayCenterTests(TestCase):
    """images/partials/map_display.html centering precedence."""

    url = "/map/embed/"

    @classmethod
    def setUpTestData(cls):
        SiteSettings.objects.update_or_create(
            pk=1,
            defaults={
                "default_map_longitude": -1.5,
                "default_map_latitude": 52.5,
                "default_search_bbox_west": -2,
                "default_search_bbox_south": 52,
                "default_search_bbox_east": -1,
                "default_search_bbox_north": 53,
            },
        )
        item = WikidataItem.objects.bulk_create(
            [WikidataItem(wikidata_id="Q1370", title="Virginia")]
        )[0]
        cls.region = make_region(
            short_name="Virginia",
            slug="virginia",
            wikidata_item=item,
            wikidata_coordinate_location=Point(-78.6569, 37.4316, srid=4326),
        )

    def test_falls_back_to_site_default_without_a_region(self):
        resp = self.client.get(self.url)
        self.assertContains(resp, "const mapCenter = [-1.5, 52.5];")

    def test_uses_the_selected_region_coordinate(self):
        self.client.cookies[REGION_COOKIE_NAME] = self.region.slug
        resp = self.client.get(self.url)
        self.assertContains(resp, "const mapCenter = [-78.6569, 37.4316];")

    def test_custom_centerpoint_outranks_the_wikidata_one(self):
        Region.objects.filter(pk=self.region.pk).update(
            custom_coordinate_location=Point(-77.44, 37.53, srid=4326)
        )
        self.client.cookies[REGION_COOKIE_NAME] = self.region.slug
        resp = self.client.get(self.url)
        self.assertContains(resp, "const mapCenter = [-77.44, 37.53];")

    def test_explicit_center_outranks_the_region(self):
        bounds = Polygon.from_bbox((-79, 36, -75, 40))
        bounds.srid = 4326
        Region.objects.filter(pk=self.region.pk).update(map_bounds=bounds)
        self.client.cookies[REGION_COOKIE_NAME] = self.region.slug
        resp = self.client.get(self.url, {"center_lng": "-77.44", "center_lat": "37.53"})
        self.assertContains(resp, "const mapCenter = [-77.44, 37.53];")
        self.assertContains(resp, "const mapBounds = null;")

    def test_selected_region_supplies_responsive_map_bounds(self):
        bounds = Polygon.from_bbox((-79, 36, -75, 40))
        bounds.srid = 4326
        Region.objects.filter(pk=self.region.pk).update(map_bounds=bounds)
        self.client.cookies[REGION_COOKIE_NAME] = self.region.slug
        resp = self.client.get(self.url)
        self.assertContains(resp, "const mapBounds = [-79.0, 36.0, -75.0, 40.0];")

    def test_explicit_zoom_outranks_region_bounds(self):
        bounds = Polygon.from_bbox((-79, 36, -75, 40))
        bounds.srid = 4326
        Region.objects.filter(pk=self.region.pk).update(map_bounds=bounds)
        self.client.cookies[REGION_COOKIE_NAME] = self.region.slug
        resp = self.client.get(self.url, {"zoom_level": "8.5"})
        self.assertContains(resp, "const mapBounds = null;")

    def test_unknown_region_slug_falls_back_to_site_default(self):
        self.client.cookies[REGION_COOKIE_NAME] = "no-such-region"
        resp = self.client.get(self.url)
        self.assertContains(resp, "const mapCenter = [-1.5, 52.5];")
