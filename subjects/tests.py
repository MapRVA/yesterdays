import importlib
import json
import re
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import pyoxigraph
import requests
from django.contrib.admin.sites import AdminSite
from django.contrib.admin.utils import flatten_fieldsets
from django.contrib.auth.models import User
from django.contrib.gis.geos import Point, Polygon
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, connection, transaction
from django.test import (
    Client,
    RequestFactory,
    SimpleTestCase,
    TestCase,
    override_settings,
)
from django.urls import reverse
from django.utils import timezone

from images.models import (
    Collection,
    CollectionEmbeddingStats,
    Image,
    Source,
    SubjectMapping,
)
from images.tasks import (
    refresh_collection_embedding_stats,
    refresh_next_collection_embedding_stats,
)

from .admin import SubjectAdmin
from .models import (
    OsmElement,
    Subject,
    SubjectAncestor,
    WikidataItem,
    _preferred_wikidata_label,
)
from .similarity import build_subject_query_embedding
from .sparql_safety import UnsafeSparqlInput, looks_like_pid, looks_like_qid
from .tasks import (
    PostpassResultTruncated,
    fetch_osm_features,
    get_next_requested_wikidata_item,
    get_next_stale_wikidata_item,
    sync_osm_elements,
)
from .views import _MAX_AUTOCOMPLETE_QUERY_LEN
from .wikidata_closure import (
    ClosureLoadError,
    apply_closure,
    build_graph_payload,
    closure_query,
    extract_seed_metadata,
    parse_closure,
)

DIM = 768

SQUARE = {
    "type": "Polygon",
    "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]],
}


def _subject(title, slug, qid):
    """A Subject with the WikidataItem its NOT NULL FK demands."""
    return Subject.objects.create(
        title=title,
        slug=slug,
        wikidata_item=WikidataItem.objects.create(wikidata_id=qid, title=title),
    )


def _feature(osm_type, osm_id, geometry=None):
    """A Postpass feature as fetch_osm_features hands it to the sync."""
    return {
        "type": "Feature",
        "properties": {"osm_id": osm_id, "osm_type": osm_type, "tags": {}},
        "geometry": geometry or {"type": "Point", "coordinates": [-117.46, 34.10]},
    }


class FetchOsmFeaturesTests(SimpleTestCase):
    def test_query_dedupes_per_element_and_fetches_one_past_the_cap(self):
        session = Mock()
        session.post.return_value.json.return_value = {"features": []}

        with override_settings(METADATA_REFRESH_POSTPASS_MAX_FEATURES=25):
            fetch_osm_features(session, "Q491128")

        sql = session.post.call_args.kwargs["data"]["data"]
        self.assertIn("DISTINCT ON (osm_type, osm_id)", sql)
        self.assertIn("""tags @> '{"wikidata": "Q491128"}'::jsonb""", sql)
        self.assertIn("ST_Dimension(geom) DESC", sql)
        self.assertIn("LIMIT 26", sql)
        # ->> can't use Postpass's GIN indexes; a bbox would defeat the point
        self.assertNotIn("->>", sql)
        self.assertNotIn("&&", sql)

    def test_more_matches_than_the_cap_is_refused(self):
        session = Mock()
        session.post.return_value.json.return_value = {
            "features": [_feature("W", i) for i in range(3)]
        }

        with override_settings(METADATA_REFRESH_POSTPASS_MAX_FEATURES=2):
            with self.assertRaises(PostpassResultTruncated):
                fetch_osm_features(session, "Q491128")

    def test_exactly_the_cap_is_accepted(self):
        session = Mock()
        features = [_feature("W", i) for i in range(2)]
        session.post.return_value.json.return_value = {"features": features}

        with override_settings(METADATA_REFRESH_POSTPASS_MAX_FEATURES=2):
            self.assertEqual(fetch_osm_features(session, "Q491128"), features)

    def test_response_without_features_key_is_an_error(self):
        session = Mock()
        session.post.return_value.json.return_value = {"error": "nope"}

        with self.assertRaises(ValueError):
            fetch_osm_features(session, "Q491128")

    def test_invalid_qid_is_rejected_before_request(self):
        session = Mock()
        for qid in ("", "Q", "q42", "Q42' OR TRUE --"):
            with self.subTest(qid=qid), self.assertRaises(ValueError):
                fetch_osm_features(session, qid)
        session.post.assert_not_called()

    def test_http_failure_does_not_become_an_empty_result(self):
        session = Mock()
        response = session.post.return_value
        response.raise_for_status.side_effect = requests.HTTPError("503 unavailable")

        with self.assertRaises(requests.HTTPError):
            fetch_osm_features(session, "Q491128")

        response.json.assert_not_called()

    def test_timeout_does_not_become_an_empty_result(self):
        session = Mock()
        session.post.side_effect = requests.Timeout("Postpass timed out")

        with self.assertRaises(requests.Timeout):
            fetch_osm_features(session, "Q491128")

    def test_features_are_returned_without_database_access(self):
        session = Mock()
        features = [
            {
                "type": "Feature",
                "properties": {"osm_id": 123},
                "geometry": {"type": "Point", "coordinates": [-117.46, 34.10]},
            }
        ]
        session.post.return_value.json.return_value = {"features": features}

        result = fetch_osm_features(
            session, "Q491128", postpass_url="https://postpass.example/api", timeout=12
        )

        self.assertEqual(result, features)
        self.assertEqual(session.post.call_args.args, ("https://postpass.example/api",))
        self.assertEqual(session.post.call_args.kwargs["timeout"], 12)


class SyncOsmElementsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.subject = _subject("Fontana", "fontana", "Q491128")
        cls.other = _subject("Elsewhere", "elsewhere", "Q2")

    def _legacy(self, osm_id, subject=None):
        return OsmElement.objects.create(
            osm_type=None,
            osm_id=osm_id,
            subject=subject or self.subject,
            geometry=Point(0, 0, srid=4326),
        )

    def test_creates_typed_rows(self):
        counts = sync_osm_elements(
            self.subject, [_feature("N", 1), _feature("R", 2, SQUARE)]
        )

        self.assertEqual(counts, (2, 0, 0))
        self.assertEqual(
            set(self.subject.osm_elements.values_list("osm_type", "osm_id")),
            {("N", 1), ("R", 2)},
        )

    def test_updates_geometry_and_deletes_stale(self):
        OsmElement.objects.create(
            osm_type="N", osm_id=1, subject=self.subject, geometry=Point(0, 0, srid=4326)
        )
        OsmElement.objects.create(
            osm_type="W", osm_id=9, subject=self.subject, geometry=Point(0, 0, srid=4326)
        )

        counts = sync_osm_elements(self.subject, [_feature("N", 1)])

        self.assertEqual(counts, (0, 1, 1))
        element = self.subject.osm_elements.get()
        self.assertEqual((element.osm_type, element.osm_id), ("N", 1))
        self.assertAlmostEqual(element.geometry.x, -117.46)

    def test_empty_response_deletes_everything(self):
        self._legacy(1)
        OsmElement.objects.create(
            osm_type="W", osm_id=2, subject=self.subject, geometry=Point(0, 0, srid=4326)
        )

        self.assertEqual(sync_osm_elements(self.subject, []), (0, 0, 2))
        self.assertFalse(self.subject.osm_elements.exists())

    def test_claims_own_untyped_row_in_place(self):
        legacy = self._legacy(1)

        self.assertEqual(sync_osm_elements(self.subject, [_feature("W", 1)]), (0, 1, 0))

        legacy.refresh_from_db()
        self.assertEqual(legacy.osm_type, "W")
        self.assertEqual(OsmElement.objects.filter(osm_id=1).count(), 1)

    def test_leaves_other_subjects_untyped_rows_alone(self):
        legacy = self._legacy(1, subject=self.other)

        self.assertEqual(sync_osm_elements(self.subject, [_feature("W", 1)]), (1, 0, 0))

        legacy.refresh_from_db()
        self.assertIsNone(legacy.osm_type)
        self.assertEqual(legacy.subject, self.other)
        # Typed and untyped rows for one id coexist until the other refreshes
        self.assertEqual(OsmElement.objects.filter(osm_id=1).count(), 2)

    def test_typed_row_elsewhere_wins_over_own_untyped_row(self):
        typed = OsmElement.objects.create(
            osm_type="W", osm_id=1, subject=self.other, geometry=Point(0, 0, srid=4326)
        )
        legacy = self._legacy(1)

        self.assertEqual(sync_osm_elements(self.subject, [_feature("W", 1)]), (0, 1, 1))

        typed.refresh_from_db()
        self.assertEqual(typed.subject, self.subject)
        self.assertFalse(OsmElement.objects.filter(pk=legacy.pk).exists())

    def test_never_creates_untyped_rows(self):
        sync_osm_elements(self.subject, [_feature("N", 1), _feature("W", 2)])

        self.assertFalse(OsmElement.objects.filter(osm_type__isnull=True).exists())


class OsmElementConstraintTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.subject = _subject("Fontana", "fontana", "Q491128")

    def _create(self, osm_type, osm_id):
        return OsmElement.objects.create(
            osm_type=osm_type,
            osm_id=osm_id,
            subject=self.subject,
            geometry=Point(0, 0, srid=4326),
        )

    def test_same_id_with_different_types_coexists(self):
        self._create("W", 6411727)
        self._create("R", 6411727)
        self._create(None, 6411727)

        self.assertEqual(OsmElement.objects.filter(osm_id=6411727).count(), 3)

    def test_same_typed_element_twice_is_rejected(self):
        self._create("W", 1)

        with self.assertRaises(IntegrityError), transaction.atomic():
            self._create("W", 1)

    def test_same_untyped_id_twice_is_rejected(self):
        self._create(None, 1)

        with self.assertRaises(IntegrityError), transaction.atomic():
            self._create(None, 1)


class OsmTypeMigrationSqlTests(TestCase):
    """The data statements in 0021, run against rows shaped like the legacy ones."""

    def test_points_become_nodes_and_subjects_go_to_the_front_of_the_rotation(self):
        migration = importlib.import_module("subjects.migrations.0021_osmelement_osm_type")
        subject = _subject("Fontana", "fontana", "Q491128")
        subject.osm_last_checked = timezone.now()
        subject.save(update_fields=["osm_last_checked"])
        point = OsmElement.objects.create(
            osm_type=None, osm_id=1, subject=subject, geometry=Point(0, 0, srid=4326)
        )
        polygon = OsmElement.objects.create(
            osm_type=None,
            osm_id=2,
            subject=subject,
            geometry=Polygon(((0, 0), (0, 1), (1, 1), (1, 0), (0, 0)), srid=4326),
        )

        with connection.cursor() as cursor:
            cursor.execute(migration.INFER_NODE_TYPE_SQL)
            cursor.execute(migration.RESET_OSM_LAST_CHECKED_SQL)

        point.refresh_from_db()
        polygon.refresh_from_db()
        subject.refresh_from_db()
        self.assertEqual(point.osm_type, "N")
        self.assertIsNone(polygon.osm_type)
        self.assertEqual(subject.osm_last_checked.year, 1970)


def _unit(seed):
    rng = np.random.default_rng(seed)
    v = rng.normal(size=DIM)
    return v / np.linalg.norm(v)


def _cos(a, b):
    a, b = np.asarray(a), np.asarray(b)
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))


class BuildSubjectQueryEmbeddingTests(TestCase):
    """The collection-centered query builder (subjects/similarity.py)."""

    @classmethod
    def setUpTestData(cls):
        cls.source = Source.objects.create(
            name="Src", slug="src", url="https://example.com", description=""
        )
        cls.coll_a = Collection.objects.create(
            source=cls.source, name="A", slug="a", url="https://example.com"
        )
        cls.coll_b = Collection.objects.create(
            source=cls.source, name="B", slug="b", url="https://example.com"
        )
        # Orthogonal-ish directions for "content" and two collection "styles"
        cls.content = _unit(1)
        cls.style_a = _unit(2)
        cls.style_b = _unit(3)

    def _image(self, style, content_weight=0.5):
        """A synthetic unit embedding: collection style plus subject content."""
        v = style + content_weight * self.content
        return (v / np.linalg.norm(v)).tolist()

    def test_no_embeddings_returns_none(self):
        self.assertIsNone(build_subject_query_embedding([]))
        self.assertIsNone(build_subject_query_embedding([(self.coll_a.pk, None)]))

    def test_without_stats_falls_back_to_plain_centroid(self):
        rows = [
            (self.coll_a.pk, self._image(self.style_a)),
            (self.coll_b.pk, self._image(self.style_b)),
        ]
        query = build_subject_query_embedding(rows)
        X = np.array([r[1] for r in rows])
        X /= np.linalg.norm(X, axis=1, keepdims=True)
        centroid = X.mean(axis=0)
        centroid /= np.linalg.norm(centroid)
        self.assertAlmostEqual(_cos(query, centroid), 1.0, places=6)
        self.assertAlmostEqual(float(np.linalg.norm(query)), 1.0, places=6)

    def test_collection_centering_cancels_style(self):
        # Collection means dominated by style; images share the content signal
        CollectionEmbeddingStats.objects.create(
            collection=self.coll_a,
            mean_embedding=self.style_a.tolist(),
            embedding_count=1000,
        )
        CollectionEmbeddingStats.objects.create(
            collection=self.coll_b,
            mean_embedding=self.style_b.tolist(),
            embedding_count=1000,
        )
        rows = [
            (self.coll_a.pk, self._image(self.style_a)),
            (self.coll_b.pk, self._image(self.style_b)),
        ]
        query = np.array(build_subject_query_embedding(rows))
        plain = np.array([r[1] for r in rows]).mean(axis=0)

        # The centered query should align with content far better than the
        # plain centroid, and style alignment should shrink
        self.assertGreater(_cos(query, self.content), _cos(plain, self.content))
        self.assertGreater(_cos(query, self.content), 0.8)
        self.assertLess(abs(_cos(query, self.style_a)), abs(_cos(plain, self.style_a)))

    def test_near_duplicates_collapse(self):
        # Four copies of one view plus one distinct view: without dedupe the
        # duplicated view dominates the centroid; with dedupe both views
        # contribute equally
        view1 = self._image(self.style_a, content_weight=0.3)
        view2 = self._image(self.style_b, content_weight=0.3)
        rows = [(self.coll_a.pk, view1)] * 4 + [(self.coll_b.pk, view2)]
        query = np.array(build_subject_query_embedding(rows))
        expected = np.array(view1) + np.array(view2)
        expected /= np.linalg.norm(expected)
        self.assertAlmostEqual(_cos(query, expected), 1.0, places=6)

    def test_degenerate_residuals_fall_back_to_centroid(self):
        # A set image exactly at its (unshrunk-dominant) collection mean has
        # no residual; with only such images the builder falls back
        big_n = 10**9  # overwhelm shrinkage so mu_c ~= mean_c
        emb = self._image(self.style_a)
        CollectionEmbeddingStats.objects.create(
            collection=self.coll_a, mean_embedding=emb, embedding_count=big_n
        )
        query = build_subject_query_embedding([(self.coll_a.pk, emb)])
        self.assertAlmostEqual(_cos(query, np.array(emb)), 1.0, places=5)


class RefreshCollectionEmbeddingStatsTests(TestCase):
    """The per-collection mean embedding refresh tasks."""

    @classmethod
    def setUpTestData(cls):
        cls.source = Source.objects.create(
            name="Src", slug="src", url="https://example.com", description=""
        )
        cls.collection = Collection.objects.create(
            source=cls.source, name="A", slug="a", url="https://example.com"
        )
        cls.other = Collection.objects.create(
            source=cls.source, name="B", slug="b", url="https://example.com"
        )

    def _image(self, title, embedding, collection=None, **kwargs):
        return Image.objects.create(
            collection=collection or self.collection,
            title=title,
            permalink=f"https://img.example.com/{title}.jpg",
            embedding=embedding,
            **kwargs,
        )

    def test_computes_mean_and_count(self):
        self._image("one", [1.0] * DIM)
        self._image("two", [3.0] * DIM)
        self._image("no-embedding", None)

        refresh_collection_embedding_stats()

        stats = CollectionEmbeddingStats.objects.get(collection=self.collection)
        self.assertEqual(stats.embedding_count, 2)
        self.assertEqual(len(stats.mean_embedding), DIM)
        self.assertAlmostEqual(stats.mean_embedding[0], 2.0, places=5)

    def test_duplicates_excluded_even_when_searchable(self):
        original = self._image("one", [1.0] * DIM)
        duplicate = self._image("dupe", [9.0] * DIM)
        # Bypass signals so is_searchable stays True: the explicit
        # duplicate_of condition alone must exclude this image
        Image.objects.filter(pk=duplicate.pk).update(duplicate_of=original.pk)
        self.assertTrue(Image.objects.get(pk=duplicate.pk).is_searchable)

        refresh_collection_embedding_stats()

        stats = CollectionEmbeddingStats.objects.get(collection=self.collection)
        self.assertEqual(stats.embedding_count, 1)
        self.assertAlmostEqual(stats.mean_embedding[0], 1.0, places=5)

    def test_unsearchable_images_excluded_and_stale_rows_removed(self):
        image = self._image("one", [1.0] * DIM)
        refresh_collection_embedding_stats()
        self.assertTrue(
            CollectionEmbeddingStats.objects.filter(collection=self.collection).exists()
        )

        Image.objects.filter(pk=image.pk).update(is_searchable=False)
        refresh_collection_embedding_stats()
        self.assertFalse(
            CollectionEmbeddingStats.objects.filter(collection=self.collection).exists()
        )

    def test_refresh_next_picks_most_stale_collection(self):
        # collection drifts by 2 images, other by 1 -> collection goes first
        self._image("a1", [1.0] * DIM)
        self._image("a2", [1.0] * DIM)
        self._image("b1", [2.0] * DIM, collection=self.other)

        refresh_next_collection_embedding_stats()
        self.assertTrue(
            CollectionEmbeddingStats.objects.filter(collection=self.collection).exists()
        )
        self.assertFalse(
            CollectionEmbeddingStats.objects.filter(collection=self.other).exists()
        )

        refresh_next_collection_embedding_stats()
        self.assertTrue(
            CollectionEmbeddingStats.objects.filter(collection=self.other).exists()
        )

    def test_refresh_next_noop_when_in_sync(self):
        self._image("one", [1.0] * DIM)
        refresh_collection_embedding_stats()
        stats = CollectionEmbeddingStats.objects.get(collection=self.collection)
        before = stats.updated_at

        refresh_next_collection_embedding_stats()

        stats.refresh_from_db()
        self.assertEqual(stats.updated_at, before)
        # No row invented for the empty collection either
        self.assertFalse(
            CollectionEmbeddingStats.objects.filter(collection=self.other).exists()
        )

    def test_refresh_next_removes_row_when_collection_empties(self):
        image = self._image("one", [1.0] * DIM)
        refresh_collection_embedding_stats()

        Image.objects.filter(pk=image.pk).update(is_searchable=False)
        refresh_next_collection_embedding_stats()

        self.assertFalse(
            CollectionEmbeddingStats.objects.filter(collection=self.collection).exists()
        )


