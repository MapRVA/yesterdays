import datetime
import json
import re
import threading
from contextlib import contextmanager
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.contrib.gis.geos import Point, Polygon
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models import F, ProtectedError
from django.http import Http404
from django.test import (
    Client,
    SimpleTestCase,
    TestCase,
    TransactionTestCase,
    override_settings,
)
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from PIL import Image as PILImage

from images.management.commands.detect_addresses import (
    Command as DetectAddressesCommand,
)
from images import policies
from images.admin import SiteSettingsAdminForm
from images.models import (
    AerialGeoreference,
    Album,
    AlbumImage,
    Collection,
    CollectionRegionStats,
    CollectionStats,
    Comment,
    Georeference,
    GeoreferenceValidation,
    Image,
    ImageOfTheDay,
    SiteSettings,
    Source,
    SubjectMapping,
)
from images.tasks import (
    process_image,
    reconcile_collection_region_stats,
    reconcile_collection_stats,
)
from images.utils import compass_label, get_confidence_breakdown, get_overall_stats
from regions.context_processors import REGION_COOKIE_NAME
from regions.models import Region, RegionAncestor
from regions.tests import make_region
from subjects.models import Subject, WikidataItem


class RegionsMigrationTests(TransactionTestCase):
    migrate_from = ("images", "0063_duplicateimagepair_uuid")
    migrate_to = ("images", "0064_regions")

    def test_legacy_content_is_backfilled(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps

        Source = old_apps.get_model("images", "Source")
        Collection = old_apps.get_model("images", "Collection")
        Image = old_apps.get_model("images", "Image")
        ImageOfTheDay = old_apps.get_model("images", "ImageOfTheDay")

        source = Source.objects.create(
            name="Legacy source",
            slug="legacy-source",
            url="https://example.com/source",
            description="",
        )
        collection = Collection.objects.create(
            source=source,
            name="Legacy collection",
            slug="legacy-collection",
            url="https://example.com/collection",
        )
        image = Image.objects.create(
            collection=collection,
            title="Legacy image",
            permalink="https://example.com/image.jpg",
        )
        ImageOfTheDay.objects.create(image=image, day=datetime.date(2026, 8, 1))

        executor.loader.build_graph()
        executor.migrate([self.migrate_to])
        new_apps = executor.loader.project_state([self.migrate_to]).apps

        Region = new_apps.get_model("regions", "Region")
        Source = new_apps.get_model("images", "Source")
        ImageOfTheDay = new_apps.get_model("images", "ImageOfTheDay")
        CollectionRegionStats = new_apps.get_model(
            "images", "CollectionRegionStats"
        )

        region = Region.objects.get()
        self.assertEqual(region.slug, "richmond")
        self.assertEqual(region.wikidata_item.wikidata_id, "Q43421")
        self.assertEqual(Source.objects.get(pk=source.pk).region_id, region.pk)
        self.assertEqual(ImageOfTheDay.objects.get().region_id, region.pk)

        stats = CollectionRegionStats.objects.get(
            collection_id=collection.pk,
            region_id=region.pk,
        )
        self.assertEqual(stats.total_images, 1)


class ImageOfTheDayTests(TestCase):
    """Behavior of the Image of the Day queue: default slotting, the ripple on
    insert, the slide-back on delete, locked anchors, the deferred unique
    constraint that makes the reflow possible, and the region boundary every
    one of those operations stays inside."""

    # A fixed anchor so day arithmetic in assertions is easy to read:
    # self.d(1) is June 1 2026, self.d(2) is June 2, and so on.
    BASE = datetime.date(2026, 6, 1)

    @classmethod
    def setUpTestData(cls):
        cls.source = Source.objects.create(
            name="Src", slug="src", url="https://example.com", description=""
        )
        cls.collection = Collection.objects.create(
            source=cls.source, name="Col", slug="col", url="https://example.com"
        )
        # Every queue operation is scoped to one region, so the fixtures
        # carry two: self.region is the one under test, self.elsewhere is
        # the queue that must never move. bulk_create bypasses
        # WikidataItem.save(), which fetches live Wikidata metadata.
        items = WikidataItem.objects.bulk_create(
            [
                WikidataItem(wikidata_id="Q43421", title="Richmond"),
                WikidataItem(wikidata_id="Q49231", title="Norfolk"),
            ]
        )
        cls.region = make_region(
            short_name="Richmond",
            long_name="Richmond, Virginia",
            slug="richmond",
            wikidata_item=items[0],
        )
        cls.elsewhere = make_region(
            short_name="Norfolk",
            long_name="Norfolk, Virginia",
            slug="norfolk",
            wikidata_item=items[1],
        )

    # -- helpers ------------------------------------------------------------

    def d(self, n):
        """Day N of the queue (1-indexed) as a concrete date."""
        return self.BASE + datetime.timedelta(days=n - 1)

    def img(self, title):
        return Image.objects.create(
            collection=self.collection,
            title=title,
            permalink=f"https://img.example.com/{title}.jpg",
        )

    def entry(self, title, n, locked=False, region=None, **kwargs):
        """Put a new image straight onto day N, bypassing the reflow.

        Defaults to self.region so tests that only care about one queue
        don't have to say so every time.
        """
        return ImageOfTheDay.objects.create(
            image=self.img(title),
            region=region or self.region,
            day=self.d(n),
            locked=locked,
            **kwargs,
        )

    def queue(self, region=None):
        """One region's queue as {day: image_title} for compact assertions."""
        return {
            e.day: e.image.title
            for e in ImageOfTheDay.objects.filter(
                region=region or self.region
            ).select_related("image")
        }

    # -- next_available_day -------------------------------------------------

    def test_next_available_day_empty_is_today(self):
        with patch.object(timezone, "localdate", return_value=self.d(1)):
            self.assertEqual(
                ImageOfTheDay.next_available_day(self.region), self.d(1)
            )

    def test_next_available_day_skips_taken_day(self):
        self.entry("A", 1)
        with patch.object(timezone, "localdate", return_value=self.d(1)):
            self.assertEqual(
                ImageOfTheDay.next_available_day(self.region), self.d(2)
            )

    def test_next_available_day_after_contiguous_run(self):
        for n in (1, 2, 3):
            self.entry(f"I{n}", n)
        with patch.object(timezone, "localdate", return_value=self.d(1)):
            self.assertEqual(
                ImageOfTheDay.next_available_day(self.region), self.d(4)
            )

    def test_next_available_day_fills_gap_left_by_lock(self):
        # Day 1 taken, day 3 locked, day 2 free -> day 2 is returned, not day 4.
        self.entry("A", 1)
        self.entry("L", 3, locked=True)
        with patch.object(timezone, "localdate", return_value=self.d(1)):
            self.assertEqual(
                ImageOfTheDay.next_available_day(self.region), self.d(2)
            )

    def test_next_available_day_ignores_other_regions(self):
        # A full week elsewhere doesn't push this region's queue along.
        for n in (1, 2, 3, 4, 5, 6, 7):
            self.entry(f"N{n}", n, region=self.elsewhere)
        with patch.object(timezone, "localdate", return_value=self.d(1)):
            self.assertEqual(
                ImageOfTheDay.next_available_day(self.region), self.d(1)
            )

    # -- timezone correctness ----------------------------------------------

    @override_settings(TIME_ZONE="America/New_York")
    def test_next_available_day_uses_site_timezone(self):
        # 03:30 UTC on June 2 is still 23:30 on June 1 in New York.
        instant = datetime.datetime(2026, 6, 2, 3, 30, tzinfo=datetime.timezone.utc)
        with patch.object(timezone, "now", return_value=instant):
            self.assertEqual(
                ImageOfTheDay.next_available_day(self.region),
                datetime.date(2026, 6, 1),
            )

    @override_settings(TIME_ZONE="America/New_York")
    def test_for_today_uses_site_timezone(self):
        instant = datetime.datetime(2026, 6, 2, 3, 30, tzinfo=datetime.timezone.utc)
        entry = ImageOfTheDay.objects.create(
            image=self.img("A"), region=self.region, day=datetime.date(2026, 6, 1)
        )
        with patch.object(timezone, "now", return_value=instant):
            # Local day is June 1; a UTC reading would wrongly return None.
            self.assertEqual(ImageOfTheDay.for_today(self.region), entry)

    def test_for_today_returns_none_when_empty(self):
        with patch.object(timezone, "localdate", return_value=self.d(1)):
            self.assertIsNone(ImageOfTheDay.for_today(self.region))

    def test_for_today_is_per_region(self):
        mine = self.entry("A", 1)
        theirs = self.entry("B", 1, region=self.elsewhere)
        with patch.object(timezone, "localdate", return_value=self.d(1)):
            self.assertEqual(ImageOfTheDay.for_today(self.region), mine)
            self.assertEqual(ImageOfTheDay.for_today(self.elsewhere), theirs)

    # -- current_or_most_recent --------------------------------------------

    def test_current_or_most_recent_is_per_region(self):
        self.entry("A", 1)
        self.entry("B", 2, region=self.elsewhere)
        with patch.object(timezone, "localdate", return_value=self.d(3)):
            # Each region falls back to its own most recent entry, never
            # to another region's more recent one.
            self.assertEqual(
                ImageOfTheDay.current_or_most_recent(self.region).image.title, "A"
            )
            self.assertEqual(
                ImageOfTheDay.current_or_most_recent(self.elsewhere).image.title, "B"
            )

    def test_current_or_most_recent_none_for_empty_region(self):
        self.entry("A", 1)
        with patch.object(timezone, "localdate", return_value=self.d(3)):
            self.assertIsNone(ImageOfTheDay.current_or_most_recent(self.elsewhere))

    # -- place: append ------------------------------------------------------

    def test_place_appends_today_then_forward(self):
        with patch.object(timezone, "localdate", return_value=self.d(1)):
            first = ImageOfTheDay.place(self.img("A"), self.region)
            second = ImageOfTheDay.place(self.img("B"), self.region)
        self.assertEqual(first.day, self.d(1))
        self.assertEqual(second.day, self.d(2))

    def test_place_append_skips_locked_today(self):
        with patch.object(timezone, "localdate", return_value=self.d(1)):
            self.entry("L", 1, locked=True)
            entry = ImageOfTheDay.place(self.img("A"), self.region)
        self.assertEqual(entry.day, self.d(2))

    def test_place_records_locked_flag(self):
        entry = ImageOfTheDay.place(
            self.img("A"), self.region, day=self.d(1), locked=True
        )
        entry.refresh_from_db()
        self.assertTrue(entry.locked)

    def test_place_appends_to_its_own_region(self):
        # A busy queue elsewhere neither blocks day 1 here nor claims it.
        with patch.object(timezone, "localdate", return_value=self.d(1)):
            theirs = ImageOfTheDay.place(self.img("N"), self.elsewhere)
            mine = ImageOfTheDay.place(self.img("A"), self.region)
        self.assertEqual(theirs.day, self.d(1))
        self.assertEqual(mine.day, self.d(1))
        self.assertEqual(mine.region, self.region)

    # -- place: explicit day ------------------------------------------------

    def test_place_on_free_day_moves_nothing(self):
        self.entry("A", 1)
        ImageOfTheDay.place(self.img("B"), self.region, day=self.d(5))
        self.assertEqual(self.queue(), {self.d(1): "A", self.d(5): "B"})

    def test_place_on_occupied_day_ripples_forward(self):
        for n, name in ((1, "A"), (2, "B"), (3, "C")):
            self.entry(name, n)
        ImageOfTheDay.place(self.img("D"), self.region, day=self.d(1))
        self.assertEqual(
            self.queue(),
            {self.d(1): "D", self.d(2): "A", self.d(3): "B", self.d(4): "C"},
        )

    def test_place_ripple_leaves_other_regions_alone(self):
        for n, name in ((1, "A"), (2, "B"), (3, "C")):
            self.entry(name, n)
            self.entry(f"N{n}", n, region=self.elsewhere)

        ImageOfTheDay.place(self.img("D"), self.region, day=self.d(1))

        self.assertEqual(
            self.queue(),
            {self.d(1): "D", self.d(2): "A", self.d(3): "B", self.d(4): "C"},
        )
        # Norfolk's days are untouched: the ripple stops at the region.
        self.assertEqual(
            self.queue(self.elsewhere),
            {self.d(1): "N1", self.d(2): "N2", self.d(3): "N3"},
        )

    def test_place_hops_over_locked_anchor(self):
        self.entry("A", 1)
        self.entry("B", 2)
        locked = self.entry("L", 3, locked=True)
        self.entry("C", 4)

        ImageOfTheDay.place(self.img("D"), self.region, day=self.d(1))

        # Unlocked entries flow around the locked anchor at day 3.
        self.assertEqual(
            self.queue(),
            {
                self.d(1): "D",
                self.d(2): "A",
                self.d(3): "L",
                self.d(4): "B",
                self.d(5): "C",
            },
        )
        locked.refresh_from_db()
        self.assertEqual(locked.day, self.d(3))
        self.assertTrue(locked.locked)

    def test_place_on_locked_day_raises_and_changes_nothing(self):
        self.entry("A", 1)
        self.entry("L", 2, locked=True)
        before = self.queue()
        with self.assertRaises(ValidationError):
            ImageOfTheDay.place(self.img("X"), self.region, day=self.d(2))
        self.assertEqual(self.queue(), before)

    def test_place_ignores_a_lock_in_another_region(self):
        # Norfolk's anchor claims day 2 there, not here.
        self.entry("L", 2, locked=True, region=self.elsewhere)
        entry = ImageOfTheDay.place(self.img("A"), self.region, day=self.d(2))
        self.assertEqual(entry.day, self.d(2))

    # -- delete: slide back -------------------------------------------------

    def test_delete_middle_slides_later_entries_back(self):
        entries = {
            name: self.entry(name, n) for n, name in ((1, "A"), (2, "B"), (3, "C"))
        }
        entries["B"].delete()
        self.assertEqual(self.queue(), {self.d(1): "A", self.d(2): "C"})

    def test_delete_last_moves_nothing(self):
        self.entry("A", 1)
        self.entry("B", 2).delete()
        self.assertEqual(self.queue(), {self.d(1): "A"})

    def test_delete_slides_back_around_locked_anchor(self):
        a = self.entry("A", 1)
        locked = self.entry("L", 2, locked=True)
        self.entry("C", 3)

        a.delete()

        # C slides back past the locked anchor into the freed day 1.
        self.assertEqual(self.queue(), {self.d(1): "C", self.d(2): "L"})
        locked.refresh_from_db()
        self.assertEqual(locked.day, self.d(2))

    def test_delete_locked_entry_raises_and_keeps_it(self):
        locked = self.entry("L", 1, locked=True)
        with self.assertRaises(ValidationError):
            locked.delete()
        self.assertTrue(ImageOfTheDay.objects.filter(pk=locked.pk).exists())

    def test_delete_slide_back_leaves_other_regions_alone(self):
        a = self.entry("A", 1)
        self.entry("B", 2)
        for n, name in ((1, "N1"), (2, "N2")):
            self.entry(name, n, region=self.elsewhere)

        a.delete()

        self.assertEqual(self.queue(), {self.d(1): "B"})
        self.assertEqual(
            self.queue(self.elsewhere), {self.d(1): "N1", self.d(2): "N2"}
        )

    # -- clean: locked move guard ------------------------------------------

    def test_clean_blocks_moving_locked_entry(self):
        locked = self.entry("L", 1, locked=True)
        locked.day = self.d(2)
        with self.assertRaises(ValidationError):
            locked.full_clean()

    def test_clean_allows_moving_unlocked_entry(self):
        entry = self.entry("A", 1)
        entry.day = self.d(5)
        entry.full_clean()  # should not raise

    def test_clean_allows_toggling_lock_without_moving(self):
        entry = self.entry("A", 1)
        entry.locked = True
        entry.full_clean()  # locking in place is allowed
        entry.save()
        entry.locked = False
        entry.full_clean()  # and so is unlocking it

    def test_clean_blocks_changing_region(self):
        entry = self.entry("A", 1)
        entry.region = self.elsewhere
        with self.assertRaises(ValidationError):
            entry.full_clean()

    # -- the deferred unique constraint ------------------------------------

    def test_day_is_unique_within_a_region(self):
        self.entry("A", 1)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.entry("B", 1)
                # The constraint is deferred, so force it to be checked now
                # rather than waiting for a commit that the test never makes.
                connection.cursor().execute("SET CONSTRAINTS ALL IMMEDIATE")

    def test_same_day_allowed_in_different_regions(self):
        self.entry("A", 1)
        self.entry("N", 1, region=self.elsewhere)
        connection.cursor().execute("SET CONSTRAINTS ALL IMMEDIATE")
        self.assertEqual(ImageOfTheDay.objects.filter(day=self.d(1)).count(), 2)

    def test_unique_constraint_is_deferred(self):
        a = self.entry("A", 1)
        b = self.entry("B", 2)
        # Swapping two days requires the rows to transiently share a day.
        # This only succeeds because the constraint is validated at COMMIT,
        # not per-statement -- the same property the ripple relies on.
        with transaction.atomic():
            a.day = self.d(2)
            a.save(update_fields=["day", "updated"])
            b.day = self.d(1)
            b.save(update_fields=["day", "updated"])
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(a.day, self.d(2))
        self.assertEqual(b.day, self.d(1))

    # -- foreign key reuse --------------------------------------------------

    def test_same_image_can_be_featured_on_multiple_days(self):
        image = self.img("A")
        ImageOfTheDay.objects.create(image=image, region=self.region, day=self.d(1))
        ImageOfTheDay.objects.create(image=image, region=self.region, day=self.d(2))
        self.assertEqual(image.featured_days.count(), 2)

    def test_same_image_can_be_featured_in_two_regions(self):
        image = self.img("A")
        ImageOfTheDay.objects.create(image=image, region=self.region, day=self.d(1))
        ImageOfTheDay.objects.create(image=image, region=self.elsewhere, day=self.d(1))
        self.assertEqual(image.featured_days.count(), 2)

    def test_region_in_use_cannot_be_deleted(self):
        self.entry("A", 1)
        with self.assertRaises(ProtectedError):
            self.region.delete()

    # -- move: relocate and pin --------------------------------------------

    def test_move_ripples_at_destination_and_locks(self):
        for n, name in ((1, "A"), (2, "B"), (3, "C"), (4, "D"), (5, "E")):
            self.entry(name, n)
        entry = ImageOfTheDay.objects.get(region=self.region, day=self.d(5))

        moved = ImageOfTheDay.move(entry, self.d(2))

        # E lands pinned at day 2; the entries it displaces ripple forward.
        self.assertEqual(
            self.queue(),
            {
                self.d(1): "A",
                self.d(2): "E",
                self.d(3): "B",
                self.d(4): "C",
                self.d(5): "D",
            },
        )
        self.assertEqual(moved.day, self.d(2))
        self.assertTrue(moved.locked)

    def test_move_vacates_old_day_and_slides_back(self):
        for n, name in ((1, "A"), (2, "B"), (3, "C")):
            self.entry(name, n)
        entry = ImageOfTheDay.objects.get(region=self.region, day=self.d(1))

        ImageOfTheDay.move(entry, self.d(5))

        # Leaving day 1 pulls B and C back; A is pinned out at day 5.
        self.assertEqual(
            self.queue(),
            {self.d(1): "B", self.d(2): "C", self.d(5): "A"},
        )

    def test_move_keeps_the_entry_in_its_region(self):
        entry = self.entry("A", 1)
        self.entry("N", 5, region=self.elsewhere)

        moved = ImageOfTheDay.move(entry, self.d(5))

        self.assertEqual(moved.region, self.region)
        self.assertEqual(self.queue(), {self.d(5): "A"})
        self.assertEqual(self.queue(self.elsewhere), {self.d(5): "N"})

    def test_move_relocks_already_locked_entry(self):
        self.entry("A", 1)
        self.entry("B", 2)
        self.entry("L", 3, locked=True)
        self.entry("C", 4)
        locked = ImageOfTheDay.objects.get(region=self.region, day=self.d(3))

        moved = ImageOfTheDay.move(locked, self.d(1))

        self.assertEqual(
            self.queue(),
            {
                self.d(1): "L",
                self.d(2): "A",
                self.d(3): "B",
                self.d(4): "C",
            },
        )
        self.assertTrue(moved.locked)

    def test_move_to_locked_day_raises_and_rolls_back(self):
        self.entry("A", 1)
        self.entry("L", 2, locked=True)
        entry = ImageOfTheDay.objects.get(region=self.region, day=self.d(1))
        before = self.queue()

        with self.assertRaises(ValidationError):
            ImageOfTheDay.move(entry, self.d(2))

        # The whole move rolls back: the entry is neither deleted nor moved.
        self.assertEqual(self.queue(), before)
        queued = {
            e.day: e.locked
            for e in ImageOfTheDay.objects.filter(region=self.region)
        }
        self.assertFalse(queued[self.d(1)])
        self.assertTrue(queued[self.d(2)])

    def test_move_carries_note_and_user(self):
        user = User.objects.create_user(username="osm_1", first_name="Alice")
        entry = self.entry("A", 1, note="keep me", user=user)

        moved = ImageOfTheDay.move(entry, self.d(5), note="keep me", user=user)

        moved.refresh_from_db()
        self.assertEqual(moved.day, self.d(5))
        self.assertTrue(moved.locked)
        self.assertEqual(moved.note, "keep me")
        self.assertEqual(moved.user, user)
        # The entry is recreated, so the old row is gone.
        self.assertNotEqual(moved.pk, entry.pk)
        self.assertFalse(ImageOfTheDay.objects.filter(pk=entry.pk).exists())

    # -- unlock: free and compact ------------------------------------------

    def test_unlock_moves_to_first_available_day(self):
        for n, name in ((1, "A"), (2, "B"), (3, "C")):
            self.entry(name, n)
        anchor = self.entry("D", 10, locked=True)

        with patch.object(timezone, "localdate", return_value=self.d(1)):
            ImageOfTheDay.unlock(anchor)

        # Freed from day 10, D drops into the first open day, 4.
        self.assertEqual(
            self.queue(),
            {self.d(1): "A", self.d(2): "B", self.d(3): "C", self.d(4): "D"},
        )
        anchor.refresh_from_db()
        self.assertFalse(anchor.locked)

    def test_unlock_stays_when_already_earliest(self):
        anchor = self.entry("A", 1, locked=True)
        self.entry("B", 2)
        self.entry("C", 3)

        with patch.object(timezone, "localdate", return_value=self.d(1)):
            ImageOfTheDay.unlock(anchor)

        # Day 1 is already the earliest slot, so it just unlocks in place.
        self.assertEqual(
            self.queue(),
            {self.d(1): "A", self.d(2): "B", self.d(3): "C"},
        )
        anchor.refresh_from_db()
        self.assertFalse(anchor.locked)

    def test_unlock_slides_later_entries_back(self):
        self.entry("A", 1)
        self.entry("B", 2)
        anchor = self.entry("L", 5, locked=True)
        self.entry("C", 6)

        with patch.object(timezone, "localdate", return_value=self.d(1)):
            ImageOfTheDay.unlock(anchor)

        # L drops to the first open day (3); C compacts up behind it.
        self.assertEqual(
            self.queue(),
            {self.d(1): "A", self.d(2): "B", self.d(3): "L", self.d(4): "C"},
        )

    def test_unlock_compacts_around_remaining_locks(self):
        self.entry("A", 1)
        self.entry("B", 2)
        held = self.entry("L", 3, locked=True)
        anchor = self.entry("T", 10, locked=True)

        with patch.object(timezone, "localdate", return_value=self.d(1)):
            ImageOfTheDay.unlock(anchor)

        # T drops to day 4 (1, 2 taken, 3 still locked); the other lock holds.
        self.assertEqual(
            self.queue(),
            {self.d(1): "A", self.d(2): "B", self.d(3): "L", self.d(4): "T"},
        )
        held.refresh_from_db()
        anchor.refresh_from_db()
        self.assertTrue(held.locked)
        self.assertFalse(anchor.locked)

    def test_unlock_compacts_only_its_own_region(self):
        self.entry("A", 1)
        anchor = self.entry("L", 5, locked=True)
        # A gap at day 2 elsewhere must survive: compaction is region-local.
        self.entry("N1", 1, region=self.elsewhere)
        self.entry("N3", 3, region=self.elsewhere)

        with patch.object(timezone, "localdate", return_value=self.d(1)):
            ImageOfTheDay.unlock(anchor)

        self.assertEqual(self.queue(), {self.d(1): "A", self.d(2): "L"})
        self.assertEqual(
            self.queue(self.elsewhere), {self.d(1): "N1", self.d(3): "N3"}
        )


class FeaturedImageQueueViewTests(TestCase):
    """The staff views that build the queues: which region an image joins,
    and which region's queue the staff page shows."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username="staff", first_name="Sam", is_staff=True
        )
        source = Source.objects.create(
            name="Src", slug="src", url="https://example.com", description=""
        )
        cls.collection = Collection.objects.create(
            source=source, name="Col", slug="col", url="https://example.com"
        )
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

    def setUp(self):
        self.client.force_login(self.staff)
        self.image = Image.objects.create(
            collection=self.collection,
            title="Broad Street",
            permalink="https://img.example.com/broad.jpg",
        )

    def _queue(self, **payload):
        return self.client.post(
            reverse("images:queue_featured_image", args=[self.image.id]),
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_queueing_requires_a_region(self):
        response = self._queue(day="", note="")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ImageOfTheDay.objects.exists())

    def test_unknown_region_is_rejected(self):
        response = self._queue(region="atlantis")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ImageOfTheDay.objects.exists())

    def test_image_joins_the_chosen_region(self):
        response = self._queue(region="norfolk")
        self.assertEqual(response.status_code, 201)
        entry = ImageOfTheDay.objects.get()
        # The chosen region wins outright — the image's own region (none
        # here) is only ever the modal's default.
        self.assertEqual(entry.region, self.norfolk)
        self.assertEqual(entry.day, timezone.localdate())
        self.assertFalse(entry.locked)

    def test_explicit_day_pins_the_entry(self):
        day = timezone.localdate() + datetime.timedelta(days=3)
        response = self._queue(region="richmond", day=day.isoformat())
        self.assertEqual(response.status_code, 201)
        entry = ImageOfTheDay.objects.get()
        self.assertEqual(entry.day, day)
        self.assertTrue(entry.locked)

    def test_queue_page_shows_only_the_navbar_region(self):
        ImageOfTheDay.place(self.image, self.richmond)
        ImageOfTheDay.place(self.image, self.norfolk)

        self.client.cookies[REGION_COOKIE_NAME] = "richmond"
        response = self.client.get(reverse("images:featured_image_queue"))

        self.assertEqual(response.context["region"], self.richmond)
        self.assertEqual(
            [e.region for e in response.context["entries"]], [self.richmond]
        )

    def test_queue_page_without_a_region_has_no_queue(self):
        ImageOfTheDay.place(self.image, self.richmond)
        response = self.client.get(reverse("images:featured_image_queue"))
        self.assertIsNone(response.context["region"])
        self.assertIsNone(response.context["entries"])


class FeaturedQueueNavbarAlertTests(TestCase):
    """The navbar notification dot flagging a low featured-image queue."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username="staff", first_name="Sam", is_staff=True
        )
        source = Source.objects.create(
            name="Src", slug="src", url="https://example.com", description=""
        )
        collection = Collection.objects.create(
            source=source, name="Col", slug="col", url="https://example.com"
        )
        cls.image = Image.objects.create(
            collection=collection,
            title="Broad Street",
            permalink="https://img.example.com/broad.jpg",
        )
        item = WikidataItem.objects.create(wikidata_id="Q43421", title="Richmond")
        cls.region = make_region(
            short_name="Richmond",
            long_name="Richmond, Virginia",
            slug="richmond",
            wikidata_item=item,
        )

    def setUp(self):
        self.client.force_login(self.staff)
        self.client.cookies[REGION_COOKIE_NAME] = "richmond"

    def _context(self):
        response = self.client.get(reverse("images:featured_image_queue"))
        return response.context

    def test_never_featured_region_gets_no_dot(self):
        # No queue and no history: the feature simply isn't in use here, so
        # there is nothing to nag about.
        self.assertIsNone(self._context().get("featured_queue_badge_class"))

    def test_lapsed_queue_gets_a_dot(self):
        # A region that has featured before but has run dry is a real lapse.
        ImageOfTheDay.objects.create(
            image=self.image,
            region=self.region,
            day=timezone.localdate() - datetime.timedelta(days=1),
        )
        context = self._context()
        self.assertEqual(context["featured_queue_days"], 0)
        self.assertEqual(context["featured_queue_badge_class"], "bg-danger")

    def test_healthy_queue_gets_no_dot(self):
        for offset in range(8):
            ImageOfTheDay.objects.create(
                image=self.image,
                region=self.region,
                day=timezone.localdate() + datetime.timedelta(days=offset),
            )
        self.assertIsNone(self._context()["featured_queue_badge_class"])


