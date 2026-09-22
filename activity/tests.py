from datetime import timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.contrib.gis.geos import Point, Polygon
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from images.models import (
    AerialGeoreference,
    AerialGeoreferenceValidation,
    Collection,
    Comment,
    Georeference,
    GeoreferenceValidation,
    Image,
    SiteSettings,
    Source,
    SubjectMappingActivity,
)
from regions.context_processors import REGION_COOKIE_NAME
from regions.models import Region, RegionAncestor
from subjects.models import Subject, WikidataItem

from .models import (
    CollectionIntroduction,
    GeoreferenceGroup,
    GeoreferenceGroupMember,
    SitewideMilestone,
    SubjectIntroduction,
    SubjectMappingActivityGroup,
    UserMilestone,
)
from .views import get_activity_events


TEST_POINT = Point(-77.44, 37.54, srid=4326)
TEST_POLYGON = Polygon(
    (
        (-77.45, 37.53),
        (-77.43, 37.53),
        (-77.43, 37.55),
        (-77.45, 37.55),
        (-77.45, 37.53),
    ),
    srid=4326,
)


class ActivityRegionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="mapper", password="test")
        cls.validator = User.objects.create_user(username="validator", password="test")

        state_item, city_item, other_item = WikidataItem.objects.bulk_create(
            [
                WikidataItem(wikidata_id="QACT1", title="Virginia"),
                WikidataItem(wikidata_id="QACT2", title="Richmond"),
                WikidataItem(wikidata_id="QACT3", title="Elsewhere"),
            ]
        )
        cls.state = Region.objects.create(
            short_name="Virginia",
            long_name="Virginia",
            slug="activity-virginia",
            wikidata_item=state_item,
            wikidata_coordinate_location=TEST_POINT,
        )
        cls.city = Region.objects.create(
            short_name="Richmond",
            long_name="Richmond, Virginia",
            slug="activity-richmond",
            wikidata_item=city_item,
            wikidata_coordinate_location=TEST_POINT,
        )
        cls.other = Region.objects.create(
            short_name="Elsewhere",
            long_name="Elsewhere",
            slug="activity-elsewhere",
            wikidata_item=other_item,
            wikidata_coordinate_location=TEST_POINT,
        )
        RegionAncestor.objects.create(region=cls.city, ancestor=state_item)

        cls.city_source = Source.objects.create(
            name="Richmond Source",
            slug="activity-richmond-source",
            url="https://example.com/richmond",
            description="",
            region=cls.city,
        )
        cls.other_source = Source.objects.create(
            name="Other Source",
            slug="activity-other-source",
            url="https://example.com/other",
            description="",
            region=cls.other,
        )
        cls.regionless_source = Source.objects.create(
            name="Regionless Source",
            slug="activity-regionless-source",
            url="https://example.com/regionless",
            description="",
        )
        cls.city_source_collection = Collection.objects.create(
            source=cls.city_source,
            name="Source Region Collection",
            slug="source-region",
        )
        cls.city_collection = Collection.objects.create(
            source=cls.other_source,
            name="Collection Region Collection",
            slug="collection-region",
            region=cls.city,
        )
        cls.other_collection = Collection.objects.create(
            source=cls.other_source,
            name="Other Collection",
            slug="other",
        )
        cls.regionless_collection = Collection.objects.create(
            source=cls.regionless_source,
            name="Regionless Collection",
            slug="regionless",
        )

        cls.source_region_image = cls._make_image(
            cls.city_source_collection, "Source Region Image"
        )
        cls.collection_region_image = cls._make_image(
            cls.city_collection, "Collection Region Image"
        )
        cls.image_region_image = cls._make_image(
            cls.other_collection, "Image Region Image", region=cls.city
        )
        cls.other_image = cls._make_image(cls.other_collection, "Other Image")
        cls.regionless_image = cls._make_image(
            cls.regionless_collection, "Regionless Image"
        )
        cls.image_override_other = cls._make_image(
            cls.city_source_collection, "Other Override Image", region=cls.other
        )

        cls.comments = {}
        for image in (
            cls.source_region_image,
            cls.collection_region_image,
            cls.image_region_image,
            cls.other_image,
            cls.regionless_image,
            cls.image_override_other,
        ):
            cls.comments[image.pk] = Comment.objects.create(
                image=image,
                text=f"Comment on {image.title}",
                commented_by=cls.user,
            )

    @classmethod
    def _make_image(cls, collection, title, region=None):
        return Image.objects.create(
            collection=collection,
            region=region,
            title=title,
            permalink=f"https://img.example.com/{title.lower().replace(' ', '-')}.jpg",
        )

    def _objects(self, event_type, region=None, before=None):
        return [
            event
            for returned_type, event, _timestamp in get_activity_events(
                before=before,
                event_types={event_type},
                region=region,
                limit=100,
            )
            if returned_type == event_type
        ]

    def test_comments_follow_effective_region_precedence_and_descendants(self):
        expected_ids = {
            self.source_region_image.pk,
            self.collection_region_image.pk,
            self.image_region_image.pk,
        }

        city_comments = self._objects("comment", self.city)
        state_comments = self._objects("comment", self.state)
        global_comments = self._objects("comment")

        self.assertEqual({comment.image_id for comment in city_comments}, expected_ids)
        self.assertEqual({comment.image_id for comment in state_comments}, expected_ids)
        self.assertEqual(len(global_comments), 6)
        self.assertNotIn(
            self.image_override_other.pk,
            {comment.image_id for comment in city_comments},
        )

    def test_georeference_group_uses_only_matching_members_and_timestamp(self):
        city_time = timezone.now() - timedelta(hours=4)
        other_time = timezone.now() - timedelta(hours=1)
        group = GeoreferenceGroup.objects.create(
            user=self.user,
            started_at=city_time,
            ended_at=other_time,
            count=2,
        )
        city_georeference = Georeference.objects.create(
            image=self.source_region_image,
            point=TEST_POINT,
            georeferenced_by=self.user,
        )
        other_georeference = AerialGeoreference.objects.create(
            image=self.other_image,
            polygon=TEST_POLYGON,
            georeferenced_by=self.user,
        )
        GeoreferenceGroupMember.objects.create(
            group=group,
            georeference=city_georeference,
            added_at=city_time,
        )
        GeoreferenceGroupMember.objects.create(
            group=group,
            aerial_georeference=other_georeference,
            added_at=other_time,
        )

        regional_event = get_activity_events(
            event_types={"group"}, region=self.state, limit=10
        )[0]
        regional_group = regional_event[1]
        self.assertEqual(regional_group.feed_count, 1)
        self.assertEqual(
            [member.image.pk for member in regional_group.feed_members],
            [self.source_region_image.pk],
        )
        self.assertEqual(regional_event[2], city_time)

        other_event = get_activity_events(
            event_types={"group"}, region=self.other, limit=10
        )[0]
        self.assertEqual(other_event[1].feed_count, 1)
        self.assertEqual(
            [member.image.pk for member in other_event[1].feed_members],
            [self.other_image.pk],
        )
        self.assertEqual(other_event[2], other_time)

        before = city_time + timedelta(minutes=30)
        self.assertEqual(
            len(
                get_activity_events(
                    before=before,
                    event_types={"group"},
                    region=self.state,
                    limit=10,
                )
            ),
            1,
        )
        self.assertEqual(
            get_activity_events(before=before, event_types={"group"}, limit=10),
            [],
        )

        self.client.cookies[REGION_COOKIE_NAME] = self.state.slug
        response = self.client.get(reverse("activity:feed"), {"types": "group"})
        self.assertContains(response, "Source Region Image")
        self.assertNotContains(response, "Other Image")

    def test_subject_group_uses_only_matching_members_and_timestamp(self):
        subject_item = WikidataItem.objects.bulk_create(
            [WikidataItem(wikidata_id="QACT4", title="Test Subject")]
        )[0]
        subject = Subject.objects.create(
            title="Test Subject",
            slug="activity-test-subject",
            wikidata_item=subject_item,
        )
        city_time = timezone.now() - timedelta(hours=5)
        other_time = timezone.now() - timedelta(hours=2)
        group = SubjectMappingActivityGroup.objects.create(
            user=self.user,
            subject=subject,
            action=SubjectMappingActivity.ACTION_ADDED,
            started_at=city_time,
            ended_at=other_time,
            count=2,
        )
        city_activity = SubjectMappingActivity.objects.create(
            user=self.user,
            image=self.collection_region_image,
            subject=subject,
            action=SubjectMappingActivity.ACTION_ADDED,
            group=group,
        )
        other_activity = SubjectMappingActivity.objects.create(
            user=self.user,
            image=self.other_image,
            subject=subject,
            action=SubjectMappingActivity.ACTION_ADDED,
            group=group,
        )
        SubjectMappingActivity.objects.filter(pk=city_activity.pk).update(
            created_at=city_time
        )
        SubjectMappingActivity.objects.filter(pk=other_activity.pk).update(
            created_at=other_time
        )

        event = get_activity_events(
            event_types={"subject"}, region=self.state, limit=10
        )[0]
        scoped_group = event[1]
        self.assertEqual(scoped_group.feed_count, 1)
        self.assertEqual(
            [member.image_id for member in scoped_group.feed_members],
            [self.collection_region_image.pk],
        )
        self.assertEqual(event[2], city_time)

    def test_other_image_backed_events_are_region_scoped(self):
        city_point = Georeference.objects.create(
            image=self.source_region_image,
            point=TEST_POINT,
            georeferenced_by=self.user,
        )
        other_aerial = AerialGeoreference.objects.create(
            image=self.other_image,
            polygon=TEST_POLYGON,
            georeferenced_by=self.user,
        )
        city_validation = GeoreferenceValidation.objects.create(
            georeference=city_point,
            validated_by=self.validator,
            validation="correct",
        )
        other_validation = AerialGeoreferenceValidation.objects.create(
            georeference=other_aerial,
            validated_by=self.validator,
            validation="correct",
        )

        subject_items = WikidataItem.objects.bulk_create(
            [
                WikidataItem(wikidata_id="QACT5", title="City Subject"),
                WikidataItem(wikidata_id="QACT6", title="Other Subject"),
                WikidataItem(wikidata_id="QACT7", title="Unplaced Subject"),
            ]
        )
        subjects = [
            Subject.objects.create(
                title=item.title,
                slug=f"activity-{item.wikidata_id.lower()}",
                wikidata_item=item,
            )
            for item in subject_items
        ]
        introductions = [
            SubjectIntroduction.objects.create(
                subject=subjects[0],
                user=self.user,
                image=self.source_region_image,
                created_at=timezone.now(),
            ),
            SubjectIntroduction.objects.create(
                subject=subjects[1],
                user=self.user,
                image=self.other_image,
                created_at=timezone.now(),
            ),
            SubjectIntroduction.objects.create(
                subject=subjects[2],
                user=self.user,
                image=None,
                created_at=timezone.now(),
            ),
        ]
        city_collection_intros = {
            CollectionIntroduction.objects.create(
                collection=self.city_source_collection, created_at=timezone.now()
            ).pk,
            CollectionIntroduction.objects.create(
                collection=self.city_collection, created_at=timezone.now()
            ).pk,
        }
        CollectionIntroduction.objects.create(
            collection=self.other_collection, created_at=timezone.now()
        )

        self.assertEqual(
            [event.pk for event in self._objects("validation", self.state)],
            [city_validation.pk],
        )
        self.assertEqual(
            [event.pk for event in self._objects("new_subject", self.state)],
            [introductions[0].pk],
        )
        self.assertEqual(
            {event.pk for event in self._objects("new_collection", self.state)},
            city_collection_intros,
        )
        self.assertEqual(
            [event.pk for event in self._objects("validation", self.other)],
            [other_validation.pk],
        )

    def test_milestones_remain_global_in_a_regional_feed(self):
        user_milestone = UserMilestone.objects.create(
            user=self.user, count=5, reached_at=timezone.now()
        )
        sitewide_milestone = SitewideMilestone.objects.create(
            count=100, reached_at=timezone.now()
        )

        self.assertEqual(self._objects("milestone", self.state), [user_milestone])
        self.assertEqual(self._objects("sitewide", self.state), [sitewide_milestone])

    def test_home_full_feed_and_ajax_use_the_region_cookie(self):
        self.addCleanup(cache.delete, "site_settings")
        settings = SiteSettings.load()
        settings.home_feed_show_georeferences = False
        settings.home_feed_show_comments = True
        settings.home_feed_show_user_milestones = False
        settings.home_feed_show_site_milestones = False
        settings.home_feed_show_validations = False
        settings.home_feed_show_subjects = False
        settings.home_feed_show_new_subjects = False
        settings.home_feed_show_new_collections = False
        settings.home_feed_item_count = 20
        settings.save()

        self.client.cookies[REGION_COOKIE_NAME] = self.state.slug
        home_response = self.client.get(reverse("home"))
        home_images = {
            event.image_id
            for event_type, event, _timestamp in home_response.context[
                "activity_events"
            ]
            if event_type == "comment"
        }
        self.assertEqual(
            home_images,
            {
                self.source_region_image.pk,
                self.collection_region_image.pk,
                self.image_region_image.pk,
            },
        )
        self.assertContains(home_response, "Recent Activity in Virginia")

        feed_response = self.client.get(reverse("activity:feed"), {"types": "comment"})
        self.assertContains(feed_response, "Activity in Virginia")
        self.assertContains(feed_response, "Source Region Image")
        self.assertNotContains(feed_response, "Other Image")

        ajax_response = self.client.get(
            reverse("activity:feed"),
            {"types": "comment"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertContains(ajax_response, "Source Region Image")
        self.assertNotContains(ajax_response, "Other Image")

    def test_api_feed_remains_sitewide_when_cookie_is_set(self):
        self.client.cookies[REGION_COOKIE_NAME] = self.state.slug
        response = self.client.get("/api/v2/activity/?types=comment&limit=100")
        returned_image_ids = {event["data"]["image_id"] for event in response.json()}

        self.assertIn(self.source_region_image.pk, returned_image_ids)
        self.assertIn(self.other_image.pk, returned_image_ids)
        self.assertIn(self.regionless_image.pk, returned_image_ids)