# --- Wikidata closure tests -------------------------------------------------

_WD = "http://www.wikidata.org/entity/"
_WDS = "http://www.wikidata.org/entity/statement/"
_WDT = "http://www.wikidata.org/prop/direct/"
_RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"


def _wd(local):
    return pyoxigraph.NamedNode(f"{_WD}{local}")


def _wdt(pid):
    return pyoxigraph.NamedNode(f"{_WDT}{pid}")


def _label(entity, text, lang="en"):
    return pyoxigraph.Triple(
        _wd(entity),
        pyoxigraph.NamedNode(_RDFS_LABEL),
        pyoxigraph.Literal(text, language=lang),
    )


# A miniature Wikidata neighbourhood for running the closure CONSTRUCT
# against in-memory. Exercises what a recorded WDQS fixture can't: triples
# that must be EXCLUDED (off-language labels, non-nav predicates on
# nav-profile entities, entities no branch selects).
_CLOSURE_FIXTURE_TTL = """\
@prefix wd: <http://www.wikidata.org/entity/> .
@prefix wds: <http://www.wikidata.org/entity/statement/> .
@prefix wdt: <http://www.wikidata.org/prop/direct/> .
@prefix p: <http://www.wikidata.org/prop/> .
@prefix ps: <http://www.wikidata.org/prop/statement/> .
@prefix pq: <http://www.wikidata.org/prop/qualifier/> .
@prefix wikibase: <http://wikiba.se/ontology#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix schema: <http://schema.org/> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

# Seed: a historic hall with a heritage-designation statement (qualified
# by the district it sits in) and an authority-control identifier.
wd:Q100 rdfs:label "Test Hall"@en, "Salle d'essai"@fr ;
    schema:description "historic building"@en ;
    wdt:P31 wd:Q200 ;
    wdt:P84 wd:Q300 ;
    wdt:P131 wd:Q700 ;
    wdt:P1435 wd:Q400 ;
    wdt:P5473 "127-0345" ;
    p:P1435 wds:Q100-aaaa-bbbb .

wds:Q100-aaaa-bbbb ps:P1435 wd:Q400 ;
    pq:P361 wd:Q500 .

# Class chain: Q100 -P31-> Q200 -P279-> Q210
wd:Q200 rdfs:label "building"@en, "bâtiment"@fr ;
    wdt:P279 wd:Q210 .
wd:Q210 rdfs:label "structure"@en .

# Direct reference (architect) with its own onward claim.
wd:Q300 rdfs:label "Test Architect"@en ;
    wdt:P31 wd:Q5 ;
    wdt:P800 wd:Q999 .

# Only reachable one hop past a direct reference - no branch selects it.
wd:Q5 rdfs:label "human"@en .

wd:Q400 rdfs:label "historic landmark"@en .

# Qualifier target: nav profile only, so its P571 must not come along.
wd:Q500 rdfs:label "Test District"@en, "Quartier d'essai"@fr ;
    wdt:P571 "1900-01-01T00:00:00Z"^^xsd:dateTime .

# P1629 target of the authority property: nav profile.
wd:Q600 rdfs:label "Test Register"@en, "Registre d'essai"@fr ;
    wdt:P571 "1966-01-01T00:00:00Z"^^xsd:dateTime .

# Containment chain: Q100 -P131-> Q700 -P131-> Q710 -P131-> Q720. Q700
# is also a first-hop direct claim target, so it gets the FULL profile
# in both query variants; only Q710/Q720 distinguish the region variant
# (which walks P131+) from the subject one (which stops at Q700).
wd:Q700 rdfs:label "Test City"@en ;
    wdt:P131 wd:Q710 .
wd:Q710 rdfs:label "Test State"@en, "État d'essai"@fr ;
    wdt:P131 wd:Q720 ;
    wdt:P571 "1788-01-01T00:00:00Z"^^xsd:dateTime .
wd:Q720 rdfs:label "Test Country"@en .

# A non-authority property descriptor - the generalized descriptor branch
# must mirror it too.
wd:P1435 wikibase:directClaim wdt:P1435 ;
    rdfs:label "heritage designation"@en ;
    wdt:P31 wd:Q18608871 .

# An authority-control property descriptor (P31 Q18618628).
wd:P5473 wikibase:directClaim wdt:P5473 ;
    rdfs:label "Test Register number"@en, "numéro au registre"@fr ;
    wdt:P31 wd:Q18618628 ;
    wdt:P1630 "https://register.example/$1" ;
    wdt:P1629 wd:Q600 .

<https://en.wikipedia.org/wiki/Test_Hall> schema:about wd:Q100 ;
    schema:isPartOf <https://en.wikipedia.org/> .
"""


class ClosureQueryTests(SimpleTestCase):
    """Input validation and structure of the assembled closure CONSTRUCT."""

    def test_rejects_invalid_qids(self):
        for bad in ("Q42; DROP ALL", "P31", "", "Q042", None, "Q42 "):
            with self.assertRaises(UnsafeSparqlInput):
                closure_query(bad)

    def test_rejects_invalid_language_tags(self):
        with override_settings(WIKIDATA_MIRROR_LANGUAGES=['en"), DROP ALL; #']):
            with self.assertRaises(UnsafeSparqlInput):
                closure_query("Q42")

    def test_rejects_empty_language_list(self):
        with override_settings(WIKIDATA_MIRROR_LANGUAGES=[]):
            with self.assertRaises(ImproperlyConfigured):
                closure_query("Q42")

    def test_structure(self):
        query = closure_query("Q42")
        # 5 top-level branches (4 UNIONs) + 1 inner in the full-profile
        # branch + 2 inner in the nav-profile branch.
        self.assertEqual(query.count("UNION"), 7)
        # The nav predicate whitelist exists exactly once (shared tail).
        self.assertEqual(query.count("skos:altLabel"), 1)
        # One language filter per emission tail: seed, full, nav, descriptors.
        self.assertEqual(query.count('lang(?o) IN ("en", "mul")'), 4)
        # No leftover placeholders and no synthetic predicates.
        self.assertNotIn("{qid}", query)
        self.assertNotIn("{lang_filter}", query)
        self.assertNotIn("urn:yesterdays", query)
        self.assertEqual(query.count("{"), query.count("}"))

    def test_language_setting_reaches_filter(self):
        with override_settings(WIKIDATA_MIRROR_LANGUAGES=["en", "fr"]):
            self.assertIn('lang(?o) IN ("en", "fr", "mul")', closure_query("Q42"))

    def test_query_is_valid_sparql(self):
        # pyoxigraph parses the query eagerly - a syntax error raises here.
        pyoxigraph.Store().query(closure_query("Q42"))