class GeoreferenceRegionQueueTests(TestCase):
    """The navbar region scopes only the default contribution queue."""

    @classmethod
    def setUpTestData(cls):
        items = WikidataItem.objects.bulk_create(
            [
                WikidataItem(wikidata_id="Q1370", title="Virginia"),
                WikidataItem(wikidata_id="Q43421", title="Richmond"),
                WikidataItem(wikidata_id="Q49231", title="Norfolk"),
                WikidataItem(wikidata_id="Q100000", title="Outside subject"),
            ]
        )
        cls.region = make_region(
            short_name="Virginia",
            long_name="Virginia",
            slug="virginia",
            wikidata_item=items[0],
        )
        cls.child_region = make_region(
            short_name="Richmond",
            long_name="Richmond, Virginia",
            slug="richmond",
            wikidata_item=items[1],
        )
        cls.outside_region = make_region(
            short_name="Norfolk",
            long_name="Norfolk, Virginia",
            slug="norfolk",
            wikidata_item=items[2],
        )
        RegionAncestor.objects.create(
            region=cls.child_region, ancestor=cls.region.wikidata_item
        )

        source_region = Source.objects.create(
            name="Source region",
            slug="source-region",
            url="https://example.com/source-region",
            description="",
            region=cls.region,
        )
        source_region_collection = Collection.objects.create(
            source=source_region,
            name="Source inherited",
            slug="source-inherited",
        )
        cls.source_inherited = Image.objects.create(
            collection=source_region_collection,
            title="Source inherited",
            permalink="https://img.example.com/source-inherited.jpg",
            difficulty="easy",
        )

        collection_source = Source.objects.create(
            name="Collection source",
            slug="collection-source",
            url="https://example.com/collection-source",
            description="",
            region=cls.outside_region,
        )
        collection_region = Collection.objects.create(
            source=collection_source,
            name="Collection override",
            slug="collection-override",
            region=cls.region,
        )
        cls.collection_override = Image.objects.create(
            collection=collection_region,
            title="Collection override",
            permalink="https://img.example.com/collection-override.jpg",
            difficulty="medium",
        )

        image_source = Source.objects.create(
            name="Image source",
            slug="image-source",
            url="https://example.com/image-source",
            description="",
            region=cls.outside_region,
        )
        image_collection = Collection.objects.create(
            source=image_source,
            name="Image override",
            slug="image-override",
            region=cls.outside_region,
        )
        cls.image_override = Image.objects.create(
            collection=image_collection,
            title="Image override",
            permalink="https://img.example.com/image-override.jpg",
            difficulty="hard",
            region=cls.child_region,
        )

        cls.outside_source = Source.objects.create(
            name="Outside source",
            slug="outside-source",
            url="https://example.com/outside-source",
            description="",
            region=cls.outside_region,
        )
        cls.outside_collection = Collection.objects.create(
            source=cls.outside_source,
            name="Outside collection",
            slug="outside-collection",
        )
        cls.outside_image = Image.objects.create(
            collection=cls.outside_collection,
            title="Outside image",
            permalink="https://img.example.com/outside.jpg",
            difficulty="easy",
        )

        cls.owner = User.objects.create_user(username="album-owner")
        cls.album = Album.objects.create(
            owner=cls.owner, title="Outside album", public=True
        )
        cls.album.images.add(cls.outside_image)
        cls.subject = Subject.objects.create(
            title="Outside subject", slug="outside-subject", wikidata_item=items[3]
        )
        SubjectMapping.objects.create(
            image=cls.outside_image, subject=cls.subject
        )

        cls.url = reverse("images:georeference_interface")

    def setUp(self):
        self.client.cookies[REGION_COOKIE_NAME] = self.region.slug

    def assertOutsideQueue(self, params):
        response = self.client.get(self.url, params)
        self.assertEqual(response.context["current_image"], self.outside_image)
        self.assertEqual(response.context["remaining_count"], 1)

    def test_default_queue_uses_effective_region_and_descendants(self):
        response = self.client.get(self.url)

        self.assertIn(
            response.context["current_image"],
            {self.source_inherited, self.collection_override, self.image_override},
        )
        self.assertEqual(response.context["remaining_count"], 3)

    def test_difficulty_narrows_the_regional_queue(self):
        response = self.client.get(self.url, {"difficulty": "easy"})

        self.assertEqual(response.context["current_image"], self.source_inherited)
        self.assertEqual(response.context["remaining_count"], 1)

    def test_explicit_image_overrides_region_preference(self):
        response = self.client.get(self.url, {"image": self.outside_image.id})
        self.assertEqual(response.context["current_image"], self.outside_image)

    def test_explicit_source_overrides_region_preference(self):
        self.assertOutsideQueue({"source": self.outside_source.slug})

    def test_explicit_collection_overrides_region_preference(self):
        self.assertOutsideQueue(
            {
                "source": self.outside_source.slug,
                "collection": self.outside_collection.slug,
            }
        )

    def test_explicit_album_overrides_region_preference(self):
        self.assertOutsideQueue({"album": self.album.id})

    def test_explicit_subject_overrides_region_preference(self):
        self.assertOutsideQueue({"subject": self.subject.slug})


class StatsEventsMixin:
    """Run the on_commit stats refreshes that a write schedules.

    Refreshes run via transaction.on_commit, so writes have to be wrapped in
    captureOnCommitCallbacks (and the image-processing task that Image saves
    also enqueue on commit has to be muted). Shared by both stats suites so
    the pairing can't drift between them.
    """

    @contextmanager
    def stats_events(self):
        with (
            patch("images.tasks.process_image.apply_async"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            yield

    def img(self, title, **kwargs):
        with self.stats_events():
            return Image.objects.create(
                collection=kwargs.pop("collection", self.collection),
                title=title,
                permalink=f"https://img.example.com/{title}.jpg",
                **kwargs,
            )


class CollectionStatsTests(StatsEventsMixin, TestCase):
    """The denormalized CollectionStats rows stay correct as images and
    georeferences change."""

    @classmethod
    def setUpTestData(cls):
        cls.source = Source.objects.create(
            name="Src", slug="src", url="https://example.com", description=""
        )
        cls.collection = Collection.objects.create(
            source=cls.source, name="Col", slug="col", url="https://example.com"
        )

    def stats(self, collection=None):
        return CollectionStats.objects.get(pk=(collection or self.collection).pk)

    def counts(self, collection=None):
        s = self.stats(collection)
        return (
            s.total_images,
            s.georeferenced_images,
            s.will_not_georef_images,
            s.pending_images,
        )

    def test_new_collection_seeds_zeroed_row(self):
        with self.stats_events():
            collection = Collection.objects.create(
                source=self.source, name="New", slug="new", url="https://example.com"
            )
        self.assertEqual(self.counts(collection), (0, 0, 0, 0))

    def test_georeference_lifecycle(self):
        image = self.img("A")
        self.assertEqual(self.counts(), (1, 0, 0, 1))

        with self.stats_events():
            georef = Georeference.objects.create(
                image=image, point=Point(-77.43, 37.54, srid=4326), confidence="high"
            )
        self.assertEqual(self.counts(), (1, 1, 0, 0))
        self.assertEqual(self.stats().georeferenced_high, 1)

        with self.stats_events():
            georef.delete()
        self.assertEqual(self.counts(), (1, 0, 0, 1))

    def test_confidence_follows_most_recent_georeference(self):
        image = self.img("A")
        with self.stats_events():
            Georeference.objects.create(
                image=image, point=Point(-77.43, 37.54, srid=4326), confidence="low"
            )
        old = Georeference.objects.get(image=image)
        Georeference.objects.filter(pk=old.pk).update(
            georeferenced_at=timezone.now() - datetime.timedelta(days=1)
        )
        with self.stats_events():
            # Attributed, because only the first georeference on an image may
            # be anonymous (see the partial unique index on Georeference).
            Georeference.objects.create(
                image=image,
                point=Point(-77.44, 37.54, srid=4326),
                confidence="medium",
                georeferenced_by=User.objects.create_user("stats_corrector"),
            )
        s = self.stats()
        self.assertEqual(
            (s.georeferenced_low, s.georeferenced_medium, s.georeferenced_high),
            (0, 1, 0),
        )
        self.assertEqual(s.georeferenced_images, 1)

    def test_aerial_image_counts_only_via_aerial_georeference(self):
        aerial = self.img("Aerial", aerial=True)
        with self.stats_events():
            AerialGeoreference.objects.create(
                image=aerial,
                polygon=Polygon(
                    (
                        (-77.44, 37.53),
                        (-77.43, 37.53),
                        (-77.43, 37.54),
                        (-77.44, 37.54),
                        (-77.44, 37.53),
                    ),
                    srid=4326,
                ),
                confidence="medium",
            )
        self.assertEqual(self.counts(), (1, 1, 0, 0))

        # An aerial image with ONLY a point georeference is not "done" — it
        # still needs a polygon georeference, so it stays available (pending).
        aerial_pt = self.img("AerialPoint", aerial=True)
        with self.stats_events():
            Georeference.objects.create(
                image=aerial_pt,
                point=Point(-77.43, 37.54, srid=4326),
                confidence="low",
            )
        # total 2, only the polygon-georeferenced aerial is done, the other pends
        self.assertEqual(self.counts(), (2, 1, 0, 1))
        s = self.stats()
        self.assertEqual(s.georeferenced_images, 1)
        self.assertEqual(s.georeferenced_low, 0)

    def test_will_not_georef_and_duplicates(self):
        image = self.img("A")
        self.img("Dup", duplicate_of=image)
        self.assertEqual(self.counts(), (1, 0, 0, 1))

        image.will_not_georef = True
        with self.stats_events():
            image.save()
        self.assertEqual(self.counts(), (1, 0, 1, 0))

    def test_image_move_refreshes_both_collections(self):
        other = Collection.objects.create(
            source=self.source, name="Other", slug="other", url="https://example.com"
        )
        image = self.img("A")
        image.collection = other
        with self.stats_events():
            image.save()
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        self.assertEqual(self.counts(other), (1, 0, 0, 1))

    def test_image_delete_refreshes_stats(self):
        image = self.img("A")
        with self.stats_events():
            image.delete()
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_reconcile_task_heals_drift(self):
        self.img("A")
        CollectionStats.objects.filter(pk=self.collection.pk).update(
            total_images=99, georeferenced_high=42
        )
        reconcile_collection_stats()
        self.assertEqual(self.counts(), (1, 0, 0, 1))

    def test_refresh_skips_rows_computed_from_newer_snapshot(self):
        """The upsert's freshness guard: a refresh whose snapshot is older
        than the row's updated_at must leave the row untouched, so a stale
        concurrent refresh (or the reconcile) can't clobber fresher counts."""
        self.img("A")
        CollectionStats.objects.filter(pk=self.collection.pk).update(
            total_images=99,
            updated_at=timezone.now() + datetime.timedelta(hours=1),
        )
        CollectionStats.refresh_for([self.collection.pk])
        self.assertEqual(self.stats().total_images, 99)

    def test_overall_stats_and_confidence_breakdown(self):
        image = self.img("A")
        self.img("B")
        self.img("C", will_not_georef=True)
        with self.stats_events():
            Georeference.objects.create(
                image=image, point=Point(-77.43, 37.54, srid=4326), confidence="high"
            )
        # A private collection's images must not leak into sitewide numbers
        hidden = Collection.objects.create(
            source=self.source,
            name="Hidden",
            slug="hidden",
            url="https://example.com",
            public=False,
        )
        self.img("H", collection=hidden)

        overall = get_overall_stats()
        self.assertEqual(overall["total_images"], 2)  # excludes wnf + hidden
        self.assertEqual(overall["total_georeferenced"], 1)
        self.assertEqual(overall["georeferenced_percentage"], 50.0)

        breakdown = get_confidence_breakdown()
        self.assertEqual(
            breakdown,
            {"not_georeferenced": 1, "low": 0, "medium": 0, "high": 1},
        )


class CollectionRegionStatsTests(StatsEventsMixin, TestCase):
    """The per-(collection, region) rows stay correct as images, georeferences
    and region assignments change.

    Two regions: Richmond, whose P131 chain reaches Virginia, and Virginia
    itself. An image in Richmond must therefore count toward both.
    """

    @classmethod
    def setUpTestData(cls):
        # bulk_create bypasses WikidataItem.save(), which fetches live
        # Wikidata metadata; make_region supplies the coordinate the check
        # constraint wants.
        cls.city_item, cls.state_item = WikidataItem.objects.bulk_create(
            [
                WikidataItem(wikidata_id="Q43421", title="Richmond"),
                WikidataItem(wikidata_id="Q1370", title="Virginia"),
            ]
        )
        cls.city = make_region(
            short_name="Richmond",
            long_name="Richmond, Virginia",
            slug="richmond",
            wikidata_item=cls.city_item,
        )
        cls.state = make_region(
            short_name="Virginia",
            long_name="Virginia",
            slug="virginia",
            wikidata_item=cls.state_item,
        )
        # RegionAncestor.ancestor is a WikidataItem, not a Region: the table
        # records "Richmond is contained by the entity Q1370", and the refresh
        # joins back to Region through Region.wikidata_item.
        RegionAncestor.objects.create(region=cls.city, ancestor=cls.state_item)

        cls.source = Source.objects.create(
            name="Src", slug="src", url="https://example.com", description=""
        )
        cls.collection = Collection.objects.create(
            source=cls.source, name="Col", slug="col", url="https://example.com"
        )

    def rows(self, collection=None):
        """{region slug: total_images} for a collection."""
        return {
            row.region.slug: row.total_images
            for row in CollectionRegionStats.objects.filter(
                collection=collection or self.collection
            ).select_related("region")
        }

    def row(self, region, collection=None):
        return CollectionRegionStats.objects.get(
            collection=collection or self.collection, region=region
        )

    def counts(self, region, collection=None):
        r = self.row(region, collection)
        return (
            r.total_images,
            r.georeferenced_images,
            r.will_not_georef_images,
            r.pending_images,
        )

    def set_region(self, obj, region):
        with self.stats_events():
            obj.region = region
            obj.save()

    # -- region resolution -------------------------------------------------

    def test_image_with_no_region_anywhere_produces_no_rows(self):
        self.img("A")
        self.assertEqual(self.rows(), {})

    def test_region_inherited_from_source(self):
        self.set_region(self.source, self.city)
        self.img("A")
        self.assertEqual(self.rows(), {"richmond": 1, "virginia": 1})

    def test_collection_region_overrides_source(self):
        self.set_region(self.source, self.state)
        self.set_region(self.collection, self.city)
        self.img("A")
        # Resolution stops at the collection, so the source's region is only
        # reached through the rollup, never directly.
        self.assertEqual(self.rows(), {"richmond": 1, "virginia": 1})

    def test_image_region_overrides_collection(self):
        self.set_region(self.collection, self.state)
        self.img("A")
        self.img("B", region=self.city)
        # B lands in Richmond and rolls up; A sits directly in Virginia.
        self.assertEqual(self.rows(), {"richmond": 1, "virginia": 2})

    # -- P131 rollup -------------------------------------------------------

    def test_rollup_carries_every_bucket_to_the_ancestor(self):
        self.set_region(self.collection, self.city)
        image = self.img("A")
        self.img("B")
        self.img("C", will_not_georef=True)
        with self.stats_events():
            Georeference.objects.create(
                image=image, point=Point(-77.43, 37.54, srid=4326), confidence="medium"
            )

        for region in (self.city, self.state):
            self.assertEqual(self.counts(region), (3, 1, 1, 1))
            self.assertEqual(self.row(region).georeferenced_medium, 1)

    def test_two_children_do_not_double_count_in_their_shared_ancestor(self):
        norfolk_item = WikidataItem.objects.create(
            wikidata_id="Q49231", title="Norfolk"
        )
        norfolk = make_region(
            short_name="Norfolk",
            long_name="Norfolk, Virginia",
            slug="norfolk",
            wikidata_item=norfolk_item,
        )
        RegionAncestor.objects.create(region=norfolk, ancestor=self.state_item)

        self.set_region(self.collection, self.city)
        self.img("A")
        self.img("B", region=norfolk)

        # Each image reaches Virginia by exactly one path.
        self.assertEqual(
            self.rows(), {"richmond": 1, "norfolk": 1, "virginia": 2}
        )

    def test_reconcile_picks_up_a_new_ancestor_edge(self):
        RegionAncestor.objects.filter(region=self.city).delete()
        self.set_region(self.collection, self.city)
        self.img("A")
        self.assertEqual(self.rows(), {"richmond": 1})

        RegionAncestor.objects.create(region=self.city, ancestor=self.state_item)
        reconcile_collection_region_stats()

        self.assertEqual(self.rows(), {"richmond": 1, "virginia": 1})

    # -- invalidation ------------------------------------------------------

    def test_collection_region_change_zeroes_the_old_rows(self):
        norfolk_item = WikidataItem.objects.create(
            wikidata_id="Q49231", title="Norfolk"
        )
        norfolk = make_region(
            short_name="Norfolk", long_name="Norfolk", slug="norfolk",
            wikidata_item=norfolk_item,
        )
        self.set_region(self.collection, self.city)
        self.img("A")
        self.assertEqual(self.rows(), {"richmond": 1, "virginia": 1})

        self.set_region(self.collection, norfolk)

        # Richmond and Virginia are emptied rather than deleted; the reconcile
        # collects them once they've aged out.
        self.assertEqual(
            self.rows(), {"richmond": 0, "virginia": 0, "norfolk": 1}
        )

    def test_source_region_change_leaves_overriding_collections_alone(self):
        overriding = Collection.objects.create(
            source=self.source, name="Own", slug="own", url="https://example.com"
        )
        self.set_region(overriding, self.city)
        self.set_region(self.source, self.state)
        self.img("A")
        self.img("B", collection=overriding)
        self.assertEqual(self.rows(), {"virginia": 1})
        self.assertEqual(self.rows(overriding), {"richmond": 1, "virginia": 1})

        # The inheriting collection follows the source; the overriding one
        # never resolved to the source's region in the first place.
        norfolk_item = WikidataItem.objects.create(
            wikidata_id="Q49231", title="Norfolk"
        )
        norfolk = make_region(
            short_name="Norfolk", long_name="Norfolk", slug="norfolk",
            wikidata_item=norfolk_item,
        )
        self.set_region(self.source, norfolk)

        self.assertEqual(self.rows(), {"virginia": 0, "norfolk": 1})
        self.assertEqual(self.rows(overriding), {"richmond": 1, "virginia": 1})

    def test_image_move_refreshes_both_collections(self):
        other = Collection.objects.create(
            source=self.source, name="Other", slug="other", url="https://example.com"
        )
        self.set_region(self.collection, self.city)
        self.set_region(other, self.state)
        image = self.img("A")

        with self.stats_events():
            image.collection = other
            image.save()

        self.assertEqual(self.rows(), {"richmond": 0, "virginia": 0})
        self.assertEqual(self.rows(other), {"virginia": 1})

    def test_deleting_the_last_image_empties_every_row(self):
        self.set_region(self.collection, self.city)
        image = self.img("A")
        self.assertEqual(self.rows(), {"richmond": 1, "virginia": 1})

        with self.stats_events():
            image.delete()

        self.assertEqual(self.rows(), {"richmond": 0, "virginia": 0})

    def test_will_not_georef_and_duplicates(self):
        self.set_region(self.collection, self.city)
        original = self.img("A")
        self.img("Dup", duplicate_of=original)
        self.assertEqual(self.counts(self.city), (1, 0, 0, 1))

        with self.stats_events():
            original.will_not_georef = True
            original.save()

        self.assertEqual(self.counts(self.city), (1, 0, 1, 0))

    def test_in_region_selects_the_rows_the_refresh_counted(self):
        # ImageQuerySet.in_region has to apply the same eligibility rule as
        # REFRESH_SQL's image_rows join, all the way up the rollup, or a page
        # that filters images by region disagrees with the counts published
        # for that region.
        self.set_region(self.collection, self.city)
        original = self.img("A")
        duplicate = self.img("Dup", duplicate_of=original)

        for region in (self.city, self.state):
            with self.subTest(region=region.slug):
                in_region = Image.objects.in_region(region)
                self.assertNotIn(duplicate, in_region)
                self.assertEqual(in_region.count(), self.row(region).total_images)

    def test_aerial_image_counts_only_via_aerial_georeference(self):
        self.set_region(self.collection, self.city)
        aerial = self.img("Aerial", aerial=True)
        with self.stats_events():
            Georeference.objects.create(
                image=aerial, point=Point(-77.43, 37.54, srid=4326), confidence="high"
            )
        # A point georeference on an aerial leaves it available.
        self.assertEqual(self.counts(self.city), (1, 0, 0, 1))

        with self.stats_events():
            AerialGeoreference.objects.create(
                image=aerial,
                polygon=Polygon(
                    (
                        (-77.5, 37.5),
                        (-77.4, 37.5),
                        (-77.4, 37.6),
                        (-77.5, 37.6),
                        (-77.5, 37.5),
                    ),
                    srid=4326,
                ),
                confidence="low",
            )
        self.assertEqual(self.counts(self.city), (1, 1, 0, 0))
        self.assertEqual(self.row(self.state).georeferenced_low, 1)

    def test_region_create_queues_a_reconcile(self):
        with (
            patch(
                "images.signals.reconcile_collection_region_stats.delay"
            ) as reconcile,
            self.captureOnCommitCallbacks(execute=True),
        ):
            item = WikidataItem.objects.create(wikidata_id="Q49231", title="Norfolk")
            make_region(
                short_name="Norfolk", long_name="Norfolk", slug="norfolk",
                wikidata_item=item,
            )
        reconcile.assert_called_once()

    # -- self-healing ------------------------------------------------------

    def test_reconcile_heals_drift_in_both_directions(self):
        self.set_region(self.collection, self.city)
        self.img("A")
        # Undercounted a live pair...
        CollectionRegionStats.objects.filter(region=self.city).update(total_images=99)
        # ...and a row for a pair that has no images at all
        empty = Collection.objects.create(
            source=self.source, name="Empty", slug="empty", url="https://example.com"
        )
        CollectionRegionStats.objects.create(
            collection=empty, region=self.city, total_images=42
        )

        reconcile_collection_region_stats()

        self.assertEqual(self.rows(), {"richmond": 1, "virginia": 1})
        # Zeroed rather than deleted; it ages out on a later reconcile
        self.assertEqual(self.rows(empty), {"richmond": 0})

    def test_reconcile_collects_rows_that_have_aged_out_at_zero(self):
        self.set_region(self.collection, self.city)
        image = self.img("A")
        with self.stats_events():
            image.delete()
        self.assertEqual(self.rows(), {"richmond": 0, "virginia": 0})

        CollectionRegionStats.objects.update(
            updated_at=timezone.now() - datetime.timedelta(days=2)
        )
        reconcile_collection_region_stats()

        self.assertEqual(self.rows(), {})

    def test_refresh_skips_rows_computed_from_newer_snapshot(self):
        self.set_region(self.collection, self.city)
        self.img("A")
        CollectionRegionStats.objects.filter(region=self.city).update(
            total_images=99, updated_at=timezone.now() + datetime.timedelta(hours=1)
        )

        CollectionRegionStats.refresh_for([self.collection.pk])

        self.assertEqual(self.row(self.city).total_images, 99)

    def test_freshness_guard_also_covers_the_emptying_arm(self):
        self.set_region(self.collection, self.city)
        image = self.img("A")
        with self.stats_events():
            # Delete the image but pin the rows into the future, as a refresh
            # that started later would leave them
            Image.objects.filter(pk=image.pk).delete()
        CollectionRegionStats.objects.update(
            total_images=99, updated_at=timezone.now() + datetime.timedelta(hours=1)
        )

        CollectionRegionStats.refresh_for([self.collection.pk])

        self.assertEqual(self.rows(), {"richmond": 99, "virginia": 99})


class SourceDetailRegionOrderingTests(StatsEventsMixin, TestCase):
    """Source collections and their statistics follow the selected region."""

    @classmethod
    def setUpTestData(cls):
        item = WikidataItem.objects.bulk_create(
            [WikidataItem(wikidata_id="Q43421", title="Richmond")]
        )[0]
        cls.region = make_region(
            short_name="Richmond",
            long_name="Richmond, Virginia",
            slug="richmond",
            wikidata_item=item,
        )
        cls.source = Source.objects.create(
            name="Src", slug="src", url="https://example.com", description=""
        )
        # Alphabetical order is Big, Small; region counts reverse that.
        cls.big = Collection.objects.create(
            source=cls.source, name="Big", slug="big", url="https://example.com"
        )
        cls.small = Collection.objects.create(
            source=cls.source,
            name="Small",
            slug="small",
            url="https://example.com",
            region=cls.region,
        )
        cls.collection = cls.big  # the mixin's default for img()

    def setUp(self):
        for n in range(3):
            self.img(f"big{n}", collection=self.big)
        self.img("small0", collection=self.small)

    def names(self, response):
        return [collection.name for collection in response.context["collections"]]

    def test_default_order_without_a_region_selected(self):
        response = self.client.get(self.source.get_absolute_url())
        self.assertEqual(self.names(response), ["Big", "Small"])

    def test_selected_region_only_shows_matching_collections(self):
        self.client.cookies[REGION_COOKIE_NAME] = "richmond"
        response = self.client.get(self.source.get_absolute_url())
        self.assertEqual(self.names(response), ["Small"])
        self.assertEqual(response.context["collection_count"], 1)
        self.assertEqual(response.context["total_images"], 1)
        self.assertContains(
            response, "Showing 1 of 2 collections with images in Richmond"
        )

    def test_card_numbers_and_order_use_regional_images(self):
        self.img("big-city", collection=self.big, region=self.region)
        self.img("small-city", collection=self.small)
        self.client.cookies[REGION_COOKIE_NAME] = "richmond"
        response = self.client.get(self.source.get_absolute_url())
        self.assertEqual(self.names(response), ["Small", "Big"])
        by_name = {c.name: c for c in response.context["collections"]}
        self.assertEqual(by_name["Big"].total_images, 1)
        self.assertEqual(by_name["Small"].total_images, 2)
        self.assertEqual(response.context["total_images"], 3)
        self.assertEqual(response.context["pending_images"], 3)
        self.assertIn(response.context["top_rated_image"].pk,
                      Image.objects.in_region(self.region).values_list("pk", flat=True))
        self.assertContains(response, "&region=Q43421")
        self.assertNotContains(response, "Showing 2 of 2 collections")

    def test_source_falls_back_when_no_public_collection_matches(self):
        self.small.public = False
        self.small.save()
        self.client.cookies[REGION_COOKIE_NAME] = "richmond"
        response = self.client.get(self.source.get_absolute_url())
        self.assertEqual(self.names(response), ["Big"])
        self.assertEqual(response.context["total_images"], 3)
        self.assertTrue(response.context["region_fallback"])
        self.assertIsNone(response.context["browse_region"])
        self.assertEqual(response.context["current_region"], self.region)
        self.assertContains(response, "Src has no images in your selected region. Showing all images from Src.")
        self.assertNotContains(response, "&region=Q43421")

    def test_collection_count_still_renders(self):
        """`collections` is a list, so a template calling .count on it would
        silently render an empty stat card rather than raising."""
        response = self.client.get(self.source.get_absolute_url())
        self.assertEqual(response.context["collection_count"], 2)
        self.assertNotContains(response, '<div class="h4 mb-0"></div>')


class CollectionDetailRegionFilterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        items = WikidataItem.objects.bulk_create(
            [
                WikidataItem(wikidata_id="Q43421", title="Richmond"),
                WikidataItem(wikidata_id="Q1370", title="Virginia"),
                WikidataItem(wikidata_id="Q1391", title="Maryland"),
                WikidataItem(wikidata_id="Q30", title="United States"),
                WikidataItem(wikidata_id="Q61", title="Washington, D.C."),
            ]
        )
        cls.city = make_region(
            short_name="Richmond",
            long_name="Richmond, Virginia",
            slug="richmond",
            wikidata_item=items[0],
        )
        cls.state = make_region(
            short_name="Virginia",
            long_name="Virginia",
            slug="virginia",
            wikidata_item=items[1],
        )
        cls.other = make_region(
            short_name="Maryland",
            long_name="Maryland",
            slug="maryland",
            wikidata_item=items[2],
        )
        cls.country = make_region(
            short_name="United States",
            long_name="United States",
            slug="united-states",
            wikidata_item=items[3],
        )
        cls.empty = make_region(
            short_name="Washington, D.C.",
            long_name="Washington, D.C.",
            slug="washington-dc",
            wikidata_item=items[4],
        )
        RegionAncestor.objects.bulk_create(
            [
                RegionAncestor(region=cls.city, ancestor=items[1]),
                RegionAncestor(region=cls.city, ancestor=items[3]),
                RegionAncestor(region=cls.state, ancestor=items[3]),
                RegionAncestor(region=cls.other, ancestor=items[3]),
            ]
        )

        cls.source = Source.objects.create(
            name="Src", slug="src", url="https://example.com", description=""
        )
        cls.collection = Collection.objects.create(
            source=cls.source,
            name="Collection",
            slug="collection",
            url="https://example.com/collection",
        )
        cls.city_image, cls.state_image, cls.other_image = Image.objects.bulk_create(
            [
                Image(
                    collection=cls.collection,
                    title="City image",
                    permalink="https://img.example.com/city.jpg",
                    region=cls.city,
                ),
                Image(
                    collection=cls.collection,
                    title="State image",
                    permalink="https://img.example.com/state.jpg",
                    region=cls.state,
                ),
                Image(
                    collection=cls.collection,
                    title="Other image",
                    permalink="https://img.example.com/other.jpg",
                    region=cls.other,
                ),
            ]
        )
        CollectionStats.refresh_for([cls.collection.pk])
        CollectionRegionStats.refresh_for([cls.collection.pk])

    def page_image_ids(self, response):
        return {image.id for image in response.context["page_obj"]}

    def refresh_stats(self):
        CollectionStats.refresh_for([self.collection.pk])
        CollectionRegionStats.refresh_for([self.collection.pk])

    def test_selected_region_filters_images_counts_and_preview(self):
        self.client.cookies[REGION_COOKIE_NAME] = self.state.slug
        response = self.client.get(self.collection.get_absolute_url())
        expected = {self.city_image.pk, self.state_image.pk}
        self.assertEqual(self.page_image_ids(response), expected)
        self.assertEqual(response.context["total_images"], 2)
        self.assertEqual(response.context["pending_images"], 2)
        self.assertIn(response.context["top_rated_image"].pk, expected)
        self.assertFalse(response.context["region_fallback"])
        self.assertContains(response, "&amp;region=Q1370")
        self.assertContains(response, "Showing 2 of 3 images in Virginia")

    def test_city_does_not_include_images_assigned_to_its_ancestor(self):
        self.client.cookies[REGION_COOKIE_NAME] = self.city.slug
        response = self.client.get(self.collection.get_absolute_url())
        self.assertEqual(self.page_image_ids(response), {self.city_image.pk})

    def test_global_and_stale_cookie_keep_all_images(self):
        for slug in ("", "deleted-region"):
            with self.subTest(slug=slug):
                self.client.cookies[REGION_COOKIE_NAME] = slug
                response = self.client.get(self.collection.get_absolute_url())
                self.assertEqual(self.page_image_ids(response),
                                 {self.city_image.pk, self.state_image.pk, self.other_image.pk})
                self.assertEqual(response.context["total_images"], 3)
                self.assertFalse(response.context["region_fallback"])
                self.assertNotContains(response, "Showing 3 of 3 images")

    def test_no_regional_images_falls_back_without_changing_selection(self):
        self.client.cookies[REGION_COOKIE_NAME] = self.empty.slug
        response = self.client.get(self.collection.get_absolute_url())
        self.assertEqual(len(self.page_image_ids(response)), 3)
        self.assertEqual(response.context["total_images"], 3)
        self.assertTrue(response.context["region_fallback"])
        self.assertIsNone(response.context["browse_region"])
        self.assertEqual(response.context["current_region"], self.empty)
        self.assertNotIn("region=", response.context["georeference_url"])
        self.assertContains(response, "Collection has no images in your selected region. Showing all images from Collection.")

    def test_empty_collection_keeps_empty_state_and_banner(self):
        self.collection.images.all().delete()
        self.refresh_stats()
        self.client.cookies[REGION_COOKIE_NAME] = self.city.slug
        response = self.client.get(self.collection.get_absolute_url())
        self.assertEqual(response.context["total_images"], 0)
        self.assertEqual(self.page_image_ids(response), set())
        self.assertContains(response, "has no images in your selected region")
        self.assertContains(response, "No Images")

    def test_grid_filters_do_not_broaden_or_change_header_counts(self):
        self.client.cookies[REGION_COOKIE_NAME] = self.state.slug
        for params in ({"start_year": "1900"}, {"georeference_status": "georeferenced"},
                       {"with_subjects": "999999"}):
            with self.subTest(params=params):
                response = self.client.get(self.collection.get_absolute_url(), params)
                self.assertEqual(self.page_image_ids(response), set())
                self.assertEqual(response.context["total_images"], 2)
                self.assertFalse(response.context["region_fallback"])

    def test_inheritance_overrides_duplicates_and_unassigned_images(self):
        self.source.region = self.city
        self.source.save()
        inherited, duplicate = Image.objects.bulk_create([
            Image(collection=self.collection, title="Inherited", permalink="https://example.com/inherited"),
            Image(collection=self.collection, title="Duplicate", permalink="https://example.com/duplicate",
                  duplicate_of=self.city_image),
        ])
        self.refresh_stats()
        self.client.cookies[REGION_COOKIE_NAME] = self.city.slug
        response = self.client.get(self.collection.get_absolute_url())
        self.assertEqual(self.page_image_ids(response), {self.city_image.pk, inherited.pk})
        self.assertEqual(response.context["total_images"], 2)
        self.collection.region = self.other
        self.collection.save()
        self.refresh_stats()
        response = self.client.get(self.collection.get_absolute_url())
        self.assertEqual(self.page_image_ids(response), {self.city_image.pk})
        self.source.region = None
        self.source.save()
        self.collection.region = None
        self.collection.save()
        self.refresh_stats()
        response = self.client.get(self.collection.get_absolute_url())
        self.assertEqual(self.page_image_ids(response), {self.city_image.pk})

    def test_statistics_progress_and_map_use_the_same_region(self):
        Georeference.objects.create(image=self.city_image, point=Point(-77.43, 37.54), confidence="high")
        Image.objects.filter(pk=self.state_image.pk).update(will_not_georef=True)
        self.refresh_stats()
        self.client.cookies[REGION_COOKIE_NAME] = self.state.slug
        response = self.client.get(self.collection.get_absolute_url())
        self.assertEqual(response.context["total_images"], 2)
        self.assertEqual(response.context["georeferenced_images"], 1)
        self.assertEqual(response.context["will_not_georef_images"], 1)
        self.assertEqual(response.context["pending_images"], 0)
        self.assertEqual(response.context["completion_percentage"], 100)
        self.assertContains(response, "urlParams.append('region', 'Q1370')")
        self.assertNotContains(response, "Start Georeferencing")
        self.client.cookies[REGION_COOKIE_NAME] = self.empty.slug
        response = self.client.get(self.collection.get_absolute_url())
        self.assertEqual(response.context["total_images"], 3)
        self.assertNotContains(response, "urlParams.append('region'")

    def test_pagination_stays_in_region(self):
        Image.objects.bulk_create([
            Image(collection=self.collection, title=f"Additional state image {index}",
                  permalink=f"https://img.example.com/state-{index}.jpg", region=self.state)
            for index in range(23)
        ])
        self.refresh_stats()
        self.client.cookies[REGION_COOKIE_NAME] = self.state.slug
        first = self.client.get(self.collection.get_absolute_url())
        second = self.client.get(self.collection.get_absolute_url(), {"page": 2})
        self.assertTrue(first.context["page_obj"].has_next())
        self.assertEqual(second.context["page_obj"].paginator.count, 25)
        self.assertNotIn(self.other_image.pk, self.page_image_ids(first) | self.page_image_ids(second))

    def test_regional_tiles_filter_by_inherited_region_and_ancestry(self):
        # Keep all points identical: only the metadata region may distinguish them.
        self.source.region = self.city
        self.source.save()
        Image.objects.filter(pk=self.city_image.pk).update(region=None)
        for image in (self.city_image, self.state_image, self.other_image):
            Georeference.objects.create(image=image, point=Point(-77.43, 37.54), confidence="high")
        with connection.cursor() as cursor:
            cursor.execute("REFRESH MATERIALIZED VIEW public_georeferences_mvt")
        url = reverse("images:vector_tiles", kwargs={"v": 1, "z": 0, "x": 0, "y": 0})
        params = {"collection": self.collection.pk}
        global_tile = self.client.get(url, params)
        city_tile = self.client.get(url, {**params, "region": "Q43421"})
        image_tile = self.client.get(url, {**params, "image": self.city_image.pk})
        state_tile = self.client.get(url, {**params, "region": "Q1370"})
        empty_tile = self.client.get(url, {**params, "region": "Q61"})
        self.assertEqual(city_tile.status_code, 200)
        self.assertTrue(city_tile.content)
        self.assertEqual(city_tile.content, image_tile.content)
        self.assertNotEqual(state_tile.content, city_tile.content)
        self.assertNotEqual(state_tile.content, global_tile.content)
        self.assertEqual(empty_tile.content, b"")
        self.assertEqual(city_tile["Cache-Control"], "public, max-age=0, s-maxage=300")
        self.client.cookies[REGION_COOKIE_NAME] = self.city.slug
        self.assertEqual(self.client.get(url, params).content, global_tile.content)
        self.assertEqual(self.client.get(url, {"region": "deleted-region"}).status_code, 404)

    def test_georeference_queue_combines_region_collection_and_difficulty(self):
        Image.objects.filter(pk=self.city_image.pk).update(difficulty="easy")
        url = reverse("images:georeference_interface")
        params = {"source": self.source.slug, "collection": self.collection.slug,
                  "region": "Q1370", "difficulty": "easy"}
        response = self.client.get(url, params)
        self.assertEqual(response.context["current_image"], self.city_image)
        self.assertEqual(response.context["remaining_count"], 1)
        response = self.client.get(url, {**params, "region": "Q61"})
        self.assertIsNone(response.context["current_image"])
        self.assertEqual(response.context["remaining_count"], 0)
        self.assertEqual(self.client.get(url, {**params, "region": "deleted-region"}).status_code, 404)
        response = self.client.get(url, {**params, "image": self.other_image.pk})
        self.assertEqual(response.context["current_image"], self.other_image)


class SourceBrowseRegionFilteringTests(StatsEventsMixin, TestCase):
    """The source browse page only lists sources with images in its region."""

    @classmethod
    def setUpTestData(cls):
        items = WikidataItem.objects.bulk_create(
            [
                WikidataItem(wikidata_id="Q43421", title="Richmond"),
                WikidataItem(wikidata_id="Q1370", title="Virginia"),
                WikidataItem(wikidata_id="Q49231", title="Norfolk"),
            ]
        )
        cls.city = make_region(
            short_name="Richmond",
            long_name="Richmond, Virginia",
            slug="richmond",
            wikidata_item=items[0],
        )
        cls.state = make_region(
            short_name="Virginia",
            long_name="Virginia",
            slug="virginia",
            wikidata_item=items[1],
        )
        cls.other = make_region(
            short_name="Norfolk",
            long_name="Norfolk, Virginia",
            slug="norfolk",
            wikidata_item=items[2],
        )
        RegionAncestor.objects.create(region=cls.city, ancestor=items[1])

        cls.city_collection = cls.collection("City", cls.city)
        cls.state_collection = cls.collection("State", cls.state)
        cls.other_collection = cls.collection("Other", cls.other)
        cls.empty_source = Source.objects.create(
            name="Empty", slug="empty", url="https://example.com", description=""
        )

    @classmethod
    def collection(cls, name, region):
        source = Source.objects.create(
            name=name, slug=name.lower(), url="https://example.com", description=""
        )
        return Collection.objects.create(
            source=source,
            name=name,
            slug=name.lower(),
            url="https://example.com",
            region=region,
        )

    def setUp(self):
        for collection in (
            self.city_collection,
            self.state_collection,
            self.other_collection,
        ):
            self.img(collection.name, collection=collection)

    def source_slugs(self):
        response = self.client.get(reverse("images:browse_sources"))
        self.assertEqual(response.status_code, 200)
        return {source.slug for source in response.context["sources"]}

    def test_selected_region_shows_only_sources_with_matching_images(self):
        self.client.cookies[REGION_COOKIE_NAME] = self.city.slug
        self.assertEqual(self.source_slugs(), {"city"})

    def test_selected_ancestor_region_includes_descendant_images(self):
        self.client.cookies[REGION_COOKIE_NAME] = self.state.slug
        self.assertEqual(self.source_slugs(), {"city", "state"})

    def test_without_a_selected_region_keeps_the_sitewide_source_list(self):
        self.assertEqual(self.source_slugs(), {"city", "empty", "other", "state"})

    def test_source_cards_and_overall_stats_are_regional(self):
        self.img("Outside override", collection=self.city_collection, region=self.other)
        self.img("Skipped", collection=self.city_collection, will_not_georef=True)
        self.img("Duplicate", collection=self.city_collection,
                 duplicate_of=self.city_collection.images.first())
        georeferenced = self.img("Georeferenced", collection=self.city_collection)
        with self.stats_events():
            Georeference.objects.create(image=georeferenced, point=Point(-77.43, 37.54), confidence="high")
        Collection.objects.create(source=self.city_collection.source, name="Empty collection", slug="empty")
        self.client.cookies[REGION_COOKIE_NAME] = self.city.slug
        response = self.client.get(reverse("images:browse_sources"))
        source, = response.context["sources"]
        self.assertEqual(source.public_collections_count, 1)
        self.assertEqual(source.total_images, 3)
        self.assertEqual(source.georeferenced_images, 1)
        self.assertEqual(source.will_not_georef_images, 1)
        self.assertEqual(source.pending_images, 1)
        overall = response.context["overall_stats"]
        self.assertEqual(overall["total_sources"], 1)
        self.assertEqual(overall["total_collections"], 1)
        self.assertEqual(overall["total_images"], 2)
        self.assertEqual(overall["total_georeferenced"], 1)
        self.assertContains(response, "Showing 1 of 4 sources with images in Richmond")
        self.assertIn(response.context["top_rated_image"].pk,
                      Image.objects.in_region(self.city).values_list("pk", flat=True))
        self.assertContains(response, "&region=Q43421")
        detail = self.client.get(self.city_collection.source.get_absolute_url())
        self.assertEqual(detail.context["total_images"], 3)
        self.assertContains(detail, "urlParams.append('region', 'Q43421')")

    def test_private_and_zeroed_collections_do_not_create_matching_sources(self):
        self.city_collection.public = False
        self.city_collection.save()
        self.img("Private", collection=self.other_collection, region=self.city)
        self.other_collection.source.public = False
        self.other_collection.source.save()
        CollectionRegionStats.objects.create(collection=self.state_collection, region=self.city, total_images=0)
        self.client.cookies[REGION_COOKIE_NAME] = self.city.slug
        response = self.client.get(reverse("images:browse_sources"))
        self.assertEqual(list(response.context["sources"]), [])
        self.assertEqual(response.context["overall_stats"]["total_images"], 0)
        self.assertContains(response, "There are no image sources with images in")

    def test_empty_source_falls_back_and_keeps_empty_state(self):
        self.client.cookies[REGION_COOKIE_NAME] = self.city.slug
        response = self.client.get(self.empty_source.get_absolute_url())
        self.assertTrue(response.context["region_fallback"])
        self.assertEqual(response.context["total_images"], 0)
        self.assertContains(response, "Empty has no images in your selected region.")
        self.assertContains(response, "No Collections")

    def test_stale_cookie_keeps_global_counts_and_list(self):
        self.client.cookies[REGION_COOKIE_NAME] = "deleted-region"
        response = self.client.get(reverse("images:browse_sources"))
        self.assertEqual(len(response.context["sources"]), 4)
        self.assertEqual(response.context["overall_stats"]["total_images"], 3)



class FavoritesRegionTests(StatsEventsMixin, TestCase):
    """The favorites page scopes its listing to the navbar region, rolling
    descendant regions up into their ancestors, and composes with the
    existing source/collection filters.

    Ratings are unnecessary: the backing SQL view carries every public
    image, unrated ones sorting last, so membership alone is under test.
    """

    @classmethod
    def setUpTestData(cls):
        cls.city_item, cls.state_item = WikidataItem.objects.bulk_create(
            [
                WikidataItem(wikidata_id="Q43421", title="Richmond"),
                WikidataItem(wikidata_id="Q1370", title="Virginia"),
            ]
        )
        cls.city = make_region(
            short_name="Richmond",
            long_name="Richmond, Virginia",
            slug="richmond",
            wikidata_item=cls.city_item,
        )
        cls.state = make_region(
            short_name="Virginia",
            long_name="Virginia",
            slug="virginia",
            wikidata_item=cls.state_item,
        )
        RegionAncestor.objects.create(region=cls.city, ancestor=cls.state_item)

        cls.source = Source.objects.create(
            name="Src", slug="src", url="https://example.com", description=""
        )
        cls.city_coll = Collection.objects.create(
            source=cls.source,
            name="City",
            slug="city",
            url="https://example.com",
            region=cls.city,
        )
        cls.state_coll = Collection.objects.create(
            source=cls.source,
            name="State",
            slug="state",
            url="https://example.com",
            region=cls.state,
        )
        cls.plain_coll = Collection.objects.create(
            source=cls.source, name="Plain", slug="plain", url="https://example.com"
        )
        cls.collection = cls.plain_coll  # the mixin's default for img()

    def setUp(self):
        self.city_image = self.img("city0", collection=self.city_coll)
        self.state_image = self.img("state0", collection=self.state_coll)
        self.plain_image = self.img("plain0", collection=self.plain_coll)

    def favorites(self, **params):
        return self.client.get(reverse("images:favorites"), params)

    def ids(self, response):
        return {image.id for image in response.context["page_obj"]}

    def test_no_region_shows_everything(self):
        response = self.favorites()
        self.assertEqual(
            self.ids(response),
            {self.city_image.id, self.state_image.id, self.plain_image.id},
        )

    def test_selected_region_rolls_up_descendants(self):
        self.client.cookies[REGION_COOKIE_NAME] = "virginia"
        response = self.favorites()
        self.assertEqual(
            self.ids(response), {self.city_image.id, self.state_image.id}
        )

    def test_child_region_excludes_its_ancestors(self):
        self.client.cookies[REGION_COOKIE_NAME] = "richmond"
        response = self.favorites()
        self.assertEqual(self.ids(response), {self.city_image.id})

    def test_stale_cookie_falls_back_to_global(self):
        self.client.cookies[REGION_COOKIE_NAME] = "atlantis"
        response = self.favorites()
        self.assertEqual(len(self.ids(response)), 3)

    def test_image_level_region_override_is_respected(self):
        override = self.img("override0", collection=self.plain_coll, region=self.city)
        self.client.cookies[REGION_COOKIE_NAME] = "richmond"
        response = self.favorites()
        self.assertEqual(self.ids(response), {self.city_image.id, override.id})

    def test_region_inherited_from_source(self):
        src = Source.objects.create(
            name="Src2",
            slug="src2",
            url="https://example.com",
            description="",
            region=self.state,
        )
        coll = Collection.objects.create(
            source=src, name="Col2", slug="col2", url="https://example.com"
        )
        inherited = self.img("inherited0", collection=coll)
        self.client.cookies[REGION_COOKIE_NAME] = "virginia"
        response = self.favorites()
        self.assertEqual(
            self.ids(response),
            {self.city_image.id, self.state_image.id, inherited.id},
        )

    def test_region_composes_with_collection_filter(self):
        self.client.cookies[REGION_COOKIE_NAME] = "virginia"
        response = self.favorites(collection=self.city_coll.id)
        self.assertEqual(self.ids(response), {self.city_image.id})

        # AND semantics: the unregioned collection has nothing in Virginia.
        response = self.favorites(collection=self.plain_coll.id)
        self.assertEqual(self.ids(response), set())


class FromAboveRegionTests(TestCase):
    """The aerial card grid follows the navbar region except after a map click."""

    @classmethod
    def setUpTestData(cls):
        city_item, other_item = WikidataItem.objects.bulk_create(
            [
                WikidataItem(wikidata_id="Q43421", title="Richmond"),
                WikidataItem(wikidata_id="Q49231", title="Norfolk"),
            ]
        )
        cls.city = make_region(
            short_name="Richmond",
            long_name="Richmond, Virginia",
            slug="richmond",
            wikidata_item=city_item,
        )
        cls.other = make_region(
            short_name="Norfolk",
            long_name="Norfolk, Virginia",
            slug="norfolk",
            wikidata_item=other_item,
        )
        cls.source = Source.objects.create(
            name="Src", slug="src", url="https://example.com", description=""
        )
        cls.city_collection = Collection.objects.create(
            source=cls.source,
            name="City",
            slug="city",
            url="https://example.com",
            region=cls.city,
        )
        cls.other_collection = Collection.objects.create(
            source=cls.source,
            name="Other",
            slug="other",
            url="https://example.com",
            region=cls.other,
        )
        cls.city_aerial = Image.objects.create(
            collection=cls.city_collection,
            title="City aerial",
            permalink="https://img.example.com/city.jpg",
            aerial=True,
        )
        cls.other_aerial = Image.objects.create(
            collection=cls.other_collection,
            title="Other aerial",
            permalink="https://img.example.com/other.jpg",
            aerial=True,
        )
        AerialGeoreference.objects.create(
            image=cls.other_aerial,
            polygon=Polygon(
                ((-77.5, 37.5), (-77.4, 37.5), (-77.4, 37.6), (-77.5, 37.5)),
                srid=4326,
            ),
            confidence="high",
        )

    def aerial_ids(self, **params):
        response = self.client.get(reverse("images:browse_aerials"), params)
        self.assertEqual(response.status_code, 200)
        return {image.id for image in response.context["page_obj"]}

    def test_default_grid_is_scoped_to_the_selected_region(self):
        self.client.cookies[REGION_COOKIE_NAME] = self.city.slug
        self.assertEqual(self.aerial_ids(), {self.city_aerial.id})

    def test_map_click_results_ignore_the_selected_region(self):
        self.client.cookies[REGION_COOKIE_NAME] = self.city.slug
        self.assertEqual(
            self.aerial_ids(lat="37.55", lon="-77.45"), {self.other_aerial.id}
        )


class StatsPageRegionTests(StatsEventsMixin, TestCase):
    """The stats page follows the navbar region: tiles, both charts and the
    contributor table all scope to the region and its descendants, and fall
    back to sitewide when no (valid) region is selected.

    Richmond sits inside Virginia; Texas is unrelated. Each region's
    collection holds one georeferenced image plus one that isn't, and a
    private Richmond collection carries a georeference that must never
    show, in any scope.
    """

    @classmethod
    def setUpTestData(cls):
        cls.city_item, cls.state_item, cls.other_item = (
            WikidataItem.objects.bulk_create(
                [
                    WikidataItem(wikidata_id="Q43421", title="Richmond"),
                    WikidataItem(wikidata_id="Q1370", title="Virginia"),
                    WikidataItem(wikidata_id="Q1439", title="Texas"),
                ]
            )
        )
        cls.city = make_region(
            short_name="Richmond",
            long_name="Richmond, Virginia",
            slug="richmond",
            wikidata_item=cls.city_item,
        )
        cls.state = make_region(
            short_name="Virginia",
            long_name="Virginia",
            slug="virginia",
            wikidata_item=cls.state_item,
        )
        cls.other = make_region(
            short_name="Texas",
            long_name="Texas",
            slug="texas",
            wikidata_item=cls.other_item,
        )
        RegionAncestor.objects.create(region=cls.city, ancestor=cls.state_item)

        cls.source = Source.objects.create(
            name="Src", slug="src", url="https://example.com", description=""
        )
        cls.city_coll = Collection.objects.create(
            source=cls.source,
            name="City",
            slug="city",
            url="https://example.com",
            region=cls.city,
        )
        cls.other_coll = Collection.objects.create(
            source=cls.source,
            name="Other",
            slug="other",
            url="https://example.com",
            region=cls.other,
        )
        cls.hidden_coll = Collection.objects.create(
            source=cls.source,
            name="Hidden",
            slug="hidden",
            url="https://example.com",
            region=cls.city,
            public=False,
        )
        cls.collection = cls.city_coll  # the mixin's default for img()

        cls.alice = User.objects.create_user(username="alice", first_name="alice")
        cls.bob = User.objects.create_user(username="bob", first_name="bob")

    def georef(self, image, user, confidence="high"):
        with self.stats_events():
            return Georeference.objects.create(
                image=image,
                point=Point(-77.43, 37.54, srid=4326),
                confidence=confidence,
                georeferenced_by=user,
            )

    def setUp(self):
        city_placed = self.img("city-placed", collection=self.city_coll)
        self.img("city-pending", collection=self.city_coll)
        other_placed = self.img("other-placed", collection=self.other_coll)
        self.img("other-pending", collection=self.other_coll)
        hidden_placed = self.img("hidden-placed", collection=self.hidden_coll)

        city_georef = self.georef(city_placed, self.alice)
        self.georef(other_placed, self.bob, confidence="low")
        self.georef(hidden_placed, self.bob)
        GeoreferenceValidation.objects.create(
            georeference=city_georef, validated_by=self.bob, validation="correct"
        )

    def stats(self, region_slug=None):
        if region_slug is not None:
            self.client.cookies[REGION_COOKIE_NAME] = region_slug
        return self.client.get(reverse("stats"))

    def contributors(self, response):
        return {
            name: (row["georeferences"], row["validations"])
            for name, row in response.context["contributors"]
        }

    def test_global_is_sitewide_but_public_only(self):
        response = self.stats()
        self.assertContains(response, "Site Statistics")
        overall = response.context["overall_stats"]
        self.assertEqual(overall["total_images"], 4)
        self.assertEqual(overall["total_georeferenced"], 2)
        self.assertEqual(overall["total_collections"], 2)
        self.assertEqual(json.loads(response.context["status_counts"]), [2, 1, 0, 1])
        self.assertEqual(json.loads(response.context["daily_counts"]), [2])
        # Bob's georeference in the private collection is invisible; his
        # validation of Alice's public one is not.
        self.assertEqual(
            self.contributors(response), {"alice": (1, 0), "bob": (1, 1)}
        )

    def test_region_scopes_every_figure(self):
        response = self.stats("richmond")
        self.assertContains(response, "Statistics for Richmond, Virginia")
        self.assertContains(response, "Contributors in Richmond")
        overall = response.context["overall_stats"]
        self.assertEqual(overall["total_images"], 2)
        self.assertEqual(overall["total_georeferenced"], 1)
        self.assertEqual(overall["total_collections"], 1)
        self.assertEqual(overall["total_sources"], 1)
        self.assertEqual(json.loads(response.context["status_counts"]), [1, 0, 0, 1])
        self.assertEqual(json.loads(response.context["daily_counts"]), [1])
        self.assertEqual(self.contributors(response), {"alice": (1, 0), "bob": (0, 1)})

    def test_ancestor_region_rolls_descendants_up(self):
        response = self.stats("virginia")
        overall = response.context["overall_stats"]
        self.assertEqual(overall["total_images"], 2)
        self.assertEqual(overall["total_georeferenced"], 1)
        self.assertEqual(self.contributors(response), {"alice": (1, 0), "bob": (0, 1)})

    def test_region_with_nothing_renders_empty_state(self):
        empty = make_region(
            short_name="Nowhere",
            long_name="Nowhere",
            slug="nowhere",
            # bulk_create, as in setUpTestData: WikidataItem.save() would
            # fetch live Wikidata metadata.
            wikidata_item=WikidataItem.objects.bulk_create(
                [WikidataItem(wikidata_id="Q404", title="Nowhere")]
            )[0],
        )
        response = self.stats(empty.slug)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["overall_stats"]["total_images"], 0)
        self.assertEqual(json.loads(response.context["daily_counts"]), [])
        self.assertContains(response, "No contributions in Nowhere yet.")

    def test_stale_cookie_falls_back_to_global(self):
        response = self.stats("atlantis")
        self.assertContains(response, "Site Statistics")
        self.assertEqual(response.context["overall_stats"]["total_images"], 4)


class ProcessImageGenerationGuardTests(TestCase):
    """process_image must never persist asset URLs from a superseded
    generation. cleanup_old_image_assets deletes every R2 generation
    directory except the current asset_generation's, so a stale write leaves
    the DB pointing at objects that no longer exist (404 thumbnails). This
    is exactly what happened when API imports queued several concurrent
    process_image tasks per image: each claimed its own generation, and the
    last DB write was not always the task holding the newest one."""

    @classmethod
    def setUpTestData(cls):
        cls.source = Source.objects.create(
            name="Src", slug="src", url="https://example.com", description=""
        )
        cls.collection = Collection.objects.create(
            source=cls.source, name="Col", slug="col", url="https://example.com"
        )

    def make_image(self):
        with patch("images.tasks.process_image.apply_async"):
            return Image.objects.create(
                collection=self.collection,
                title="Test",
                permalink="https://img.example.com/test.jpg",
            )

    @contextmanager
    def run_environment(self, download_side_effect):
        """Patch process_image's collaborators: image download, R2 uploads
        (return a URL derived from the key), and the tile task."""
        uploader = patch("images.tasks.R2Uploader").start()
        uploader.return_value.upload_file_content.side_effect = (
            lambda content, key, **kwargs: f"https://cdn.test/{key}"
        )
        patch("images.tasks.download_image", side_effect=download_side_effect).start()
        tiles = patch("images.tasks.generate_iiif_tiles.delay").start()
        try:
            yield tiles
        finally:
            patch.stopall()

    def test_persists_thumbnail_for_current_generation(self):
        image = self.make_image()

        def download(url, **kwargs):
            return PILImage.new("RGB", (600, 400))

        with self.run_environment(download) as tiles:
            process_image(image.id)

        image.refresh_from_db()
        self.assertEqual(image.asset_generation, 1)
        self.assertEqual(
            image.thumbnail,
            f"https://cdn.test/images/{image.id}/1/thumbnail.webp",
        )
        tiles.assert_called_once_with(image.id)

    def test_discards_write_when_generation_superseded(self):
        """Simulate a concurrent process_image run claiming a newer
        generation while this one is mid-flight (during the download): the
        stale run must not persist its URLs or queue tiling."""
        image = self.make_image()

        def download(url, **kwargs):
            Image.objects.filter(pk=image.id).update(
                asset_generation=F("asset_generation") + 1
            )
            return PILImage.new("RGB", (600, 400))

        with self.run_environment(download) as tiles:
            process_image(image.id)

        image.refresh_from_db()
        self.assertEqual(image.asset_generation, 2)
        # The superseded run uploaded to .../1/thumbnail.webp; persisting
        # that URL is the bug — generation 1's directory gets deleted by
        # cleanup_old_image_assets once generation 2 completes.
        self.assertFalse(image.thumbnail)
        tiles.assert_not_called()

    def test_forced_regeneration_advances_generation_and_recreates_assets(self):
        image = self.make_image()
        Image.objects.filter(pk=image.id).update(
            asset_generation=1,
            thumbnail=f"https://cdn.test/images/{image.id}/1/thumbnail.webp",
            tile_status="complete",
            iiif_url=f"https://cdn.test/images/{image.id}/1/tiles",
        )

        def download(url, **kwargs):
            return PILImage.new("RGB", (600, 400))

        with self.run_environment(download) as tiles:
            process_image(image.id, force=True)

        image.refresh_from_db()
        self.assertEqual(image.asset_generation, 2)
        self.assertEqual(
            image.thumbnail,
            f"https://cdn.test/images/{image.id}/2/thumbnail.webp",
        )
        self.assertEqual(image.tile_status, "")
        # The old tiles stay referenced until generate_iiif_tiles publishes the
        # new ones — clearing iiif_url here would blank the deep-zoom viewer for
        # the whole tiling run.
        self.assertEqual(image.iiif_url, f"https://cdn.test/images/{image.id}/1/tiles")
        tiles.assert_called_once_with(image.id)


class ImageAdminRegenerationActionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser(
            username="admin", email="admin@example.com", password="test"
        )
        source = Source.objects.create(
            name="Src", slug="src", url="https://example.com", description=""
        )
        collection = Collection.objects.create(
            source=source, name="Col", slug="col", url="https://example.com"
        )
        cls.image = Image.objects.create(
            collection=collection,
            title="Test",
            permalink="https://img.example.com/test.jpg",
        )

    def test_action_queues_forced_regeneration(self):
        self.client.force_login(self.user)

        with patch("images.admin.process_image.delay") as delay:
            response = self.client.post(
                reverse("admin:images_image_changelist"),
                {
                    "action": "regenerate_assets_action",
                    "_selected_action": [self.image.pk],
                },
            )

        self.assertEqual(response.status_code, 302)
        delay.assert_called_once_with(self.image.pk, force=True)

    def test_change_page_button_queues_forced_regeneration(self):
        self.client.force_login(self.user)
        change_url = reverse("admin:images_image_change", args=(self.image.pk,))

        response = self.client.get(change_url)

        self.assertContains(response, 'name="_save"')
        self.assertContains(response, 'name="_regenerate_assets"')

        # The button is a submit input on the change form, so post the form's
        # own values back (plus the inlines' management forms) with the
        # button's name attached.
        form = response.context["adminform"].form
        data = {
            name: form.get_initial_for_field(field, name)
            for name, field in form.fields.items()
            if form.get_initial_for_field(field, name) not in (None, "")
        }
        for inline in response.context["inline_admin_formsets"]:
            management_form = inline.formset.management_form
            data.update(
                {
                    management_form.add_prefix(name): value
                    for name, value in management_form.initial.items()
                }
            )
        data["_regenerate_assets"] = "Regenerate assets"

        with patch("images.admin.process_image.delay") as delay:
            response = self.client.post(change_url, data)

        self.assertRedirects(response, change_url)
        delay.assert_called_once_with(self.image.pk, force=True)


class QueueImageProcessingSignalTests(TestCase):
    """Exactly one process_image task per imported image. The API import
    flow saves the Image twice in one transaction (placeholder insert, then
    the permalink update after the S3 copy); only the save that sets the
    permalink should queue processing."""

    @classmethod
    def setUpTestData(cls):
        cls.source = Source.objects.create(
            name="Src", slug="src", url="https://example.com", description=""
        )
        cls.collection = Collection.objects.create(
            source=cls.source, name="Col", slug="col", url="https://example.com"
        )

    def test_import_commit_sequence_queues_one_task(self):
        with (
            patch("images.tasks.process_image.apply_async") as apply_async,
            self.captureOnCommitCallbacks(execute=True),
        ):
            image = Image.objects.create(
                collection=self.collection,
                title="Imported",
                permalink="",
            )
            image.permalink = "https://cdn.test/images/1/original.jpg"
            image.save(update_fields=["permalink"])

        apply_async.assert_called_once_with(args=[image.id])

    def test_save_without_permalink_queues_nothing(self):
        with (
            patch("images.tasks.process_image.apply_async") as apply_async,
            self.captureOnCommitCallbacks(execute=True),
        ):
            Image.objects.create(
                collection=self.collection,
                title="Placeholder",
                permalink="",
            )

        apply_async.assert_not_called()


# Coordinates for a small aerial-georeference footprint (a closed ring).
_SEARCH_POLYGON = (
    (-77.44, 37.53),
    (-77.43, 37.53),
    (-77.43, 37.54),
    (-77.44, 37.54),
    (-77.44, 37.53),
)


class SearchGeoreferenceFilterTests(TestCase):
    """The "georeferenced only" / "not georeferenced only" search filters must
    respect BOTH kinds of georeference, mirroring ``Image.is_georeferenced``:
    point georefs for regular images and aerial (polygon) georefs for aerial
    images.

    Regression test: the raw-SQL search filters used to check only the point
    ``images_georeference`` table, so an aerial image with only a polygon georef
    leaked through "not georeferenced only" (and was wrongly hidden by
    "georeferenced only"). Covers the ``semantic_search`` and ``text_search``
    endpoints; ``reverse_image_search`` shares ``semantic_search``'s SQL.
    """

    # A distinctive token in every image's description so a single trigram query
    # matches all fixtures regardless of their georeference state.
    TOKEN = "riverbend"

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="osm_1", password="test")
        cls.source = Source.objects.create(
            name="Src",
            slug="src",
            url="https://example.com",
            description="",
            public=True,
        )
        cls.collection = Collection.objects.create(
            source=cls.source,
            name="Col",
            slug="col",
            url="https://example.com",
            public=True,
        )

        # Non-aerial image with a point georeference -> georeferenced.
        cls.img_point = cls._make_image("Point photo")
        Georeference.objects.create(
            image=cls.img_point,
            point=Point(-77.43, 37.54, srid=4326),
            confidence="high",
            georeferenced_by=cls.user,
        )
        # Aerial image with a polygon georeference -> georeferenced. THE BUG CASE.
        cls.img_aerial = cls._make_image("Aerial photo", aerial=True)
        AerialGeoreference.objects.create(
            image=cls.img_aerial,
            polygon=Polygon(_SEARCH_POLYGON, srid=4326),
            confidence="high",
            georeferenced_by=cls.user,
        )
        # Non-aerial image with no georeference -> not georeferenced.
        cls.img_plain = cls._make_image("Plain photo")
        # Aerial image with no georeference -> not georeferenced.
        cls.img_aerial_plain = cls._make_image("Aerial pending photo", aerial=True)

        cls.all_ids = {
            cls.img_point.id,
            cls.img_aerial.id,
            cls.img_plain.id,
            cls.img_aerial_plain.id,
        }
        cls.georeferenced_ids = {cls.img_point.id, cls.img_aerial.id}
        cls.not_georeferenced_ids = {cls.img_plain.id, cls.img_aerial_plain.id}

    @classmethod
    def _make_image(cls, title, aerial=False):
        img = Image.objects.create(
            collection=cls.collection,
            title=title,
            permalink=f"https://img.example.com/{title}.jpg",
            description=f"A photograph of the {cls.TOKEN} district.",
            aerial=aerial,
            embedding=[0.1] * 768,
        )
        # Pick up the signal-computed is_searchable flag.
        img.refresh_from_db()
        return img

    # -- text search (trigram) ---------------------------------------------

    def _text_search_ids(self, **params):
        resp = self.client.get("/api/v1/search/text/", {"q": self.TOKEN, **params})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["success"])
        return {r["id"] for r in data["results"]}

    def test_text_search_no_filter_returns_all(self):
        self.assertEqual(self._text_search_ids(), self.all_ids)

    def test_text_search_not_georeferenced_only_excludes_aerial_polygon(self):
        ids = self._text_search_ids(non_georeferenced_only="true")
        # The aerial image is georeferenced via a polygon and must NOT leak
        # through the "not georeferenced only" filter.
        self.assertNotIn(self.img_aerial.id, ids)
        self.assertEqual(ids, self.not_georeferenced_ids)

    def test_text_search_georeferenced_only_includes_aerial_polygon(self):
        ids = self._text_search_ids(georeferenced_only="true")
        # The aerial-with-polygon image must be counted as georeferenced.
        self.assertIn(self.img_aerial.id, ids)
        self.assertEqual(ids, self.georeferenced_ids)

    # -- semantic search (CLIP embeddings; query encoding mocked) -----------

    def _semantic_search_ids(self, **params):
        with patch(
            "images.views.search.semantic._get_text_embedding",
            return_value=[0.1] * 768,
        ):
            resp = self.client.get("/api/v1/search/", {"q": self.TOKEN, **params})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["success"])
        return {r["id"] for r in data["results"]}

    def test_semantic_search_no_filter_returns_all(self):
        self.assertEqual(self._semantic_search_ids(), self.all_ids)

    def test_semantic_search_not_georeferenced_only_excludes_aerial_polygon(self):
        ids = self._semantic_search_ids(non_georeferenced_only="true")
        self.assertNotIn(self.img_aerial.id, ids)
        self.assertEqual(ids, self.not_georeferenced_ids)

    def test_semantic_search_georeferenced_only_includes_aerial_polygon(self):
        ids = self._semantic_search_ids(georeferenced_only="true")
        self.assertIn(self.img_aerial.id, ids)
        self.assertEqual(ids, self.georeferenced_ids)