class ClosureQuerySemanticsTests(SimpleTestCase):
    """Run the closure CONSTRUCT against the in-memory fixture graph."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.store = pyoxigraph.Store()
        cls.store.load(
            _CLOSURE_FIXTURE_TTL.encode(), format=pyoxigraph.RdfFormat.TURTLE
        )

    def _closure(self, qid="Q100"):
        return set(self.store.query(closure_query(qid)))

    def test_seed_literals_language_filtered(self):
        result = self._closure()
        self.assertIn(_label("Q100", "Test Hall"), result)
        self.assertNotIn(_label("Q100", "Salle d'essai", "fr"), result)

    def test_configured_languages_widen_the_mirror(self):
        with override_settings(WIKIDATA_MIRROR_LANGUAGES=["en", "fr"]):
            result = self._closure()
        self.assertIn(_label("Q100", "Test Hall"), result)
        self.assertIn(_label("Q100", "Salle d'essai", "fr"), result)

    def test_ancestors_get_nav_profile(self):
        result = self._closure()
        self.assertIn(_label("Q200", "building"), result)
        self.assertNotIn(_label("Q200", "bâtiment", "fr"), result)
        self.assertIn(pyoxigraph.Triple(_wd("Q200"), _wdt("P279"), _wd("Q210")), result)
        self.assertIn(_label("Q210", "structure"), result)

    def test_qualifier_target_gets_labels_but_not_statements(self):
        result = self._closure()
        self.assertIn(_label("Q500", "Test District"), result)
        self.assertFalse(
            any(
                t.subject == _wd("Q500") and t.predicate == _wdt("P571") for t in result
            )
        )

    def test_direct_reference_gets_full_profile(self):
        result = self._closure()
        self.assertIn(_label("Q300", "Test Architect"), result)
        self.assertIn(pyoxigraph.Triple(_wd("Q300"), _wdt("P800"), _wd("Q999")), result)

    def test_second_hop_entities_are_not_pulled(self):
        # Q5 is only reachable through the architect's own P31 - no branch
        # selects it, so its label stays out of the closure.
        self.assertNotIn(_label("Q5", "human"), self._closure())

    def test_statement_bodies_come_along(self):
        result = self._closure()
        stmt = pyoxigraph.NamedNode(f"{_WDS}Q100-aaaa-bbbb")
        ps = pyoxigraph.NamedNode("http://www.wikidata.org/prop/statement/P1435")
        pq = pyoxigraph.NamedNode("http://www.wikidata.org/prop/qualifier/P361")
        self.assertIn(pyoxigraph.Triple(stmt, ps, _wd("Q400")), result)
        self.assertIn(pyoxigraph.Triple(stmt, pq, _wd("Q500")), result)

    def test_descriptors_mirrored_for_every_used_property(self):
        result = self._closure()
        # Non-authority property: mirrored too (the generalization).
        self.assertIn(_label("P1435", "heritage designation"), result)
        # Authority property: full descriptor set, off-language label dropped.
        self.assertIn(_label("P5473", "Test Register number"), result)
        self.assertNotIn(_label("P5473", "numéro au registre", "fr"), result)
        self.assertIn(
            pyoxigraph.Triple(
                _wd("P5473"),
                _wdt("P1630"),
                pyoxigraph.Literal("https://register.example/$1"),
            ),
            result,
        )
        self.assertIn(
            pyoxigraph.Triple(_wd("P5473"), _wdt("P1629"), _wd("Q600")), result
        )

    def test_authority_item_mirrored_with_nav_profile(self):
        result = self._closure()
        self.assertIn(_label("Q600", "Test Register"), result)
        self.assertNotIn(_label("Q600", "Registre d'essai", "fr"), result)
        self.assertFalse(
            any(
                t.subject == _wd("Q600") and t.predicate == _wdt("P571") for t in result
            )
        )

    def test_sitelink_triples_present(self):
        result = self._closure()
        article = pyoxigraph.NamedNode("https://en.wikipedia.org/wiki/Test_Hall")
        self.assertIn(
            pyoxigraph.Triple(
                article, pyoxigraph.NamedNode("http://schema.org/about"), _wd("Q100")
            ),
            result,
        )


class RegionClosureQueryTests(SimpleTestCase):
    """The ``include_p131`` (region-seed) variant of the closure CONSTRUCT."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.store = pyoxigraph.Store()
        cls.store.load(
            _CLOSURE_FIXTURE_TTL.encode(), format=pyoxigraph.RdfFormat.TURTLE
        )

    def _closure(self, qid="Q100", **kwargs):
        return set(self.store.query(closure_query(qid, **kwargs)))

    def test_default_variant_unchanged(self):
        self.assertEqual(
            closure_query("Q42"), closure_query("Q42", include_p131=False)
        )
        self.assertNotIn("P131+", closure_query("Q42"))

    def test_region_query_structure(self):
        query = closure_query("Q42", include_p131=True)
        # One more top-level branch than the subject variant's 7 UNIONs.
        self.assertEqual(query.count("UNION"), 8)
        # The nav predicate whitelist: shared tail plus the P131 tail.
        self.assertEqual(query.count("skos:altLabel"), 2)
        # One language filter per emission tail: seed, full, nav,
        # descriptors, containment chain.
        self.assertEqual(query.count('lang(?o) IN ("en", "mul")'), 5)
        self.assertNotIn("{qid}", query)
        self.assertNotIn("{lang_filter}", query)
        self.assertEqual(query.count("{"), query.count("}"))

    def test_region_query_is_valid_sparql(self):
        pyoxigraph.Store().query(closure_query("Q42", include_p131=True))

    def test_p131_chain_lands_with_edges(self):
        result = self._closure(include_p131=True)
        self.assertIn(_label("Q710", "Test State"), result)
        self.assertNotIn(_label("Q710", "État d'essai", "fr"), result)
        self.assertIn(_label("Q720", "Test Country"), result)
        self.assertIn(pyoxigraph.Triple(_wd("Q700"), _wdt("P131"), _wd("Q710")), result)
        self.assertIn(pyoxigraph.Triple(_wd("Q710"), _wdt("P131"), _wd("Q720")), result)

    def test_chain_entities_get_nav_plus_p131_only(self):
        result = self._closure(include_p131=True)
        self.assertFalse(
            any(
                t.subject == _wd("Q710") and t.predicate == _wdt("P571") for t in result
            )
        )

    def test_chain_absent_from_subject_variant(self):
        result = self._closure()
        # Q700 is a first-hop direct claim target, so its full profile —
        # including its own P131 edge — comes along even for subjects...
        self.assertIn(_label("Q700", "Test City"), result)
        self.assertIn(pyoxigraph.Triple(_wd("Q700"), _wdt("P131"), _wd("Q710")), result)
        # ...but the walk stops there: deeper chain entities stay out.
        self.assertFalse(any(t.subject == _wd("Q710") for t in result))
        self.assertNotIn(_label("Q720", "Test Country"), result)


class ParseClosureTests(SimpleTestCase):
    """Grouping and named-graph routing of the closure response."""

    # Shaped like a (tiny) closure CONSTRUCT response.
    TTL = """\
@prefix wd: <http://www.wikidata.org/entity/> .
@prefix wds: <http://www.wikidata.org/entity/statement/> .
@prefix wdt: <http://www.wikidata.org/prop/direct/> .
@prefix ps: <http://www.wikidata.org/prop/statement/> .
@prefix wikibase: <http://wikiba.se/ontology#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix schema: <http://schema.org/> .

wd:Q100 rdfs:label "Test Hall"@en, "Salle d'essai"@fr ;
    wdt:P31 wd:Q200 .
wds:Q100-aaaa-bbbb ps:P1435 wd:Q400 .
wds:q100-cccc-dddd ps:P1435 wd:Q400 .
wd:Q200 rdfs:label "building"@en .
wd:P5473 wikibase:directClaim wdt:P5473 ;
    rdfs:label "Test Register number"@en .
<https://en.wikipedia.org/wiki/Test_Hall> schema:about wd:Q100 .
<http://www.wikidata.org/entity/QBOGUS> rdfs:label "nope"@en .
_:blank rdfs:label "anonymous"@en .
"""

    def setUp(self):
        self.groups, self.labels = parse_closure(self.TTL.encode())

    def test_entities_grouped_by_their_own_iri(self):
        self.assertIn(f"{_WD}Q100", self.groups)
        self.assertIn(f"{_WD}Q200", self.groups)

    def test_statement_triples_routed_to_owning_entity(self):
        # Both the modern (Q100-...) and legacy-lowercase (q100-...)
        # statement IRIs resolve to the same owning entity graph.
        stmt_triples = [
            t for t in self.groups[f"{_WD}Q100"] if t.subject.value.startswith(_WDS)
        ]
        self.assertEqual(len(stmt_triples), 2)

    def test_property_descriptors_get_their_own_graph(self):
        self.assertIn(f"{_WD}P5473", self.groups)
        self.assertEqual(len(self.groups[f"{_WD}P5473"]), 2)

    def test_invalid_and_foreign_subjects_dropped(self):
        self.assertNotIn(f"{_WD}QBOGUS", self.groups)
        self.assertFalse(
            any(iri.startswith("https://en.wikipedia.org/") for iri in self.groups)
        )

    def test_labels_collect_preferred_q_entity_labels(self):
        self.assertEqual(self.labels["Q100"], "Test Hall")
        self.assertEqual(self.labels["Q200"], "building")
        self.assertNotIn("P5473", self.labels)

    def test_labels_fall_back_to_mul_but_prefer_english(self):
        ttl = b"""\
@prefix wd: <http://www.wikidata.org/entity/> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
wd:Q201 rdfs:label "Default name"@mul .
wd:Q202 rdfs:label "Default override"@mul, "English name"@en .
"""
        _, labels = parse_closure(ttl)

        self.assertEqual(labels["Q201"], "Default name")
        self.assertEqual(labels["Q202"], "English name")

    def test_payload_covers_every_group(self):
        payload = build_graph_payload(self.groups)
        node_ids = {e["id"] for e in payload["entities"]} | {
            p["id"] for p in payload["properties"]
        }
        self.assertEqual(node_ids, {iri.removeprefix(_WD) for iri in self.groups})
        self.assertEqual({p["id"] for p in payload["properties"]}, {"P5473"})

    def test_legacy_lowercase_statements_keep_their_owner(self):
        payload = build_graph_payload(self.groups)
        owners = {s["id"]: s["owner"] for s in payload["statements"]}
        self.assertEqual(
            owners, {"Q100-aaaa-bbbb": "Q100", "q100-cccc-dddd": "Q100"}
        )