class SearchRegionFilterTests(TestCase):
    """Search is scoped to the navbar region, across all three search modes.

    The raw-SQL endpoints can't use ImageQuerySet.in_region, so they share
    Image.EFFECTIVE_REGION_SQL instead. It has to reproduce in_region exactly:
    resolve each image's region image -> collection -> source (first match
    wins, NOT an OR across the three FKs) and match the selected region plus
    its descendants.
    """

    TOKEN = "riverbend"

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="osm_region", password="test")
        cls.city_item, cls.state_item = WikidataItem.objects.bulk_create(
            [
                WikidataItem(wikidata_id="Q43421", title="Richmond"),
                WikidataItem(wikidata_id="Q1370", title="Virginia"),
            ]
        )
        cls.city = make_region(
            short_name="Richmond",
            long_name="Richmond, Virginia",
            slug="richmond",
            wikidata_item=cls.city_item,
        )
        cls.state = make_region(
            short_name="Virginia",
            long_name="Virginia",
            slug="virginia",
            wikidata_item=cls.state_item,
        )
        # The city sits inside the state, so selecting the state must roll the
        # city's images up into it.
        RegionAncestor.objects.create(region=cls.city, ancestor=cls.state_item)

        cls.plain_source = Source.objects.create(
            name="Plain Src",
            slug="plain-src",
            url="https://example.com",
            description="",
            public=True,
        )
        cls.state_source = Source.objects.create(
            name="State Src",
            slug="state-src",
            url="https://example.com",
            description="",
            public=True,
            region=cls.state,
        )
        cls.plain_coll = cls._make_collection("Plain", "plain", cls.plain_source)
        cls.city_coll = cls._make_collection(
            "City", "city", cls.plain_source, region=cls.city
        )
        cls.inherit_coll = cls._make_collection("Inherit", "inherit", cls.state_source)

        # One image per resolution path, plus the override case.
        cls.img_city_via_image = cls._make_image(
            "City by image", cls.plain_coll, region=cls.city
        )
        cls.img_city_via_collection = cls._make_image("City by collection", cls.city_coll)
        cls.img_state_via_source = cls._make_image("State by source", cls.inherit_coll)
        cls.img_no_region = cls._make_image("No region", cls.plain_coll)
        # Its collection says city, but the image itself says state: precedence
        # means this image depicts the state, and must NOT appear under city.
        cls.img_override = cls._make_image(
            "Override", cls.city_coll, region=cls.state
        )

        cls.all_ids = {
            cls.img_city_via_image.id,
            cls.img_city_via_collection.id,
            cls.img_state_via_source.id,
            cls.img_no_region.id,
            cls.img_override.id,
        }
        cls.city_ids = {cls.img_city_via_image.id, cls.img_city_via_collection.id}
        # Everything except the region-less image: the city rolls up into the
        # state, and two images resolve to the state directly.
        cls.state_ids = cls.all_ids - {cls.img_no_region.id}

    @classmethod
    def _make_collection(cls, name, slug, source, region=None):
        return Collection.objects.create(
            source=source,
            name=name,
            slug=slug,
            url="https://example.com",
            public=True,
            region=region,
        )

    @classmethod
    def _make_image(cls, title, collection, region=None):
        img = Image.objects.create(
            collection=collection,
            title=title,
            permalink=f"https://img.example.com/{title}.jpg",
            description=f"A photograph of the {cls.TOKEN} district.",
            region=region,
            embedding=[0.1] * 768,
        )
        # Pick up the signal-computed is_searchable flag.
        img.refresh_from_db()
        return img

    def select_region(self, slug):
        self.client.cookies[REGION_COOKIE_NAME] = slug

    # -- text search (trigram) ---------------------------------------------

    def _text_search(self, **params):
        resp = self.client.get("/api/v1/search/text/", {"q": self.TOKEN, **params})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["success"])
        return data

    def _text_search_ids(self, **params):
        return {r["id"] for r in self._text_search(**params)["results"]}

    def test_text_search_without_a_region_is_sitewide(self):
        self.assertEqual(self._text_search_ids(), self.all_ids)

    def test_text_search_scopes_to_the_selected_region(self):
        self.select_region("richmond")
        ids = self._text_search_ids()
        # The override image lives in a city collection but depicts the state.
        self.assertNotIn(self.img_override.id, ids)
        self.assertEqual(ids, self.city_ids)

    def test_text_search_rolls_descendant_regions_up(self):
        self.select_region("virginia")
        ids = self._text_search_ids()
        self.assertNotIn(self.img_no_region.id, ids)
        self.assertEqual(ids, self.state_ids)

    def test_text_search_ignores_an_unknown_region_cookie(self):
        # A stale slug resolves to None rather than erroring or hiding results.
        self.select_region("atlantis")
        self.assertEqual(self._text_search_ids(), self.all_ids)

    def test_text_search_count_is_filtered_too(self):
        # The count query is separate SQL from the page query; both need the
        # region condition or pagination reports phantom results.
        self.select_region("richmond")
        self.assertEqual(self._text_search()["count"], len(self.city_ids))

    def test_text_search_matches_the_orm_in_region(self):
        # The SQL mirror and its canonical ORM twin must select the same rows.
        self.select_region("richmond")
        self.assertEqual(
            self._text_search_ids(),
            set(Image.objects.in_region(self.city).values_list("id", flat=True)),
        )

    # -- semantic search (CLIP embeddings; query encoding mocked) -----------

    def _semantic_search_ids(self, **params):
        with patch(
            "images.views.search.semantic._get_text_embedding",
            return_value=[0.1] * 768,
        ):
            resp = self.client.get("/api/v1/search/", {"q": self.TOKEN, **params})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["success"])
        return {r["id"] for r in data["results"]}

    def test_semantic_search_without_a_region_is_sitewide(self):
        self.assertEqual(self._semantic_search_ids(), self.all_ids)

    def test_semantic_search_scopes_to_the_selected_region(self):
        self.select_region("richmond")
        ids = self._semantic_search_ids()
        self.assertNotIn(self.img_override.id, ids)
        self.assertEqual(ids, self.city_ids)

    def test_semantic_search_rolls_descendant_regions_up(self):
        self.select_region("virginia")
        self.assertEqual(self._semantic_search_ids(), self.state_ids)

    def test_semantic_search_ignores_an_unknown_region_cookie(self):
        self.select_region("atlantis")
        self.assertEqual(self._semantic_search_ids(), self.all_ids)

    # -- reverse image search ----------------------------------------------

    def _reverse_search_ids(self):
        # reverse_image_search duplicates semantic_search's WHERE-building, so
        # the region condition is a separate insertion and needs its own test.
        buf = BytesIO()
        PILImage.new("RGB", (2, 2)).save(buf, format="PNG")
        upload = SimpleUploadedFile("query.png", buf.getvalue(), content_type="image/png")
        with patch(
            "images.views.search.reverse_image._get_image_embedding",
            return_value=[0.1] * 768,
        ):
            resp = self.client.post("/api/v1/search/reverse/", {"image": upload})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["success"])
        return {r["id"] for r in data["results"]}

    def test_reverse_search_scopes_to_the_selected_region(self):
        self.client.force_login(self.user)
        self.assertEqual(self._reverse_search_ids(), self.all_ids)
        self.select_region("richmond")
        ids = self._reverse_search_ids()
        self.assertNotIn(self.img_override.id, ids)
        self.assertEqual(ids, self.city_ids)


class BulkImageFlagTests(TestCase):
    """The staff-only bulk endpoints that mark images "from above" (aerial) or
    "will not georeference". They report the number of images actually changed
    (excluding those already tagged) and refresh CollectionStats, which the
    underlying queryset UPDATE would otherwise bypass."""

    FROM_ABOVE_URL = "/api/v1/bulk/from-above/"
    WILL_NOT_GEOREF_URL = "/api/v1/bulk/will-not-georef/"

    @classmethod
    def setUpTestData(cls):
        cls.source = Source.objects.create(
            name="Src", slug="src", url="https://example.com", description=""
        )
        cls.collection = Collection.objects.create(
            source=cls.source, name="Col", slug="col", url="https://example.com"
        )
        cls.staff = User.objects.create_user(
            username="osm_staff", first_name="Sam", is_staff=True
        )
        cls.regular = User.objects.create_user(username="osm_regular", first_name="Reg")

    def make_images(self, n, **kwargs):
        return [
            Image.objects.create(
                collection=self.collection,
                title=f"img{i}",
                permalink=f"https://img.example.com/{i}.jpg",
                **kwargs,
            )
            for i in range(n)
        ]

    def post(self, url, image_ids):
        return self.client.post(
            url, {"image_ids": image_ids}, content_type="application/json"
        )

    # -- happy path --------------------------------------------------------

    def test_staff_marks_from_above(self):
        images = self.make_images(3)
        self.client.force_login(self.staff)
        resp = self.post(self.FROM_ABOVE_URL, [img.id for img in images])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"success": True, "updated_count": 3})
        for img in images:
            img.refresh_from_db()
            self.assertTrue(img.aerial)

    def test_staff_marks_will_not_georef(self):
        images = self.make_images(2)
        self.client.force_login(self.staff)
        resp = self.post(self.WILL_NOT_GEOREF_URL, [img.id for img in images])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["updated_count"], 2)
        for img in images:
            img.refresh_from_db()
            self.assertTrue(img.will_not_georef)

    # -- the "actual number changed" logic ---------------------------------

    def test_updated_count_excludes_already_tagged(self):
        already = self.make_images(1, aerial=True)[0]
        fresh = self.make_images(2)
        self.client.force_login(self.staff)
        resp = self.post(self.FROM_ABOVE_URL, [already.id, *[img.id for img in fresh]])
        self.assertEqual(resp.status_code, 200)
        # 3 selected, 1 already aerial -> only 2 actually changed.
        self.assertEqual(resp.json()["updated_count"], 2)
        for img in fresh:
            img.refresh_from_db()
            self.assertTrue(img.aerial)

    def test_updated_count_zero_when_all_already_tagged(self):
        images = self.make_images(2, will_not_georef=True)
        self.client.force_login(self.staff)
        resp = self.post(self.WILL_NOT_GEOREF_URL, [img.id for img in images])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["updated_count"], 0)

    # -- CollectionStats stays in sync despite the queryset UPDATE ----------

    def test_marking_refreshes_collection_stats(self):
        images = self.make_images(3)
        self.client.force_login(self.staff)
        self.post(self.WILL_NOT_GEOREF_URL, [img.id for img in images])
        stats = CollectionStats.objects.get(pk=self.collection.pk)
        self.assertEqual(stats.will_not_georef_images, 3)

    # -- permissions -------------------------------------------------------

    def test_anonymous_is_unauthorized(self):
        images = self.make_images(2)
        resp = self.post(self.FROM_ABOVE_URL, [img.id for img in images])
        self.assertEqual(resp.status_code, 401)
        for img in images:
            img.refresh_from_db()
            self.assertFalse(img.aerial)

    def test_non_staff_is_forbidden(self):
        images = self.make_images(2)
        self.client.force_login(self.regular)
        resp = self.post(self.WILL_NOT_GEOREF_URL, [img.id for img in images])
        self.assertEqual(resp.status_code, 403)
        for img in images:
            img.refresh_from_db()
            self.assertFalse(img.will_not_georef)

    # -- input validation --------------------------------------------------

    def test_empty_image_ids_is_bad_request(self):
        self.client.force_login(self.staff)
        resp = self.post(self.FROM_ABOVE_URL, [])
        self.assertEqual(resp.status_code, 400)

    def test_get_is_not_allowed(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self.FROM_ABOVE_URL)
        self.assertEqual(resp.status_code, 405)