class MalformedClosureTests(SimpleTestCase):
    """Bad Turtle from WDQS is a ClosureLoadError, not a stray SyntaxError."""

    PREFIXES = """\
@prefix wd: <http://www.wikidata.org/entity/> .
@prefix wdt: <http://www.wikidata.org/prop/direct/> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

"""

    # A CONSTRUCT response cut off mid-statement, as WDQS leaves it when a
    # query outruns its time limit after the stream has started.
    TRUNCATED = PREFIXES + 'wd:Q100 rdfs:label "Test Hall"@en ;\n    wdt:P31 wd:Q2'

    # Whole document, one unusable term: a raw space inside an IRI.
    BAD_IRI = PREFIXES + "wd:Q100 wdt:P973 <https://example.org/a b> .\n"

    def test_truncated_response_reports_truncation(self):
        for label, parse in (
            ("parse_closure", parse_closure),
            ("extract_seed_metadata", lambda ttl: extract_seed_metadata(ttl, "Q100")),
        ):
            with self.subTest(label):
                with self.assertRaises(ClosureLoadError) as ctx:
                    parse(self.TRUNCATED.encode())
                self.assertIn("truncated in transit", str(ctx.exception))

    def test_malformed_term_reports_the_offending_line(self):
        with self.assertRaises(ClosureLoadError) as ctx:
            parse_closure(self.BAD_IRI.encode())
        message = str(ctx.exception)
        self.assertIn("Invalid IRI", message)
        self.assertIn("line 5 reads: wd:Q100 wdt:P973", message)


class BuildGraphPayloadTests(SimpleTestCase):
    """The RDF-triples → property-graph transform behind the Memgraph load."""

    # Shaped like a closure CONSTRUCT response, exercising every predicate
    # class the transform routes: multi-valued literals, language-keyed
    # labels/aliases, entity vs literal vs external-IRI claim objects,
    # statement bodies with qualifiers/rank/references, property
    # descriptors, and the namespaces that must be dropped (wdtn:, psv:).
    TTL = """\
@prefix wd: <http://www.wikidata.org/entity/> .
@prefix wds: <http://www.wikidata.org/entity/statement/> .
@prefix wdt: <http://www.wikidata.org/prop/direct/> .
@prefix wdtn: <http://www.wikidata.org/prop/direct-normalized/> .
@prefix p: <http://www.wikidata.org/prop/> .
@prefix ps: <http://www.wikidata.org/prop/statement/> .
@prefix psv: <http://www.wikidata.org/prop/statement/value/> .
@prefix pq: <http://www.wikidata.org/prop/qualifier/> .
@prefix wikibase: <http://wikiba.se/ontology#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix schema: <http://schema.org/> .
@prefix prov: <http://www.w3.org/ns/prov#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

wd:Q100 rdfs:label "Test Hall"@en, "Second Label"@en, "Salão"@pt-BR ;
    schema:description "historic building"@en ;
    skos:altLabel "The Hall"@en, "Old Hall"@en ;
    wdt:P31 wd:Q200 ;
    wdt:P31 wd:Q200 ;
    wdt:P571 "1895-01-01T00:00:00Z"^^xsd:dateTime,
             "1896-01-01T00:00:00Z"^^xsd:dateTime ;
    wdt:P5473 "127-0345" ;
    wdt:P18 <http://commons.wikimedia.org/wiki/Special:FilePath/hall.jpg> ;
    wdtn:P5473 <https://register.example/entity/127-0345> ;
    p:P1435 wds:Q100-aaaa-bbbb .

wds:Q100-aaaa-bbbb ps:P1435 wd:Q400 ;
    psv:P1435 wd:Q999 ;
    pq:P361 wd:Q500 ;
    pq:P580 "1936-01-01T00:00:00Z"^^xsd:dateTime ;
    wikibase:rank wikibase:NormalRank ;
    prov:wasDerivedFrom <http://www.wikidata.org/reference/abc> .

wd:Q200 rdfs:label "building"@en ;
    wdt:P279 wd:Q210 .

wd:Q400 rdfs:label "historic landmark"@en .
wd:Q500 rdfs:label "Test District"@en .

wd:P5473 wikibase:directClaim wdt:P5473 ;
    rdfs:label "Test Register number"@en ;
    wdt:P31 wd:Q18618628 ;
    wdt:P1630 "https://register.example/$1" ;
    wdt:P1629 wd:Q600 .

wd:Q600 rdfs:label "Test Register"@en .
"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        groups, _ = parse_closure(cls.TTL.encode())
        cls.payload = build_graph_payload(groups)
        cls.entity_props = {e["id"]: e["props"] for e in cls.payload["entities"]}
        cls.property_props = {p["id"]: p["props"] for p in cls.payload["properties"]}

    def test_labels_language_keyed_first_literal_wins(self):
        props = self.entity_props["Q100"]
        self.assertEqual(props["label_en"], "Test Hall")
        self.assertEqual(props["label_pt_br"], "Salão")
        self.assertEqual(props["description_en"], "historic building")

    def test_aliases_collect_into_lists(self):
        self.assertEqual(
            self.entity_props["Q100"]["aliases_en"], ["The Hall", "Old Hall"]
        )

    def test_literal_claims_become_multivalue_lists(self):
        props = self.entity_props["Q100"]
        self.assertEqual(
            props["P571"],
            ["1895-01-01T00:00:00Z", "1896-01-01T00:00:00Z"],
        )
        self.assertEqual(props["P5473"], ["127-0345"])

    def test_external_iri_claims_keep_their_iri_string(self):
        self.assertEqual(
            self.entity_props["Q100"]["P18"],
            ["http://commons.wikimedia.org/wiki/Special:FilePath/hall.jpg"],
        )

    def test_normalized_values_dropped(self):
        # wdtn:P5473 must not pollute the P5473 literal list, and psv:
        # value nodes must not become statement props or edges.
        self.assertEqual(self.entity_props["Q100"]["P5473"], ["127-0345"])
        stmt = self.payload["statements"][0]
        self.assertNotIn("ps_P1435", stmt["props"])
        self.assertFalse(
            any(e["dst"] == "Q999" for e in self.payload["statement_edges"])
        )

    def test_entity_claims_become_typed_edges_deduped(self):
        edges = self.payload["direct_edges"]
        self.assertEqual(
            edges[("Entity", "P31", "Entity")], [{"src": "Q100", "dst": "Q200"}]
        )
        self.assertEqual(
            edges[("Entity", "P279", "Entity")], [{"src": "Q200", "dst": "Q210"}]
        )
        # Edge targets outside the closure stay stubs, not payload nodes.
        self.assertNotIn("Q210", self.entity_props)

    def test_statement_node_shape(self):
        (stmt,) = self.payload["statements"]
        self.assertEqual(stmt["id"], "Q100-aaaa-bbbb")
        self.assertEqual(stmt["owner"], "Q100")
        self.assertEqual(stmt["pid"], "P1435")
        self.assertEqual(stmt["props"]["rank"], "NormalRank")
        self.assertEqual(
            stmt["props"]["derived_from"], ["http://www.wikidata.org/reference/abc"]
        )
        self.assertEqual(stmt["props"]["pq_P580"], ["1936-01-01T00:00:00Z"])

    def test_statement_entity_objects_become_edges(self):
        self.assertEqual(
            self.payload["statement_edges"],
            [
                {
                    "stmt": "Q100-aaaa-bbbb",
                    "kind": "VALUE",
                    "pid": "P1435",
                    "dst": "Q400",
                },
                {
                    "stmt": "Q100-aaaa-bbbb",
                    "kind": "QUALIFIER",
                    "pid": "P361",
                    "dst": "Q500",
                },
            ],
        )

    def test_property_descriptor_shape(self):
        props = self.property_props["P5473"]
        self.assertEqual(props["label_en"], "Test Register number")
        self.assertEqual(props["P1630"], ["https://register.example/$1"])
        # wikibase:directClaim is dropped — its join became id equality.
        self.assertEqual(set(props), {"label_en", "P1630"})
        edges = self.payload["direct_edges"]
        self.assertEqual(
            edges[("Property", "P31", "Entity")],
            [{"src": "P5473", "dst": "Q18618628"}],
        )
        self.assertEqual(
            edges[("Property", "P1629", "Entity")],
            [{"src": "P5473", "dst": "Q600"}],
        )


class ExtractSeedMetadataTests(SimpleTestCase):
    """The WikidataItem field values pulled out of the closure Turtle."""

    PREFIXES = """\
@prefix wd: <http://www.wikidata.org/entity/> .
@prefix wdt: <http://www.wikidata.org/prop/direct/> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix geo: <http://www.opengis.net/ont/geosparql#> .
"""

    def _extract(self, body, qid="Q100"):
        return extract_seed_metadata((self.PREFIXES + body).encode(), qid)

    def test_title_falls_back_to_mul(self):
        meta = self._extract('wd:Q100 rdfs:label "Default name"@mul .\n')

        self.assertEqual(meta["title"], "Default name")

    def test_title_prefers_english_over_mul(self):
        meta = self._extract(
            'wd:Q100 rdfs:label "Default name"@mul, "English name"@en .\n'
        )

        self.assertEqual(meta["title"], "English name")

    def test_title_does_not_fall_back_to_qid(self):
        meta = self._extract("wd:Q100 wdt:P31 wd:Q200 .\n")

        self.assertEqual(meta["title"], "")

    def test_inception_and_demolished_parsed_first_value_wins(self):
        meta = self._extract(
            """\
wd:Q100 wdt:P571 "1901-01-01T00:00:00Z"^^xsd:dateTime ;
    wdt:P576 "1910-06-15T00:00:00Z"^^xsd:dateTime .
"""
        )
        self.assertEqual(meta["inception"], date(1901, 1, 1))
        self.assertEqual(meta["demolished"], date(1910, 6, 15))

    def test_dates_absent_when_properties_missing(self):
        meta = self._extract("wd:Q100 wdt:P31 wd:Q200 .\n")
        self.assertIsNone(meta["inception"])
        self.assertIsNone(meta["demolished"])

    def test_bce_dates_rejected(self):
        # BCE literals carry a leading "-" and must not be misparsed.
        meta = self._extract(
            'wd:Q100 wdt:P576 "-0044-03-15T00:00:00Z"^^xsd:dateTime .\n'
        )
        self.assertIsNone(meta["demolished"])

    def test_dates_only_read_off_the_seed(self):
        # A neighbour's own P571/P576 must not leak onto the seed's row.
        meta = self._extract(
            """\
wd:Q100 wdt:P31 wd:Q200 .
wd:Q200 wdt:P571 "1800-01-01T00:00:00Z"^^xsd:dateTime ;
    wdt:P576 "1850-01-01T00:00:00Z"^^xsd:dateTime .
"""
        )
        self.assertIsNone(meta["inception"])
        self.assertIsNone(meta["demolished"])

    def test_coordinate_location_parsed_longitude_first(self):
        # WKT is "Point(lon lat)", the reverse of Wikidata's UI display.
        meta = self._extract(
            'wd:Q100 wdt:P625 "Point(-77.436111 37.540833)"^^geo:wktLiteral .\n'
        )
        point = meta["coordinate_location"]
        self.assertIsNotNone(point)
        self.assertAlmostEqual(point.x, -77.436111)
        self.assertAlmostEqual(point.y, 37.540833)
        self.assertEqual(point.srid, 4326)

    def test_coordinate_location_absent_when_property_missing(self):
        meta = self._extract("wd:Q100 wdt:P31 wd:Q200 .\n")
        self.assertIsNone(meta["coordinate_location"])

    def test_coordinate_location_only_read_off_the_seed(self):
        meta = self._extract(
            """\