class UserGeoreferencesPageTests(TestCase):
    """The public "images georeferenced by <user>" page at
    /user/<osm-username>/georeferences/.

    The listing joins two multi-valued relations (point and aerial
    georeferences) with an OR, so the interesting cases are the ones where that
    fan-out could duplicate or drop rows: a user who georeferenced the same
    image twice, and a user with only one kind of georeference.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="osm_1", first_name="Alice")
        cls.other = User.objects.create_user(username="osm_2", first_name="Bob")
        cls.source = Source.objects.create(
            name="Src",
            slug="src",
            url="https://example.com",
            description="",
            public=True,
        )
        cls.collection = Collection.objects.create(
            source=cls.source,
            name="Col",
            slug="col",
            url="https://example.com",
            public=True,
        )
        # A private collection's images are excluded from the public listing.
        cls.private_collection = Collection.objects.create(
            source=cls.source,
            name="Private",
            slug="private",
            url="https://example.com",
            public=False,
        )

        cls.img_point = cls._make_image("Point photo")
        cls._point_georef(cls.img_point, cls.user)

        cls.img_aerial = cls._make_image("Aerial photo", aerial=True)
        AerialGeoreference.objects.create(
            image=cls.img_aerial,
            polygon=Polygon(_SEARCH_POLYGON, srid=4326),
            confidence="high",
            georeferenced_by=cls.user,
        )

        # Georeferenced twice by the same user: must still appear once.
        cls.img_twice = cls._make_image("Corrected photo")
        cls._point_georef(cls.img_twice, cls.user)
        cls._point_georef(cls.img_twice, cls.user)

        # Somebody else's work.
        cls.img_other = cls._make_image("Bob's photo")
        cls._point_georef(cls.img_other, cls.other)

        # Our user's work, but on a non-public collection.
        cls.img_hidden = cls._make_image(
            "Hidden photo", collection=cls.private_collection
        )
        cls._point_georef(cls.img_hidden, cls.user)

        cls.url = "/user/Alice/georeferences/"

    @classmethod
    def _make_image(cls, title, aerial=False, collection=None):
        img = Image.objects.create(
            collection=collection or cls.collection,
            title=title,
            permalink=f"https://img.example.com/{title}.jpg",
            aerial=aerial,
        )
        # Pick up the signal-computed is_searchable flag.
        img.refresh_from_db()
        return img

    @classmethod
    def _point_georef(cls, image, user):
        return Georeference.objects.create(
            image=image,
            point=Point(-77.43, 37.54, srid=4326),
            confidence="high",
            georeferenced_by=user,
        )

    def _listed_ids(self, **params):
        resp = self.client.get(self.url, params)
        self.assertEqual(resp.status_code, 200)
        return [img.id for img in resp.context["page_obj"]]

    def test_lists_point_and_aerial_georeferences(self):
        ids = self._listed_ids()
        self.assertIn(self.img_point.id, ids)
        self.assertIn(self.img_aerial.id, ids)

    def test_excludes_another_users_georeferences(self):
        self.assertNotIn(self.img_other.id, self._listed_ids())

    def test_excludes_images_hidden_from_the_public(self):
        self.assertNotIn(self.img_hidden.id, self._listed_ids())

    def test_image_georeferenced_twice_appears_once(self):
        ids = self._listed_ids()
        self.assertEqual(ids.count(self.img_twice.id), 1)
        # And the total reflects deduplicated images, not georeference rows.
        self.assertEqual(len(ids), 3)

    def test_ordered_by_most_recent_georeference_first(self):
        # img_twice was georeferenced last, so it leads.
        self.assertEqual(self._listed_ids()[0], self.img_twice.id)

    def test_counts_are_image_counts(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.context["total_images"], 3)
        self.assertEqual(resp.context["map_image_count"], 2)
        self.assertEqual(resp.context["aerial_count"], 1)

    def test_map_count_excludes_images_the_tile_layer_drops(self):
        """The tile layer is built from public_georeferences_mvt, which excludes
        will_not_georef images. Such an image still belongs in the grid -- the
        user did georeference it -- but it can never be a pin, so the "on the
        map" stat (and the map's own visibility gate) must not count it."""
        skipped = self._make_image("Skipped photo")
        self._point_georef(skipped, self.user)
        Image.objects.filter(pk=skipped.pk).update(will_not_georef=True)

        resp = self.client.get(self.url)
        self.assertIn(skipped.id, [img.id for img in resp.context["page_obj"]])
        self.assertEqual(resp.context["total_images"], 4)
        self.assertEqual(resp.context["map_image_count"], 2)

    def test_filter_params_and_paging_are_accepted(self):
        self.assertEqual(self.client.get(self.url, {"page": 2}).status_code, 200)
        self.assertEqual(
            self.client.get(self.url, {"start_year": 1900}).status_code, 200
        )
        self.assertEqual(
            self.client.get(
                self.url, {"georeference_status": "georeferenced"}
            ).status_code,
            200,
        )

    def test_user_with_no_georeferences_renders_empty(self):
        User.objects.create_user(username="osm_3", first_name="Carol")
        resp = self.client.get("/user/Carol/georeferences/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["total_images"], 0)
        self.assertEqual(len(resp.context["page_obj"]), 0)

    def test_unknown_user_is_404(self):
        self.assertEqual(
            self.client.get("/user/Nobody/georeferences/").status_code, 404
        )

    def test_profile_page_links_to_the_listing(self):
        resp = self.client.get("/user/Alice/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, self.url)


class MapEmbedParamTests(TestCase):
    """Query-parameter coercion for the embeddable map view."""

    url = "/map/embed/"

    def test_numeric_params_are_floats(self):
        resp = self.client.get(
            self.url,
            {"center_lng": "-77.44", "center_lat": "37.53", "zoom_level": "11.5"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["center_lng"], -77.44)
        self.assertEqual(resp.context["center_lat"], 37.53)
        self.assertEqual(resp.context["zoom_level"], 11.5)
        self.assertContains(resp, "const mapCenter = [-77.44, 37.53];")

    def test_absent_params_are_none(self):
        resp = self.client.get(self.url)
        self.assertIsNone(resp.context["center_lng"])
        self.assertIsNone(resp.context["center_lat"])
        self.assertIsNone(resp.context["zoom_level"])

    def test_zero_is_preserved_not_treated_as_absent(self):
        resp = self.client.get(self.url, {"center_lng": "0", "center_lat": "0"})
        self.assertEqual(resp.context["center_lng"], 0.0)
        self.assertContains(resp, "const mapCenter = [0.0, 0.0];")

    def test_non_numeric_params_are_rejected(self):
        for bad in ("", "abc", "1;alert(1)", "NaN", "Infinity", "-inf"):
            with self.subTest(bad=bad):
                resp = self.client.get(
                    self.url, {"center_lng": bad, "center_lat": bad, "zoom_level": bad}
                )
                self.assertIsNone(resp.context["center_lng"])
                self.assertIsNone(resp.context["zoom_level"])

    def test_injection_attempt_does_not_reach_the_script(self):
        # These land in a JS numeric position, where HTML autoescaping is
        # no defence, so they must never survive as raw text.
        payload = "0;alert(document.domain)//"
        resp = self.client.get(self.url, {"center_lng": payload, "center_lat": payload})
        self.assertNotContains(resp, "alert(document.domain)")


class CompassLabelTests(SimpleTestCase):
    """Georeference.direction degrees rendered as a plain-language bearing."""

    def test_cardinal_and_intercardinal_points(self):
        for degrees, expected in (
            (0, "north"),
            (45, "north-east"),
            (90, "east"),
            (135, "south-east"),
            (180, "south"),
            (225, "south-west"),
            (270, "west"),
            (315, "north-west"),
        ):
            self.assertEqual(compass_label(degrees), expected, degrees)

    def test_degrees_round_to_the_nearest_octant(self):
        self.assertEqual(compass_label(22), "north")
        self.assertEqual(compass_label(23), "north-east")
        self.assertEqual(compass_label(47), "north-east")

    def test_wraps_past_north_rather_than_off_the_end(self):
        # The last octant boundary is 337.5; past it the index rounds up to
        # 8, which has to wrap back to north instead of running off the end.
        self.assertEqual(compass_label(337), "north-west")
        self.assertEqual(compass_label(338), "north")
        self.assertEqual(compass_label(359), "north")

    def test_no_direction_gives_no_label(self):
        self.assertIsNone(compass_label(None))


class DetectAddressesRegionTests(SimpleTestCase):
    def setUp(self):
        self.site_settings = SimpleNamespace(
            default_search_bbox_west=-2,
            default_search_bbox_south=52,
            default_search_bbox_east=-1,
            default_search_bbox_north=53,
        )

    def test_region_search_bounds_become_a_geopy_viewbox(self):
        map_bounds = Polygon.from_bbox((-79, 36, -75, 40))
        map_bounds.srid = 4326
        geocoder_bounds = Polygon.from_bbox((-78.8, 36.2, -75.2, 39.8))
        geocoder_bounds.srid = 4326
        region = Region(
            map_bounds=map_bounds,
            geocoder_bounds=geocoder_bounds,
        )
        self.assertEqual(
            DetectAddressesCommand.get_search_viewbox(region, self.site_settings),
            ((36.2, -78.8), (39.8, -75.2)),
        )

    def test_site_search_bounds_are_the_no_region_fallback(self):
        self.assertEqual(
            DetectAddressesCommand.get_search_viewbox(None, self.site_settings),
            ((52, -2), (53, -1)),
        )

    def test_region_name_disambiguates_the_nominatim_query(self):
        command = DetectAddressesCommand()
        geocode = Mock(return_value=object())
        progress_bar = Mock()
        viewbox = ((36.2, -78.8), (39.8, -75.2))
        command.geocode_address(
            geocode,
            "100 Main St",
            progress_bar,
            viewbox,
            "Norfolk, Virginia",
            max_retries=1,
        )
        geocode.assert_called_once_with(
            "100 Main St, Norfolk, Virginia",
            viewbox=viewbox,
            bounded=True,
            exactly_one=True,
        )


# ---------------------------------------------------------------------------
# SA-01: object-level authorization, CSRF, and input limits on the community
# write endpoints.
# ---------------------------------------------------------------------------


def _ring(west, south, size=0.01):
    """A closed, counter-clockwise square ring for polygon fixtures."""
    return [
        [west, south],
        [west + size, south],
        [west + size, south + size],
        [west, south + size],
        [west, south],
    ]


def _polygon(west=-77.5, south=37.5, size=0.01):
    return {"type": "Polygon", "coordinates": [_ring(west, south, size)]}


class CommunityWriteFixtureMixin:
    """The visibility matrix every SA-01 test works against.

    One public image, one hidden behind a private collection, one hidden
    behind a private source, plus the three eligibility variants (duplicate,
    will_not_georef, aerial) that the georeferencing policies care about.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("sa01_user", password="pw")
        cls.other_user = User.objects.create_user("sa01_other", password="pw")
        cls.staff = User.objects.create_user("sa01_staff", password="pw", is_staff=True)

        public_source = Source.objects.create(
            name="Public source",
            slug="sa01-public-source",
            url="https://example.com/public",
            description="",
            public=True,
        )
        private_source = Source.objects.create(
            name="Private source",
            slug="sa01-private-source",
            url="https://example.com/private",
            description="",
            public=False,
        )

        cls.public_collection = Collection.objects.create(
            source=public_source, name="Public collection", slug="sa01-public"
        )
        private_collection = Collection.objects.create(
            source=public_source,
            name="Private collection",
            slug="sa01-private-collection",
            public=False,
        )
        private_source_collection = Collection.objects.create(
            source=private_source,
            name="Collection in private source",
            slug="sa01-private-source-collection",
        )

        def make_image(collection, title, **kwargs):
            return Image.objects.create(
                collection=collection,
                title=title,
                permalink=f"https://img.example.com/{slugify(title)}.jpg",
                **kwargs,
            )

        cls.public_image = make_image(cls.public_collection, "sa01 public")
        cls.second_public_image = make_image(cls.public_collection, "sa01 public two")
        cls.private_collection_image = make_image(
            private_collection, "sa01 private collection"
        )
        cls.private_source_image = make_image(
            private_source_collection, "sa01 private source"
        )
        cls.duplicate_image = make_image(
            cls.public_collection, "sa01 duplicate", duplicate_of=cls.public_image
        )
        cls.will_not_georef_image = make_image(
            cls.public_collection, "sa01 will not georef", will_not_georef=True
        )
        cls.aerial_image = make_image(cls.public_collection, "sa01 aerial", aerial=True)

        # Images the ordinary community may not write to at all.
        cls.inaccessible_images = [
            cls.private_collection_image,
            cls.private_source_image,
            cls.duplicate_image,
        ]

    def login(self, user=None):
        self.client.force_login(user or self.user)

    def post_json(self, url, payload=None, client=None, method="post"):
        client = client or self.client
        return getattr(client, method)(
            url,
            data=json.dumps({} if payload is None else payload),
            content_type="application/json",
        )


class CommunityWriteAuthorizationTests(CommunityWriteFixtureMixin, TestCase):
    """Every image-targeting mutation resolves its image through a policy."""

    def image_routes(self):
        """(label, url builder, payload) for each single-image mutation."""
        return [
            (
                "georeference",
                lambda image: reverse("images:georeference_image", args=[image.id]),
                {"latitude": 37.5, "longitude": -77.4, "confidence": "high"},
            ),
            (
                "skip",
                lambda image: reverse("images:skip_image", args=[image.id]),
                {"reason": "unclear"},
            ),
            (
                "comment",
                lambda image: reverse("images:add_comment", args=[image.id]),
                {"text": "hello"},
            ),
            (
                "rating",
                lambda image: reverse("images:submit_rating", args=[image.id]),
                {"rating": 5},
            ),
        ]

    def test_inaccessible_images_are_indistinguishable_from_missing_ones(self):
        self.login()
        missing_id = Image.objects.order_by("-id").first().id + 1000

        for label, build_url, payload in self.image_routes():
            for image in self.inaccessible_images:
                with self.subTest(route=label, image=image.title):
                    response = self.post_json(build_url(image), payload)
                    self.assertEqual(response.status_code, 404)

            with self.subTest(route=label, image="missing"):
                response = self.post_json(
                    build_url(SimpleNamespace(id=missing_id)), payload
                )
                self.assertEqual(response.status_code, 404)

    def test_authorized_public_image_operations_still_work(self):
        self.login()
        expected = {"georeference": 200, "skip": 200, "comment": 201, "rating": 200}

        for label, build_url, payload in self.image_routes():
            with self.subTest(route=label):
                response = self.post_json(build_url(self.public_image), payload)
                self.assertEqual(response.status_code, expected[label])

    def test_staff_may_write_to_private_images(self):
        self.login(self.staff)
        response = self.post_json(
            reverse("images:add_comment", args=[self.private_collection_image.id]),
            {"text": "staff note"},
        )
        self.assertEqual(response.status_code, 201)

    def test_staff_may_not_write_to_duplicates(self):
        self.login(self.staff)
        response = self.post_json(
            reverse("images:add_comment", args=[self.duplicate_image.id]),
            {"text": "on a copy"},
        )
        self.assertEqual(response.status_code, 404)

    def test_will_not_georef_images_reject_point_submissions_and_skips(self):
        self.login()
        for name, payload in (
            ("images:georeference_image", {"latitude": 37.5, "longitude": -77.4,
                                           "confidence": "high"}),
            ("images:skip_image", {}),
        ):
            with self.subTest(route=name):
                response = self.post_json(
                    reverse(name, args=[self.will_not_georef_image.id]), payload
                )
                self.assertEqual(response.status_code, 404)

    def test_ordinary_users_cannot_point_georeference_an_aerial(self):
        self.login()
        response = self.post_json(
            reverse("images:georeference_image", args=[self.aerial_image.id]),
            {"latitude": 37.5, "longitude": -77.4, "confidence": "high"},
        )
        self.assertEqual(response.status_code, 404)

    def test_staff_may_point_georeference_an_aerial(self):
        """Deliberate: georeference_interface serves aerials to staff."""
        self.login(self.staff)
        response = self.post_json(
            reverse("images:georeference_image", args=[self.aerial_image.id]),
            {"latitude": 37.5, "longitude": -77.4, "confidence": "high"},
        )
        self.assertEqual(response.status_code, 200)

    def test_polygon_submission_requires_an_aerial_image_even_for_staff(self):
        self.login(self.staff)
        response = self.post_json(
            reverse(
                "images:aerial_georeference_image", args=[self.public_image.id]
            ),
            {"polygon": _polygon(), "confidence": "high"},
        )
        self.assertEqual(response.status_code, 404)

    def test_polygon_submission_requires_authentication(self):
        response = self.post_json(
            reverse(
                "images:aerial_georeference_image", args=[self.aerial_image.id]
            ),
            {"polygon": _polygon(), "confidence": "high"},
        )
        self.assertEqual(response.status_code, 401)

    def test_polygon_submission_on_an_eligible_aerial_succeeds(self):
        self.login()
        response = self.post_json(
            reverse(
                "images:aerial_georeference_image", args=[self.aerial_image.id]
            ),
            {"polygon": _polygon(), "confidence": "high"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(AerialGeoreference.objects.filter(
            image=self.aerial_image).count(), 1)

    def test_validation_resolves_through_the_related_image(self):
        hidden = Georeference.objects.create(
            image=self.private_collection_image,
            point=Point(-77.4, 37.5, srid=4326),
            confidence="high",
            georeferenced_by=self.other_user,
        )
        self.login()
        response = self.post_json(
            reverse("images:validate_georeference", args=[hidden.id]),
            {"validation": "correct"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(GeoreferenceValidation.objects.exists())

    def test_validation_of_a_visible_georeference_succeeds(self):
        georeference = Georeference.objects.create(
            image=self.public_image,
            point=Point(-77.4, 37.5, srid=4326),
            confidence="high",
            georeferenced_by=self.other_user,
        )
        self.login()
        response = self.post_json(
            reverse("images:validate_georeference", args=[georeference.id]),
            {"validation": "correct"},
        )
        self.assertEqual(response.status_code, 200)

    def test_duplicate_validation_is_a_400_not_a_500(self):
        georeference = Georeference.objects.create(
            image=self.public_image,
            point=Point(-77.4, 37.5, srid=4326),
            confidence="high",
            georeferenced_by=self.other_user,
        )
        GeoreferenceValidation.objects.create(
            georeference=georeference, validated_by=self.user, validation="correct"
        )
        self.login()
        response = self.post_json(
            reverse("images:validate_georeference", args=[georeference.id]),
            {"validation": "incorrect"},
        )
        self.assertEqual(response.status_code, 400)

    def test_mutation_endpoints_reject_get(self):
        self.login()
        urls = [
            reverse("images:georeference_image", args=[self.public_image.id]),
            reverse("images:aerial_georeference_image", args=[self.aerial_image.id]),
            reverse("images:add_comment", args=[self.public_image.id]),
            reverse("images:submit_rating", args=[self.public_image.id]),
            reverse("images:skip_image", args=[self.public_image.id]),
            reverse("images:add_image_to_album"),
            reverse("images:create_and_add_to_album"),
            reverse("images:remove_image_from_album"),
            reverse("images:bulk_add_to_album"),
            reverse("images:bulk_create_and_add_to_album"),
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 405)

    def test_anonymous_callers_cannot_comment_or_rate(self):
        for name in ("images:add_comment", "images:submit_rating"):
            with self.subTest(route=name):
                response = self.post_json(
                    reverse(name, args=[self.public_image.id]), {"text": "x", "rating": 3}
                )
                self.assertEqual(response.status_code, 401)


class CommunityWriteCsrfTests(CommunityWriteFixtureMixin, TestCase):
    """No cookie-authenticated mutation endpoint is CSRF-exempt any more."""

    def setUp(self):
        self.csrf_client = Client(enforce_csrf_checks=True)
        self.csrf_client.force_login(self.user)

    def formerly_exempt_routes(self):
        return [
            (
                reverse("images:georeference_image", args=[self.public_image.id]),
                {"latitude": 37.5, "longitude": -77.4, "confidence": "high"},
            ),
            (
                reverse(
                    "images:aerial_georeference_image", args=[self.aerial_image.id]
                ),
                {"polygon": _polygon(), "confidence": "high"},
            ),
            (
                reverse("images:add_comment", args=[self.public_image.id]),
                {"text": "hi"},
            ),
            (
                reverse("images:submit_rating", args=[self.public_image.id]),
                {"rating": 4},
            ),
            (
                reverse("images:skip_image", args=[self.public_image.id]),
                {},
            ),
            (
                reverse("images:bulk_add_to_album"),
                {"album_id": None, "image_ids": [self.public_image.id]},
            ),
            (
                reverse("images:bulk_create_and_add_to_album"),
                {"title": "t", "image_ids": [self.public_image.id]},
            ),
        ]

    def test_missing_csrf_token_is_rejected(self):
        for url, payload in self.formerly_exempt_routes():
            with self.subTest(url=url):
                response = self.post_json(url, payload, client=self.csrf_client)
                self.assertEqual(response.status_code, 403)

    def test_invalid_csrf_token_is_rejected(self):
        for url, payload in self.formerly_exempt_routes():
            with self.subTest(url=url):
                response = self.csrf_client.post(
                    url,
                    data=json.dumps(payload),
                    content_type="application/json",
                    HTTP_X_CSRFTOKEN="not-a-real-token",
                )
                self.assertEqual(response.status_code, 403)

    def fetch_csrf_token(self, client):
        """Read the CSRF cookie a rendered page sets.

        The georeferencing interface is the right page to ask: it serializes a
        token into its config for anonymous and logged-in visitors alike,
        because anonymous georeferencing is supported.
        """
        client.get(
            reverse("images:georeference_interface"),
            {"image": self.public_image.id},
        )
        self.assertIn(
            "csrftoken",
            client.cookies,
            "expected the georeferencing interface to set a CSRF cookie",
        )
        return client.cookies["csrftoken"].value

    def test_valid_csrf_token_is_accepted(self):
        token = self.fetch_csrf_token(self.csrf_client)

        response = self.csrf_client.post(
            reverse("images:add_comment", args=[self.public_image.id]),
            data=json.dumps({"text": "with a token"}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(response.status_code, 201)

    def test_anonymous_georeference_also_requires_a_token(self):
        anonymous = Client(enforce_csrf_checks=True)
        url = reverse("images:georeference_image", args=[self.public_image.id])
        payload = {"latitude": 37.5, "longitude": -77.4, "confidence": "high"}

        self.assertEqual(
            self.post_json(url, payload, client=anonymous).status_code, 403
        )

        response = anonymous.post(
            url,
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=self.fetch_csrf_token(anonymous),
        )
        self.assertEqual(response.status_code, 200)


class GeoreferenceInputValidationTests(CommunityWriteFixtureMixin, TestCase):
    """Malformed or over-limit input returns 400 without writing anything."""

    def setUp(self):
        self.login()
        self.url = reverse("images:georeference_image", args=[self.public_image.id])

    def submit(self, **overrides):
        payload = {"latitude": 37.5, "longitude": -77.4, "confidence": "high"}
        payload.update(overrides)
        return self.post_json(self.url, payload)

    def test_non_finite_json_literals_are_rejected(self):
        for literal in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(literal=literal):
                response = self.client.post(
                    self.url,
                    data=(
                        '{"latitude": %s, "longitude": -77.4, "confidence": "high"}'
                        % literal
                    ),
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 400)
        self.assertFalse(Georeference.objects.exists())

    def test_quoted_non_finite_coordinates_are_rejected(self):
        self.assertEqual(self.submit(latitude="nan").status_code, 400)
        self.assertEqual(self.submit(longitude="inf").status_code, 400)
        self.assertFalse(Georeference.objects.exists())

    def test_coordinate_bounds(self):
        self.assertEqual(self.submit(latitude=90, longitude=180).status_code, 200)
        self.assertEqual(self.submit(latitude=-90, longitude=-180).status_code, 200)
        self.assertEqual(self.submit(latitude=90.0001).status_code, 400)
        self.assertEqual(self.submit(longitude=-180.0001).status_code, 400)

    def test_direction_zero_is_preserved(self):
        response = self.submit(direction=0)
        self.assertEqual(response.status_code, 200)
        georeference = Georeference.objects.get(
            id=response.json()["georeference_id"]
        )
        self.assertEqual(georeference.direction, 0)

    def test_invalid_directions_are_rejected(self):
        for direction in (-1, 360, 12.5, "north", True):
            with self.subTest(direction=direction):
                self.assertEqual(self.submit(direction=direction).status_code, 400)

    def test_omitted_direction_stays_null(self):
        response = self.submit()
        georeference = Georeference.objects.get(
            id=response.json()["georeference_id"]
        )
        self.assertIsNone(georeference.direction)

    def test_unknown_confidence_is_rejected(self):
        self.assertEqual(self.submit(confidence="perfect").status_code, 400)

    def test_low_confidence_requires_notes(self):
        self.assertEqual(self.submit(confidence="low").status_code, 400)
        self.assertEqual(
            self.submit(confidence="low", notes="a hunch").status_code, 200
        )

    @override_settings(GEOREFERENCE_NOTES_MAX_LENGTH=20)
    def test_notes_length_boundary(self):
        self.assertEqual(self.submit(notes="x" * 20).status_code, 200)
        self.assertEqual(self.submit(notes="x" * 21).status_code, 400)

    @override_settings(COMMUNITY_WRITE_MAX_BODY_BYTES=64)
    def test_oversized_body_is_rejected_before_parsing(self):
        self.assertEqual(self.submit(notes="x" * 500).status_code, 400)
        self.assertFalse(Georeference.objects.exists())

    @override_settings(COMMENT_MAX_LENGTH=10)
    def test_comment_length_boundary(self):
        url = reverse("images:add_comment", args=[self.public_image.id])
        self.assertEqual(self.post_json(url, {"text": "x" * 10}).status_code, 201)
        self.assertEqual(self.post_json(url, {"text": "x" * 11}).status_code, 400)

    def test_empty_comment_is_rejected(self):
        url = reverse("images:add_comment", args=[self.public_image.id])
        self.assertEqual(self.post_json(url, {"text": "   "}).status_code, 400)
        self.assertFalse(Comment.objects.exists())

    @override_settings(SKIP_REASON_MAX_LENGTH=5)
    def test_skip_reason_length_boundary(self):
        url = reverse("images:skip_image", args=[self.public_image.id])
        self.assertEqual(self.post_json(url, {"reason": "12345"}).status_code, 200)
        self.assertEqual(self.post_json(url, {"reason": "123456"}).status_code, 400)

    @override_settings(VALIDATION_NOTES_MAX_LENGTH=5)
    def test_validation_notes_length_boundary(self):
        georeference = Georeference.objects.create(
            image=self.public_image,
            point=Point(-77.4, 37.5, srid=4326),
            confidence="high",
            georeferenced_by=self.other_user,
        )
        url = reverse("images:validate_georeference", args=[georeference.id])
        self.assertEqual(
            self.post_json(url, {"validation": "correct", "notes": "123456"}).status_code,
            400,
        )
        self.assertEqual(
            self.post_json(url, {"validation": "correct", "notes": "12345"}).status_code,
            200,
        )

    def test_rating_bounds(self):
        url = reverse("images:submit_rating", args=[self.public_image.id])
        self.assertEqual(self.post_json(url, {"rating": 0}).status_code, 400)
        self.assertEqual(self.post_json(url, {"rating": 11}).status_code, 400)
        self.assertEqual(self.post_json(url, {"rating": True}).status_code, 400)
        self.assertEqual(self.post_json(url, {"rating": 1}).status_code, 200)

    def test_corrections_append_rather_than_overwrite(self):
        self.assertEqual(self.submit(latitude=37.5).status_code, 200)
        self.assertEqual(self.submit(latitude=37.6).status_code, 200)
        self.assertEqual(
            Georeference.objects.filter(
                image=self.public_image, georeferenced_by=self.user
            ).count(),
            2,
        )


class PolygonValidationTests(CommunityWriteFixtureMixin, TestCase):
    """Polygon limits are enforced before any geometry or database work."""

    def setUp(self):
        self.login()
        self.url = reverse(
            "images:aerial_georeference_image", args=[self.aerial_image.id]
        )

    def submit(self, polygon, **overrides):
        payload = {"polygon": polygon, "confidence": "high"}
        payload.update(overrides)
        return self.post_json(self.url, payload)

    def assert_rejected(self, polygon, label):
        with self.subTest(polygon=label):
            self.assertEqual(self.submit(polygon).status_code, 400)
            self.assertFalse(AerialGeoreference.objects.exists())

    def test_structurally_invalid_polygons_are_rejected(self):
        cases = {
            "not an object": "a string",
            "wrong type": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
            "no coordinates": {"type": "Polygon", "coordinates": []},
            "too few positions": {
                "type": "Polygon",
                "coordinates": [[[0, 0], [1, 0], [0, 0]]],
            },
            "unclosed ring": {
                "type": "Polygon",
                "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1]]],
            },
            "non numeric coordinate": {
                "type": "Polygon",
                "coordinates": [[["a", 0], [1, 0], [1, 1], ["a", 0]]],
            },
            "longitude out of bounds": {
                "type": "Polygon",
                "coordinates": [_ring(180.5, 37.5)],
            },
            "latitude out of bounds": {
                "type": "Polygon",
                "coordinates": [_ring(-77.5, 90.5)],
            },
            "multipolygon with two polygons": {
                "type": "MultiPolygon",
                "coordinates": [[_ring(-77.5, 37.5)], [_ring(-77.4, 37.4)]],
            },
        }
        for label, polygon in cases.items():
            self.assert_rejected(polygon, label)

    def test_self_intersecting_polygon_is_rejected(self):
        bowtie = {
            "type": "Polygon",
            "coordinates": [
                [
                    [-77.5, 37.5],
                    [-77.4, 37.6],
                    [-77.4, 37.5],
                    [-77.5, 37.6],
                    [-77.5, 37.5],
                ]
            ],
        }
        self.assert_rejected(bowtie, "bowtie")

    def test_zero_area_polygon_is_rejected(self):
        degenerate = {
            "type": "Polygon",
            "coordinates": [
                [[-77.5, 37.5], [-77.4, 37.5], [-77.3, 37.5], [-77.5, 37.5]]
            ],
        }
        self.assert_rejected(degenerate, "collinear")

    @override_settings(POLYGON_MAX_AREA_SQ_DEGREES=0.0001)
    def test_over_area_polygon_is_rejected(self):
        self.assert_rejected(_polygon(size=1.0), "too large")

    @override_settings(POLYGON_MAX_RINGS=1)
    def test_too_many_rings_are_rejected(self):
        two_rings = {
            "type": "Polygon",
            "coordinates": [_ring(-77.5, 37.5, 0.1), _ring(-77.48, 37.52, 0.01)],
        }
        self.assert_rejected(two_rings, "two rings")

    @override_settings(POLYGON_MAX_VERTICES_PER_RING=5)
    def test_too_many_vertices_per_ring_are_rejected(self):
        dense = _ring(-77.5, 37.5)
        dense.insert(1, [-77.495, 37.5])
        self.assert_rejected(
            {"type": "Polygon", "coordinates": [dense]}, "dense ring"
        )

    @override_settings(POLYGON_MAX_TOTAL_VERTICES=5)
    def test_total_vertex_budget_is_enforced_across_rings(self):
        two_rings = {
            "type": "Polygon",
            "coordinates": [_ring(-77.5, 37.5, 0.1), _ring(-77.48, 37.52, 0.01)],
        }
        self.assert_rejected(two_rings, "over budget")

    @override_settings(POLYGON_MAX_BODY_BYTES=64)
    def test_oversized_polygon_body_is_rejected(self):
        self.assert_rejected(_polygon(), "oversized body")

    def test_missing_polygon_field_is_rejected(self):
        response = self.post_json(self.url, {"confidence": "high"})
        self.assertEqual(response.status_code, 400)

    def test_single_polygon_multipolygon_is_accepted(self):
        response = self.submit(
            {"type": "MultiPolygon", "coordinates": [[_ring(-77.5, 37.5)]]}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(AerialGeoreference.objects.count(), 1)

    def test_polygon_is_stored_in_wgs84(self):
        self.assertEqual(self.submit(_polygon()).status_code, 200)
        self.assertEqual(AerialGeoreference.objects.get().polygon.srid, 4326)

    def test_polygon_corrections_append(self):
        self.assertEqual(self.submit(_polygon()).status_code, 200)
        self.assertEqual(self.submit(_polygon(west=-77.6)).status_code, 200)
        self.assertEqual(
            AerialGeoreference.objects.filter(image=self.aerial_image).count(), 2
        )


class AnonymousGeoreferenceRuleTests(CommunityWriteFixtureMixin, TestCase):
    """Anonymous callers may only place the first georeference on an image."""

    def submit(self, image):
        return self.post_json(
            reverse("images:georeference_image", args=[image.id]),
            {"latitude": 37.5, "longitude": -77.4, "confidence": "high"},
        )

    def test_first_anonymous_submission_is_accepted(self):
        self.assertEqual(self.submit(self.public_image).status_code, 200)

    def test_second_anonymous_submission_is_rejected(self):
        self.assertEqual(self.submit(self.public_image).status_code, 200)
        self.assertEqual(self.submit(self.public_image).status_code, 400)
        self.assertEqual(
            Georeference.objects.filter(image=self.public_image).count(), 1
        )

    def test_anonymous_submission_after_an_authenticated_one_is_rejected(self):
        Georeference.objects.create(
            image=self.public_image,
            point=Point(-77.4, 37.5, srid=4326),
            confidence="high",
            georeferenced_by=self.user,
        )
        self.assertEqual(self.submit(self.public_image).status_code, 400)

    def test_database_constraint_blocks_a_second_anonymous_row(self):
        Georeference.objects.create(
            image=self.public_image,
            point=Point(-77.4, 37.5, srid=4326),
            confidence="high",
            georeferenced_by=None,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Georeference.objects.create(
                    image=self.public_image,
                    point=Point(-77.3, 37.6, srid=4326),
                    confidence="high",
                    georeferenced_by=None,
                )


class AlbumWriteAuthorizationTests(CommunityWriteFixtureMixin, TestCase):
    """Album writes check both album ownership and image visibility."""

    def setUp(self):
        self.login()
        self.album = Album.objects.create(owner=self.user, title="Mine")
        self.foreign_album = Album.objects.create(
            owner=self.other_user, title="Theirs"
        )

    def test_cannot_add_to_someone_elses_album(self):
        response = self.post_json(
            reverse("images:add_image_to_album"),
            {"image_id": self.public_image.id, "album_id": str(self.foreign_album.id)},
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(AlbumImage.objects.exists())

    def test_cannot_add_an_inaccessible_image(self):
        for image in self.inaccessible_images:
            with self.subTest(image=image.title):
                response = self.post_json(
                    reverse("images:add_image_to_album"),
                    {"image_id": image.id, "album_id": str(self.album.id)},
                )
                self.assertEqual(response.status_code, 404)
        self.assertFalse(AlbumImage.objects.exists())

    def test_bulk_add_is_all_or_nothing(self):
        response = self.post_json(
            reverse("images:bulk_add_to_album"),
            {
                "album_id": str(self.album.id),
                "image_ids": [
                    self.public_image.id,
                    self.private_collection_image.id,
                ],
            },
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(AlbumImage.objects.exists())

    def test_bulk_add_of_authorized_images_succeeds(self):
        response = self.post_json(
            reverse("images:bulk_add_to_album"),
            {
                "album_id": str(self.album.id),
                "image_ids": [self.public_image.id, self.second_public_image.id],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["added_count"], 2)
        self.assertEqual(
            list(
                AlbumImage.objects.filter(album=self.album)
                .order_by("order")
                .values_list("order", flat=True)
            ),
            [1, 2],
        )

    def test_bulk_create_leaves_no_orphan_album_when_unauthorized(self):
        response = self.post_json(
            reverse("images:bulk_create_and_add_to_album"),
            {
                "title": "Should not exist",
                "image_ids": [self.public_image.id, self.private_source_image.id],
            },
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Album.objects.filter(title="Should not exist").exists())

    @override_settings(BULK_MAX_IMAGE_IDS=1)
    def test_bulk_request_over_the_id_cap_is_rejected(self):
        response = self.post_json(
            reverse("images:bulk_add_to_album"),
            {
                "album_id": str(self.album.id),
                "image_ids": [self.public_image.id, self.second_public_image.id],
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_bulk_request_with_no_ids_is_rejected(self):
        response = self.post_json(
            reverse("images:bulk_add_to_album"),
            {"album_id": str(self.album.id), "image_ids": []},
        )
        self.assertEqual(response.status_code, 400)

    def test_public_album_cannot_gain_a_private_image(self):
        self.client.force_login(self.staff)
        staff_album = Album.objects.create(
            owner=self.staff, title="Staff public", public=True
        )
        response = self.post_json(
            reverse("images:add_image_to_album"),
            {
                "image_id": self.private_collection_image.id,
                "album_id": str(staff_album.id),
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(AlbumImage.objects.exists())

    def test_private_album_holding_a_private_image_cannot_be_published(self):
        self.client.force_login(self.staff)
        staff_album = Album.objects.create(owner=self.staff, title="Staging")
        AlbumImage.objects.create(
            album=staff_album, image=self.private_collection_image, order=1
        )

        response = self.post_json(
            reverse("images:toggle_album_public", args=[staff_album.id]),
            {"public": True},
        )
        self.assertEqual(response.status_code, 400)
        staff_album.refresh_from_db()
        self.assertFalse(staff_album.public)

    def test_public_album_of_public_images_is_allowed(self):
        AlbumImage.objects.create(
            album=self.album, image=self.public_image, order=1
        )
        response = self.post_json(
            reverse("images:toggle_album_public", args=[self.album.id]),
            {"public": True},
        )
        self.assertEqual(response.status_code, 200)
        self.album.refresh_from_db()
        self.assertTrue(self.album.public)

    def test_creating_a_public_album_with_a_private_image_is_rejected(self):
        self.client.force_login(self.staff)
        response = self.post_json(
            reverse("images:bulk_create_and_add_to_album"),
            {
                "title": "Leaky",
                "is_public": True,
                "image_ids": [self.private_collection_image.id],
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Album.objects.filter(title="Leaky").exists())


class AnonymousGeoreferenceConcurrencyTests(TransactionTestCase):
    """Two simultaneous anonymous submissions cannot both land.

    Uses TransactionTestCase and real threads so each request runs on its own
    database connection, which is the only way the row lock is exercised.
    """

    def setUp(self):
        source = Source.objects.create(
            name="Concurrency source",
            slug="sa01-concurrency",
            url="https://example.com/concurrency",
            description="",
        )
        collection = Collection.objects.create(
            source=source, name="Concurrency", slug="sa01-concurrency"
        )
        self.image = Image.objects.create(
            collection=collection,
            title="Concurrency",
            permalink="https://img.example.com/concurrency.jpg",
        )

    def test_only_one_of_two_simultaneous_anonymous_submissions_wins(self):
        url = reverse("images:georeference_image", args=[self.image.id])
        payload = json.dumps(
            {"latitude": 37.5, "longitude": -77.4, "confidence": "high"}
        )
        barrier = threading.Barrier(2)
        statuses = []
        lock = threading.Lock()

        def submit():
            try:
                barrier.wait(timeout=10)
                response = Client().post(
                    url, data=payload, content_type="application/json"
                )
                with lock:
                    statuses.append(response.status_code)
            finally:
                connection.close()

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(sorted(statuses), [200, 400])
        self.assertEqual(Georeference.objects.filter(image=self.image).count(), 1)


class ImagePolicyTests(CommunityWriteFixtureMixin, TestCase):
    """Direct coverage of the policy querysets the views delegate to."""

    def test_public_images_excludes_private_sources_and_collections(self):
        self.assertEqual(
            set(policies.public_images().values_list("id", flat=True)),
            {
                self.public_image.id,
                self.second_public_image.id,
                self.duplicate_image.id,
                self.will_not_georef_image.id,
                self.aerial_image.id,
            },
        )

    def test_accessible_images_for_staff_includes_private(self):
        accessible = policies.accessible_images_for(self.staff)
        self.assertIn(self.private_collection_image, accessible)
        self.assertIn(self.private_source_image, accessible)

    def test_editable_images_excludes_duplicates_for_everyone(self):
        for user in (self.user, self.staff):
            with self.subTest(user=user.username):
                self.assertNotIn(
                    self.duplicate_image, policies.editable_images_for(user)
                )

    def test_representable_images_ignores_staff_status(self):
        representable = policies.representable_images()
        self.assertIn(self.public_image, representable)
        self.assertNotIn(self.private_collection_image, representable)
        self.assertNotIn(self.duplicate_image, representable)

    def test_parse_image_ids_normalizes_and_deduplicates(self):
        self.assertEqual(policies.parse_image_ids([3, "1", 3, 2]), [3, 1, 2])

    def test_parse_image_ids_rejects_bad_input(self):
        for value in ([], "12", [None], [True], [{}], [1.5e400]):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    policies.parse_image_ids(value)

    @override_settings(BULK_MAX_IMAGE_IDS=2)
    def test_parse_image_ids_enforces_the_cap(self):
        with self.assertRaises(ValueError):
            policies.parse_image_ids([1, 2, 3])

    def test_get_images_or_404_is_all_or_nothing(self):
        queryset = policies.editable_images_for(self.user)
        self.assertEqual(
            len(
                policies.get_images_or_404(
                    queryset, [self.public_image.id, self.second_public_image.id]
                )
            ),
            2,
        )
        with self.assertRaises(Http404):
            policies.get_images_or_404(
                queryset, [self.public_image.id, self.private_source_image.id]
            )


# ---------------------------------------------------------------------------
# In-view search: the search page's HTML endpoint
# ---------------------------------------------------------------------------


class InViewSearchPageTests(TestCase):
    """``/api/v1/search/in-view/``, the endpoint the search page itself calls.

    The API tests in ``api/tests.py`` cover the query semantics through
    ``/api/v2/``. What is only reachable here is the rendered response: the
    distance badge on each card, and the geometry the page needs to draw those
    same results on its picker map.

    Both have already gone wrong once. ``{% include with %}`` binds a *missing*
    dict key to the empty string rather than ``None``, so an omitted
    ``similarity_score`` rendered an empty "% match" badge where the distance
    should have been.
    """

    ORIGIN_LAT = 37.53
    ORIGIN_LON = -77.44

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="osm_9001", password="test")
        cls.source = Source.objects.create(
            name="Src",
            slug="in-view-src",
            url="https://example.com",
            description="",
            public=True,
        )
        cls.collection = Collection.objects.create(
            source=cls.source,
            name="Col",
            slug="in-view-col",
            url="https://example.com",
            public=True,
        )

        # 111 m north of the origin, no direction recorded.
        cls.img_close = cls._make_image("Close photo")
        cls.georef_close = cls._georeference(cls.img_close, -77.44, 37.5310)

        # 1.1 km north, looking due south — straight back at the origin.
        cls.img_far = cls._make_image("Distant photo")
        cls.georef_far = cls._georeference(cls.img_far, -77.44, 37.5400, direction=180)

    @classmethod
    def _make_image(cls, title):
        img = Image.objects.create(
            collection=cls.collection,
            title=title,
            permalink=f"https://img.example.com/{slugify(title)}.jpg",
        )
        img.refresh_from_db()
        return img

    @classmethod
    def _georeference(cls, image, longitude, latitude, direction=None):
        return Georeference.objects.create(
            image=image,
            point=Point(longitude, latitude, srid=4326),
            direction=direction,
            confidence="medium",
            georeferenced_by=cls.user,
        )

    def _get(self, **params):
        return self.client.get(
            "/api/v1/search/in-view/",
            {"lat": self.ORIGIN_LAT, "lon": self.ORIGIN_LON, **params},
        )

    def _html(self, **params):
        resp = self._get(format="html", **params)
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def test_json_orders_nearest_first(self):
        resp = self._get()
        self.assertEqual(resp.status_code, 200)
        ids = [r["id"] for r in resp.json()["results"]]
        self.assertEqual(ids, [self.img_close.id, self.img_far.id])

    def test_invalid_coordinates_rejected(self):
        resp = self.client.get("/api/v1/search/in-view/", {"lat": "nan", "lon": "-77"})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()["success"])

    def test_html_renders_distance_badge(self):
        html = self._html()
        self.assertIn("111 m", html)
        self.assertIn("1.1 km", html)

    def test_html_does_not_render_a_similarity_badge(self):
        # The regression: an empty "% match" badge in the distance's place.
        self.assertNotIn("% match", self._html())

    def test_html_embeds_result_geometry_for_the_map(self):
        html = self._html()
        match = re.search(
            r'<script id="search-result-points" type="application/json">(.*?)</script>',
            html,
            re.S,
        )
        self.assertIsNotNone(match, "result geometry missing from the response")

        points = json.loads(match.group(1))
        # One point per card, in the order the cards appear, so the map and
        # the grid cannot disagree about what was found.
        self.assertEqual(
            points,
            [
                {
                    "id": self.img_close.id,
                    "lat": 37.5310,
                    "lng": -77.44,
                    "direction": None,
                },
                {
                    "id": self.img_far.id,
                    "lat": 37.5400,
                    "lng": -77.44,
                    "direction": 180,
                },
            ],
        )

    def test_text_search_html_embeds_no_geometry(self):
        # The partial is shared; only modes that plot their results send points.
        resp = self.client.get(
            "/api/v1/search/text/", {"q": "photo", "format": "html"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("search-result-points", resp.content.decode())


class HomeSubjectsOrderAdminFormTests(TestCase):
    """The settings form that orders the homepage subjects band by hand.

    The widget (images.admin.HomeSubjectsOrderWidget) lists the featured
    photograph's subjects in the order the band reads them and posts the
    result back as comma-separated subject ids.
    """

    @classmethod
    def setUpTestData(cls):
        source = Source.objects.create(name="Src", slug="ord-src", public=True)
        collection = Collection.objects.create(
            name="Coll", slug="ord-coll", source=source, public=True
        )
        cls.image = Image.objects.create(
            collection=collection,
            title="Broad Street",
            permalink="https://img.example.com/broad.jpg",
            thumbnail="https://img.example.com/broad-thumb.jpg",
        )
        # bulk_create, because WikidataItem.save() fetches the item's
        # metadata from Wikidata and a test has no business on the network.
        items = WikidataItem.objects.bulk_create(
            [
                WikidataItem(wikidata_id="Q901", title="Main Street Station"),
                WikidataItem(wikidata_id="Q902", title="Old City Hall"),
            ]
        )
        cls.station, cls.hall = (
            SubjectMapping.objects.create(
                image=cls.image,
                subject=Subject.objects.create(title=item.title, wikidata_item=item),
                order=order,
            )
            for order, item in enumerate(items)
        )

    def setUp(self):
        # SiteSettings.load() memoizes the row in the process-local cache,
        # which outlives any one test and is not rolled back with the
        # database.
        cache.clear()
        self.addCleanup(cache.clear)

    def _settings(self, **fields):
        fields.setdefault("home_subjects_image", self.image)
        obj, _ = SiteSettings.objects.update_or_create(pk=1, defaults=fields)
        return obj

    def test_widget_lists_the_featured_photographs_subjects(self):
        form = SiteSettingsAdminForm(instance=self._settings())
        rendered = str(form["home_subjects_order"])
        self.assertIn("Main Street Station", rendered)
        self.assertIn("Old City Hall", rendered)
        # Nothing pinned yet, so the band goes on following the image page.
        self.assertIn('value=""', rendered)

    def test_widget_lists_them_in_a_standing_manual_order(self):
        form = SiteSettingsAdminForm(
            instance=self._settings(
                home_subjects_order=[self.hall.subject_id, self.station.subject_id]
            )
        )
        rendered = str(form["home_subjects_order"])
        self.assertLess(
            rendered.index("Old City Hall"), rendered.index("Main Street Station")
        )

    def test_widget_explains_itself_when_there_is_nothing_to_order(self):
        rendered = str(
            SiteSettingsAdminForm(instance=self._settings(home_subjects_image=None))[
                "home_subjects_order"
            ]
        )
        self.assertIn("Pick a photograph above", rendered)
        # No input either: an omitted value leaves the stored order alone.
        self.assertNotIn("home_subjects_order", rendered)

    def test_posted_order_is_saved(self):
        settings_row = self._settings()
        form = SiteSettingsAdminForm(
            instance=settings_row,
            data={
                "site_title": "Yesterdays",
                "site_subtitle": "A community effort",
                "footer_content": "<p>Footer</p>",
                "admin_email": "admin@example.com",
                "default_map_longitude": -77.4,
                "default_map_latitude": 37.5,
                "default_map_zoom": 12,
                "default_search_bbox_west": -78,
                "default_search_bbox_south": 37,
                "default_search_bbox_east": -77,
                "default_search_bbox_north": 38,
                "home_feed_item_count": 5,
                "home_subjects_image": self.image.pk,
                "home_subjects_order": f"{self.hall.subject_id},{self.station.subject_id}",
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(
            SiteSettings.objects.get(pk=1).home_subjects_order,
            [self.hall.subject_id, self.station.subject_id],
        )

    def test_ids_the_photograph_does_not_carry_are_discarded(self):
        form = SiteSettingsAdminForm(
            instance=self._settings(),
            data={"home_subjects_order": f"{self.station.subject_id},999999,oops"},
        )
        form.is_valid()
        self.assertEqual(
            form.cleaned_data["home_subjects_order"], [self.station.subject_id]
        )