wd:Q100 wdt:P31 wd:Q200 .
wd:Q200 wdt:P625 "Point(-77.436111 37.540833)"^^geo:wktLiteral .
"""
        )
        self.assertIsNone(meta["coordinate_location"])

    def test_coordinate_location_rejects_other_globes(self):
        # A Mars coordinate is not a location on Earth's surface.
        meta = self._extract(
            'wd:Q100 wdt:P625 "<http://www.wikidata.org/entity/Q111> '
            'Point(-77.4 37.5)"^^geo:wktLiteral .\n'
        )
        self.assertIsNone(meta["coordinate_location"])

    def test_coordinate_location_accepts_explicit_earth_globe(self):
        meta = self._extract(
            'wd:Q100 wdt:P625 "<http://www.wikidata.org/entity/Q2> '
            'Point(-77.4 37.5)"^^geo:wktLiteral .\n'
        )
        point = meta["coordinate_location"]
        self.assertIsNotNone(point)
        self.assertAlmostEqual(point.x, -77.4)

    def test_coordinate_location_rejects_out_of_range_and_non_points(self):
        for literal in ('"Point(-200 37.5)"', '"Polygon((0 0, 1 1, 1 0, 0 0))"'):
            with self.subTest(literal=literal):
                meta = self._extract(
                    f"wd:Q100 wdt:P625 {literal}^^geo:wktLiteral .\n"
                )
                self.assertIsNone(meta["coordinate_location"])


class PreferredWikidataLabelTests(SimpleTestCase):
    def test_prefers_english(self):
        labels = {
            "mul": {"value": "Default name"},
            "en": {"value": "English name"},
        }

        self.assertEqual(_preferred_wikidata_label(labels), "English name")

    def test_falls_back_to_mul(self):
        labels = {"mul": {"value": "Default name"}}

        self.assertEqual(_preferred_wikidata_label(labels), "Default name")

    def test_does_not_fall_back_to_qid(self):
        self.assertEqual(_preferred_wikidata_label({}), "")


class WikidataItemDisplayLabelTests(TestCase):
    def test_existing_qid_placeholder_is_refetched(self):
        item = WikidataItem.objects.bulk_create(
            [WikidataItem(wikidata_id="Q123", title="Q123")]
        )[0]

        def populate(instance):
            instance.title = "Default name"
            return True

        with patch.object(WikidataItem, "populate_from_wikidata", populate):
            item.ensure_display_label()

        item.refresh_from_db()
        self.assertEqual(item.title, "Default name")


class WikidataItemDateRangeTests(SimpleTestCase):
    """The display string built from inception/demolished."""

    def test_both_dates(self):
        item = WikidataItem(inception=date(1901, 5, 1), demolished=date(1910, 1, 1))
        self.assertEqual(item.date_range, "1901–1910")

    def test_open_ended_when_still_standing(self):
        item = WikidataItem(inception=date(1950, 1, 1))
        self.assertEqual(item.date_range, "1950–")

    def test_unknown_start(self):
        item = WikidataItem(demolished=date(1910, 1, 1))
        self.assertEqual(item.date_range, "?–1910")

    def test_empty_without_dates(self):
        self.assertEqual(WikidataItem().date_range, "")


class _RecordingTx:
    """Stand-in for a neo4j transaction, capturing (query, params) calls."""

    def __init__(self):
        self.calls = []

    def run(self, query, **params):
        self.calls.append((query, params))


class ApplyClosureTests(SimpleTestCase):
    """The Cypher generation applying a payload to Memgraph."""

    def _payload(self):
        groups, _ = parse_closure(BuildGraphPayloadTests.TTL.encode())
        return build_graph_payload(groups)

    def test_interpolated_relationship_types_are_validated(self):
        tx = _RecordingTx()
        apply_closure(tx, self._payload())
        fixed_types = {"STATEMENT", "VALUE", "QUALIFIER"}
        for query, _ in tx.calls:
            for rel_type in re.findall(r"\[:(\w+)", query):
                self.assertTrue(
                    rel_type in fixed_types or re.fullmatch(r"P[1-9]\d*", rel_type),
                    f"unexpected relationship type {rel_type!r} in {query!r}",
                )

    def test_rejects_unvalidated_pid_before_interpolation(self):
        payload = self._payload()
        payload["direct_edges"][("Entity", "P31]->() CREATE (m)", "Entity")] = [
            {"src": "Q100", "dst": "Q200"}
        ]
        with self.assertRaises(UnsafeSparqlInput):
            apply_closure(_RecordingTx(), payload)

    def test_rejects_unknown_node_labels(self):
        payload = self._payload()
        payload["direct_edges"][("Gadget", "P31", "Entity")] = [
            {"src": "Q100", "dst": "Q200"}
        ]
        with self.assertRaises(UnsafeSparqlInput):
            apply_closure(_RecordingTx(), payload)

    def test_wipes_owned_data_before_rewriting(self):
        tx = _RecordingTx()
        apply_closure(tx, self._payload())
        queries = [q for q, _ in tx.calls]
        last_wipe = max(
            i for i, q in enumerate(queries) if "DELETE" in q and "MERGE" not in q
        )
        first_write = min(i for i, q in enumerate(queries) if "MERGE" in q)
        self.assertLess(last_wipe, first_write)


class BrowseSubjectsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        source = Source.objects.create(
            name="Public archive",
            slug="public-archive",
            url="https://example.com",
            description="",
        )
        collection = Collection.objects.create(
            source=source,
            name="Photographs",
            slug="photographs",
        )

        city_item, building_item, landmark_item = WikidataItem.objects.bulk_create(
            [
                WikidataItem(wikidata_id="Q1001", title="Example City"),
                WikidataItem(wikidata_id="Q1002", title="Example Building"),
                WikidataItem(wikidata_id="Q1003", title="Example Landmark"),
            ]
        )
        cls.city = Subject.objects.create(
            title="Example City", wikidata_item=city_item
        )
        cls.building = Subject.objects.create(
            title="Example Building", wikidata_item=building_item
        )
        cls.landmark = Subject.objects.create(
            title="Example Landmark", wikidata_item=landmark_item
        )
        SubjectAncestor.objects.create(subject=cls.building, ancestor=city_item)

        for title, subject in (
            ("City image", cls.city),
            ("Building image", cls.building),
            ("Landmark image", cls.landmark),
        ):
            image = Image.objects.create(
                collection=collection,
                title=title,
                permalink=f"https://example.com/{title}.jpg",
            )
            SubjectMapping.objects.create(image=image, subject=subject)

    def test_omits_subjects_that_are_ancestors_of_other_subjects(self):
        response = self.client.get(reverse("subjects:browse_subjects"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {subject.pk for subject in response.context["subjects"]},
            {self.building.pk, self.landmark.pk},
        )
        self.assertEqual(response.context["overall_stats"]["total_subjects"], 2)


class SubjectAutocompleteTests(TestCase):
    """The fuzzy subject-tagging autocomplete (subjects/views.py)."""

    @classmethod
    def setUpTestData(cls):
        # bulk_create bypasses WikidataItem.save(), which fetches live
        # Wikidata metadata on insert.
        items = WikidataItem.objects.bulk_create(
            [
                WikidataItem(
                    wikidata_id="Q101",
                    title="Church Hill",
                    description="Neighborhood in Richmond",
                ),
                WikidataItem(wikidata_id="Q102", title="Monument Avenue"),
                WikidataItem(wikidata_id="Q103", title="Monumental Church"),
                WikidataItem(wikidata_id="Q104", title="Shockoe Bottom"),
                WikidataItem(wikidata_id="Q105", title="Shokoe Hill"),
            ]
        )
        for item in items:
            Subject.objects.create(title=item.title, wikidata_item=item)
        cls.url = reverse("subject_autocomplete")

    def _titles(self, q):
        response = self.client.get(self.url, {"q": q})
        self.assertEqual(response.status_code, 200)
        return [row["title"] for row in response.json()]

    def test_typo_match_found(self):
        self.assertIn("Church Hill", self._titles("chruch"))
        self.assertIn("Monument Avenue", self._titles("monumnet"))

    def test_mid_title_substring_still_matches(self):
        self.assertIn("Monument Avenue", self._titles("avenue"))

    def test_substring_beats_fuzzy(self):
        titles = self._titles("shockoe")
        self.assertIn("Shockoe Bottom", titles)
        self.assertIn("Shokoe Hill", titles)
        self.assertLess(titles.index("Shockoe Bottom"), titles.index("Shokoe Hill"))

    def test_whole_word_beats_prefix_within_tier(self):
        # Both are prefix matches for "monument"; the whole-word hit
        # (similarity 1.0) should sort above the partial-word one.
        titles = self._titles("monument")
        self.assertLess(
            titles.index("Monument Avenue"), titles.index("Monumental Church")
        )

    def test_short_and_missing_queries_return_empty(self):
        self.assertEqual(self._titles("a"), [])
        response = self.client.get(self.url)
        self.assertEqual(response.json(), [])

    def test_overlong_query_returns_empty(self):
        self.assertEqual(self._titles("x" * (_MAX_AUTOCOMPLETE_QUERY_LEN + 1)), [])

    def test_response_shape(self):
        response = self.client.get(self.url, {"q": "church"})
        rows = response.json()
        self.assertEqual(set(rows[0]), {"id", "title", "description", "wikidata_id"})
        by_title = {row["title"]: row for row in rows}
        self.assertEqual(
            by_title["Church Hill"]["description"], "Neighborhood in Richmond"
        )
        self.assertEqual(by_title["Church Hill"]["wikidata_id"], "Q101")

    def test_single_query(self):
        # select_related("wikidata_item") keeps the whole response at one
        # database query despite get_description() touching the item.
        with self.assertNumQueries(1):
            self.client.get(self.url, {"q": "church"})


class ManualWikidataRefreshQueueTests(TestCase):
    """Admin-queued items jump the Beat-driven refresh rotation."""

    @classmethod
    def setUpTestData(cls):
        # bulk_create sidesteps WikidataItem.save(), which would fetch
        # from Wikidata on creation.
        cls.fresh_item, cls.stale_item, cls.orphan_item = (
            WikidataItem.objects.bulk_create(
                [
                    WikidataItem(
                        wikidata_id="Q2001",
                        title="Fresh",
                        sparql_last_loaded_at=timezone.now(),
                    ),
                    WikidataItem(
                        wikidata_id="Q2002",
                        title="Stale",
                        sparql_last_loaded_at=timezone.now() - timedelta(days=30),
                    ),
                    WikidataItem(wikidata_id="Q2003", title="Orphan"),
                ]
            )
        )
        cls.fresh = Subject.objects.create(title="Fresh", wikidata_item=cls.fresh_item)
        cls.stale = Subject.objects.create(title="Stale", wikidata_item=cls.stale_item)

    def test_stale_item_wins_when_nothing_is_queued(self):
        self.assertIsNone(get_next_requested_wikidata_item())
        self.assertEqual(get_next_stale_wikidata_item(), self.stale_item)

    def test_queued_fresh_item_jumps_ahead_of_the_stale_one(self):
        self.fresh_item.queue_sparql_refresh()

        self.assertEqual(get_next_requested_wikidata_item(), self.fresh_item)
        # The staleness rotation is untouched — the queue is a separate lane.
        self.assertEqual(get_next_stale_wikidata_item(), self.stale_item)

    def test_queueing_clears_the_failure_cap(self):
        WikidataItem.objects.filter(pk=self.stale_item.pk).update(
            sparql_fetch_failures=99
        )
        self.assertIsNone(get_next_stale_wikidata_item())

        self.stale_item.refresh_from_db()
        self.stale_item.queue_sparql_refresh()

        self.assertEqual(get_next_requested_wikidata_item(), self.stale_item)
        self.stale_item.refresh_from_db()
        self.assertEqual(self.stale_item.sparql_fetch_failures, 0)

    def test_earliest_request_is_served_first(self):
        self.fresh_item.queue_sparql_refresh()
        self.stale_item.queue_sparql_refresh()

        self.assertEqual(get_next_requested_wikidata_item(), self.fresh_item)

    def test_items_without_a_seed_are_never_queued_up(self):
        self.orphan_item.queue_sparql_refresh()

        self.assertIsNone(get_next_requested_wikidata_item())


class SubjectAdminWikidataLinkTests(TestCase):
    """The one-to-one Wikidata link is set at creation and never edited."""

    @classmethod
    def setUpTestData(cls):
        item = WikidataItem.objects.bulk_create(
            [WikidataItem(wikidata_id="Q3001", title="Example Building")]
        )[0]
        cls.subject = Subject.objects.create(
            title="Example Building", wikidata_item=item
        )

    def setUp(self):
        self.admin = SubjectAdmin(Subject, AdminSite())
        self.request = RequestFactory().get("/")

    def test_add_form_still_picks_a_wikidata_item(self):
        fields = flatten_fieldsets(self.admin.get_fieldsets(self.request))

        self.assertIn("wikidata_item", fields)
        self.assertNotIn("wikidata_item_display", fields)

    def test_change_form_shows_a_read_only_link_instead(self):
        fields = flatten_fieldsets(
            self.admin.get_fieldsets(self.request, self.subject)
        )

        self.assertNotIn("wikidata_item", fields)
        self.assertIn("wikidata_item_display", fields)

    def test_change_form_cannot_submit_a_new_wikidata_item(self):
        form = self.admin.get_form(self.request, self.subject)

        self.assertNotIn("wikidata_item", form.base_fields)


# ---------------------------------------------------------------------------
# SA-01: subject tagging mutations resolve their image through a policy.
# ---------------------------------------------------------------------------


class SubjectMutationAuthorizationTests(TestCase):
    """Subject add, remove, reorder and representative-image writes."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("sa01_subject_user", password="pw")
        cls.staff = User.objects.create_user(
            "sa01_subject_staff", password="pw", is_staff=True
        )

        public_source = Source.objects.create(
            name="Subjects public source",
            slug="sa01-subj-public",
            url="https://example.com/subj-public",
            description="",
        )
        private_source = Source.objects.create(
            name="Subjects private source",
            slug="sa01-subj-private",
            url="https://example.com/subj-private",
            description="",
            public=False,
        )
        public_collection = Collection.objects.create(
            source=public_source, name="Public", slug="sa01-subj-public"
        )
        private_collection = Collection.objects.create(
            source=public_source,
            name="Private",
            slug="sa01-subj-private-collection",
            public=False,
        )
        private_source_collection = Collection.objects.create(
            source=private_source, name="Staged", slug="sa01-subj-staged"
        )

        def make_image(collection, title, **kwargs):
            return Image.objects.create(
                collection=collection,
                title=title,
                permalink=f"https://img.example.com/{title.replace(' ', '-')}.jpg",
                **kwargs,
            )

        cls.public_image = make_image(public_collection, "subj public")
        cls.second_public_image = make_image(public_collection, "subj public two")
        cls.private_image = make_image(private_collection, "subj private")
        cls.staged_image = make_image(private_source_collection, "subj staged")
        cls.duplicate_image = make_image(
            public_collection, "subj duplicate", duplicate_of=cls.public_image
        )

        # bulk_create skips WikidataItem.save(), which would otherwise try to
        # fetch the entity from Wikidata.
        cls.wikidata_item = WikidataItem.objects.bulk_create(
            [WikidataItem(wikidata_id="Q4200001", title="Existing subject")]
        )[0]
        cls.subject = Subject.objects.create(
            title="Existing subject",
            slug="sa01-existing-subject",
            wikidata_item=cls.wikidata_item,
        )

        cls.inaccessible_images = [
            cls.private_image,
            cls.staged_image,
            cls.duplicate_image,
        ]

    def post_json(self, url, payload=None):
        return self.client.post(
            url,
            data=json.dumps({} if payload is None else payload),
            content_type="application/json",
        )

    def map_subject(self, image):
        return SubjectMapping.objects.create(
            image=image, subject=self.subject, order=1
        )

    def test_add_subject_rejects_inaccessible_images(self):
        self.client.force_login(self.user)
        for image in self.inaccessible_images:
            with self.subTest(image=image.title):
                response = self.post_json(
                    reverse("subjects:add_subject_to_image", args=[image.id]),
                    {"wikidata_id": self.wikidata_item.wikidata_id},
                )
                self.assertEqual(response.status_code, 404)
        self.assertFalse(SubjectMapping.objects.exists())

    def test_add_subject_rejects_get(self):
        self.client.force_login(self.user)
        response = self.client.get(
            reverse("subjects:add_subject_to_image", args=[self.public_image.id])
        )
        self.assertEqual(response.status_code, 405)

    def test_add_subject_requires_authentication(self):
        response = self.post_json(
            reverse("subjects:add_subject_to_image", args=[self.public_image.id]),
            {"wikidata_id": self.wikidata_item.wikidata_id},
        )
        self.assertEqual(response.status_code, 403)

    def test_malformed_wikidata_ids_are_rejected(self):
        self.client.force_login(self.user)
        url = reverse("subjects:add_subject_to_image", args=[self.public_image.id])
        for value in ("", "Qfoo", "P31", "Q", "42", None, 42):
            with self.subTest(value=value):
                response = self.post_json(url, {"wikidata_id": value})
                self.assertEqual(response.status_code, 400)
        self.assertFalse(SubjectMapping.objects.exists())

    def test_add_subject_to_a_public_image_succeeds(self):
        self.client.force_login(self.user)
        response = self.post_json(
            reverse("subjects:add_subject_to_image", args=[self.public_image.id]),
            {"wikidata_id": self.wikidata_item.wikidata_id},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            SubjectMapping.objects.filter(
                image=self.public_image, subject=self.subject
            ).exists()
        )

    def test_remove_resolves_the_mapping_through_its_image(self):
        hidden_mapping = self.map_subject(self.private_image)
        self.client.force_login(self.user)

        response = self.post_json(
            reverse(
                "subjects:remove_subject_from_image", args=[hidden_mapping.id]
            )
        )
        self.assertEqual(response.status_code, 404)
        self.assertTrue(SubjectMapping.objects.filter(id=hidden_mapping.id).exists())

    def test_remove_of_a_visible_mapping_succeeds(self):
        mapping = self.map_subject(self.public_image)
        self.client.force_login(self.user)

        response = self.post_json(
            reverse("subjects:remove_subject_from_image", args=[mapping.id])
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(SubjectMapping.objects.filter(id=mapping.id).exists())

    def test_reorder_rejects_inaccessible_images(self):
        self.client.force_login(self.user)
        for image in self.inaccessible_images:
            with self.subTest(image=image.title):
                response = self.post_json(
                    reverse("subjects:reorder_subjects", args=[image.id]),
                    {"order": []},
                )
                self.assertEqual(response.status_code, 404)

    def test_reorder_rejects_mappings_from_another_image(self):
        mine = self.map_subject(self.public_image)
        theirs = SubjectMapping.objects.create(
            image=self.second_public_image, subject=self.subject, order=1
        )
        self.client.force_login(self.user)

        response = self.post_json(
            reverse("subjects:reorder_subjects", args=[self.public_image.id]),
            {"order": [str(theirs.id)]},
        )
        self.assertEqual(response.status_code, 400)
        mine.refresh_from_db()
        self.assertEqual(mine.order, 1)

    def test_bulk_add_is_all_or_nothing(self):
        self.client.force_login(self.user)
        response = self.post_json(
            reverse("bulk_add_subject_to_images"),
            {
                "image_ids": [self.public_image.id, self.private_image.id],
                "wikidata_id": self.wikidata_item.wikidata_id,
            },
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(SubjectMapping.objects.exists())

    def test_bulk_add_of_authorized_images_succeeds(self):
        self.client.force_login(self.user)
        response = self.post_json(
            reverse("bulk_add_subject_to_images"),
            {
                "image_ids": [self.public_image.id, self.second_public_image.id],
                "wikidata_id": self.wikidata_item.wikidata_id,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["added_count"], 2)

    @override_settings(BULK_MAX_IMAGE_IDS=1)
    def test_bulk_add_enforces_the_id_cap(self):
        self.client.force_login(self.user)
        response = self.post_json(
            reverse("bulk_add_subject_to_images"),
            {
                "image_ids": [self.public_image.id, self.second_public_image.id],
                "wikidata_id": self.wikidata_item.wikidata_id,
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(SubjectMapping.objects.exists())

    def test_representative_image_must_be_public_even_for_staff(self):
        self.map_subject(self.private_image)
        self.client.force_login(self.staff)

        response = self.post_json(
            reverse(
                "subjects:set_representative_image",
                args=[self.subject.id, self.private_image.id],
            )
        )
        self.assertEqual(response.status_code, 404)
        self.subject.refresh_from_db()
        self.assertIsNone(self.subject.representative_image)

    def test_representative_image_rejects_duplicates(self):
        self.map_subject(self.duplicate_image)
        self.client.force_login(self.staff)

        response = self.post_json(
            reverse(
                "subjects:set_representative_image",
                args=[self.subject.id, self.duplicate_image.id],
            )
        )
        self.assertEqual(response.status_code, 404)

    def test_representative_image_accepts_a_public_mapped_image(self):
        self.map_subject(self.public_image)
        self.client.force_login(self.user)

        response = self.post_json(
            reverse(
                "subjects:set_representative_image",
                args=[self.subject.id, self.public_image.id],
            )
        )
        self.assertEqual(response.status_code, 200)
        self.subject.refresh_from_db()
        self.assertEqual(self.subject.representative_image, self.public_image)

    def test_representative_image_requires_the_image_to_be_mapped(self):
        self.client.force_login(self.user)
        response = self.post_json(
            reverse(
                "subjects:set_representative_image",
                args=[self.subject.id, self.public_image.id],
            )
        )
        self.assertEqual(response.status_code, 400)

    def test_subject_mutation_endpoints_reject_get(self):
        self.client.force_login(self.user)
        mapping = self.map_subject(self.public_image)
        urls = [
            reverse("subjects:add_subject_to_image", args=[self.public_image.id]),
            reverse("subjects:remove_subject_from_image", args=[mapping.id]),
            reverse("subjects:reorder_subjects", args=[self.public_image.id]),
            reverse(
                "subjects:set_representative_image",
                args=[self.subject.id, self.public_image.id],
            ),
            reverse("bulk_add_subject_to_images"),
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 405)

    def test_subject_mutations_require_a_csrf_token(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)

        response = csrf_client.post(
            reverse("subjects:add_subject_to_image", args=[self.public_image.id]),
            data=json.dumps({"wikidata_id": self.wikidata_item.wikidata_id}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
