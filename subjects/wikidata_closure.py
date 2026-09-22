"""Fetch a subject's local Wikidata graph from the Wikidata Query Service.

Strategy: one CONSTRUCT query per subject to WDQS that returns the seed's
triples plus its neighbourhood — statement bodies, labels and class edges
for referenced entities and class ancestors, and descriptors of the
properties the seed uses. The response is parsed client-side with
pyoxigraph, grouped by Wikidata entity IRI, transformed into a
property-graph payload (``build_graph_payload``), and applied to Memgraph
in a single Bolt transaction that atomically replaces each entity's
mirrored data (``apply_closure``).

This means one external request per subject refresh instead of one per
entity, and a consistent snapshot (no risk of upstream edits landing
mid-walk).
"""

import logging
import re
from datetime import datetime

import pyoxigraph
from django.conf import settings
from django.contrib.gis.geos import Point
from django.core.exceptions import ImproperlyConfigured

from .sparql_safety import (
    UnsafeSparqlInput,
    looks_like_pid,
    looks_like_qid,
    validate_language_tag,
    validate_pid,
    validate_qid,
)

logger = logging.getLogger(__name__)

WDQS_ENDPOINT = "https://query.wikidata.org/sparql"
WIKIDATA_ENTITY_IRI_BASE = "http://www.wikidata.org/entity/"
# Wikidata statement nodes are IRIs like
# ``http://www.wikidata.org/entity/statement/Q42-D8404CDA-25E4-...``;
# the leading ``Q``-segment names the entity that asserts the claim.
WIKIDATA_STATEMENT_IRI_BASE = "http://www.wikidata.org/entity/statement/"
USER_AGENT = (
    "GeoreferenceTool/1.0 (https://github.com/mapRVA/georeference-tool; sparql-mirror)"
)

# WikidataItem fields populated by ``extract_seed_metadata`` plus the
# sparql-mirror bookkeeping fields. Callers pass this as ``update_fields``
# so the seed row save touches only what the closure actually rewrites
# (and avoids re-entering ``WikidataItem.save()``'s is_new branch, which
# would trigger another WDQS fetch).
SEED_METADATA_FIELDS = (
    "title",
    "description",
    "wikipedia_url",
    "architect",
    "image_url",
    "inception",
    "demolished",
    "sparql_last_loaded_at",
    "sparql_fetch_failures",
)

RDFS_LABEL_IRI = "http://www.w3.org/2000/01/rdf-schema#label"
SCHEMA_DESCRIPTION_IRI = "http://schema.org/description"
SCHEMA_ABOUT_IRI = "http://schema.org/about"
WDT_P18_IRI = "http://www.wikidata.org/prop/direct/P18"
WDT_P84_IRI = "http://www.wikidata.org/prop/direct/P84"
WDT_P571_IRI = "http://www.wikidata.org/prop/direct/P571"
WDT_P576_IRI = "http://www.wikidata.org/prop/direct/P576"
WDT_P625_IRI = "http://www.wikidata.org/prop/direct/P625"
COMMONS_FILEPATH_PREFIX = "http://commons.wikimedia.org/wiki/Special:FilePath/"
EN_WIKIPEDIA_PREFIX = "https://en.wikipedia.org/"

# Wikidata serializes P625 as a geo:wktLiteral, "Point(<lon> <lat>)" —
# longitude first, matching WKT (and GEOS) axis order rather than the
# lat/long order the Wikidata UI displays. Coordinates on another globe
# carry a leading "<globe IRI>" prefix; Earth is the implicit default and
# usually omitted.
WIKIDATA_EARTH_IRI = f"{WIKIDATA_ENTITY_IRI_BASE}Q2"
_WKT_POINT_RE = re.compile(
    r"^Point\(\s*(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*\)$",
    re.IGNORECASE,
)

# IRI namespaces classified by ``build_graph_payload``. ``wdt:`` direct
# claims, ``p:`` statement links, and ``ps:``/``pq:`` statement
# predicates all share the ``/prop/`` base, so the specific prefixes
# must be matched before the bare ``p:`` catch-all.
WDT_IRI_BASE = "http://www.wikidata.org/prop/direct/"
WDT_NORM_IRI_BASE = "http://www.wikidata.org/prop/direct-normalized/"
P_IRI_BASE = "http://www.wikidata.org/prop/"
PS_IRI_BASE = "http://www.wikidata.org/prop/statement/"
PQ_IRI_BASE = "http://www.wikidata.org/prop/qualifier/"
SKOS_ALT_LABEL_IRI = "http://www.w3.org/2004/02/skos/core#altLabel"
WIKIBASE_RANK_IRI = "http://wikiba.se/ontology#rank"
WIKIBASE_DIRECT_CLAIM_IRI = "http://wikiba.se/ontology#directClaim"
PROV_DERIVED_FROM_IRI = "http://www.w3.org/ns/prov#wasDerivedFrom"

# The only node labels ``build_graph_payload`` emits; ``apply_closure``
# refuses anything else before interpolating a label into Cypher.
_NODE_LABELS = frozenset({"Entity", "Property"})

# ---------------------------------------------------------------------------
# Closure CONSTRUCT assembly.
#
# The query is composed from the named fragments below so each concern is
# documented next to its SPARQL, as ``#`` comments that travel with the
# assembled query into the WDQS GUI and logs. Fragments are ``str.format``
# templates sharing two placeholders — ``{qid}`` (validated seed Q-ID) and
# ``{lang_filter}`` (literal-language guard built from
# ``settings.WIKIDATA_MIRROR_LANGUAGES`` at call time) — with literal SPARQL
# braces doubled.
#
# Every branch either is a self-contained pattern (seed, sitelink) or reads
# "select neighbour ?entity rows, then emit a profile of triples about
# them". Two shared emission profiles:
#   - full (``_TAIL_FULL``): every triple, literals language-filtered
#   - nav  (``_TAIL_NAV``):  labels, descriptions, and class edges only —
#     enough to name an entity and place it in the P31/P279 ontology
#     without pulling its statement bodies, which would balloon the closure
#
# Region seeds (``closure_query(..., include_p131=True)``) get one extra
# branch, ``_BRANCH_P131``, whose tail (``_TAIL_NAV_P131``) is the nav
# profile plus wdt:P131 itself — see the branch's own comment.
# ---------------------------------------------------------------------------

_TAIL_FULL = """\
    ?entity ?p ?o .
    {lang_filter}"""

_TAIL_NAV = """\
    ?entity ?p ?o .
    FILTER(?p IN (rdfs:label, skos:altLabel, schema:description,
                  wdt:P31, wdt:P279))
    {lang_filter}"""

_TAIL_NAV_P131 = """\
    ?entity ?p ?o .
    FILTER(?p IN (rdfs:label, skos:altLabel, schema:description,
                  wdt:P31, wdt:P279, wdt:P131))
    {lang_filter}"""

_BRANCH_SEED = (
    """\
    # Seed: every triple about the subject itself - the truthy wdt:*
    # values plus the reified p:* links to the statement nodes that the
    # full-profile branch follows.
    BIND(wd:{qid} AS ?entity)
"""
    + _TAIL_FULL
)

_BRANCH_FULL = (
    """\
    # Full-profile neighbours - every (language-filtered) triple about:
    # (a) the seed's statement nodes: carries qualifiers (pq:*), typed
    #     main values (ps:*), rank, and the prov:wasDerivedFrom link.
    #     parse_closure routes these into the seed's named graph, so reads
    #     can walk ?s p:Pxx ?stmt . ?stmt pq:Pxx ?q without crossing
    #     graphs.
    # (b) entities the seed points to via truthy wdt:* claims: brands,
    #     architects, locations, ... - full data for display and category
    #     autocomplete without a second round trip. Their own reified
    #     statements are NOT pulled; that would balloon the closure.
    {{
      wd:{qid} ?stmt_link ?entity .
      FILTER(STRSTARTS(STR(?stmt_link), "http://www.wikidata.org/prop/P"))
    }} UNION {{
      wd:{qid} ?direct_pred ?entity .
      FILTER(STRSTARTS(STR(?direct_pred), "http://www.wikidata.org/prop/direct/"))
      FILTER(isIRI(?entity))
      FILTER(STRSTARTS(STR(?entity), "http://www.wikidata.org/entity/Q"))
      FILTER(?entity != wd:{qid})
    }}
"""
    + _TAIL_FULL
)

_BRANCH_NAV = (
    """\
    # Nav-profile neighbours - names and ontology placement only, for:
    # (a) entities referenced by the seed's statement nodes (ps:* main
    #     values and pq:* qualifiers), e.g. the historic district named in
    #     a pq:P361 on a heritage-designation claim; without labels here
    #     the category autocomplete's qualifier arm drops them.
    # (b) class ancestors via wdt:P31?/wdt:P279*.
    # (c) the "Wikidata item of this property" (P1629) target of each
    #     cultural-heritage authority-control property (P31 Q18618628) the
    #     seed uses - the register (e.g. National Register of Historic
    #     Places) whose label fetch_authority_ids displays. Scoped to
    #     authority properties to limit entity fan-out; drop the P31
    #     constraint here if other property items become useful.
    {{
      wd:{qid} ?stmt_link ?stmt .
      FILTER(STRSTARTS(STR(?stmt_link), "http://www.wikidata.org/prop/P"))
      ?stmt ?stmt_pred ?entity .
      FILTER(STRSTARTS(STR(?stmt_pred), "http://www.wikidata.org/prop/statement/") ||
             STRSTARTS(STR(?stmt_pred), "http://www.wikidata.org/prop/qualifier/"))
      FILTER(isIRI(?entity))
      FILTER(STRSTARTS(STR(?entity), "http://www.wikidata.org/entity/Q"))
    }} UNION {{
      wd:{qid} wdt:P31?/wdt:P279* ?entity .
    }} UNION {{
      wd:{qid} ?claim ?claim_value .
      ?authprop wikibase:directClaim ?claim .
      ?authprop wdt:P31 wd:Q18618628 .
      ?authprop wdt:P1629 ?entity .
    }}
    FILTER(?entity != wd:{qid})
"""
    + _TAIL_NAV
)

_BRANCH_PROP_DESCRIPTORS = """\
    # Property descriptors: for every property the seed uses as a truthy
    # claim, mirror the property entity's own descriptor triples - label,
    # the wikibase:directClaim bridge to its wdt: predicate, class markers
    # (wdt:P31, e.g. Q18618628 marks cultural-heritage authority-control
    # properties), the P1630 formatter URL, and the P1629 link to the
    # property's Wikidata item. parse_closure routes these wd:Pxx subjects
    # into per-property named graphs; reads join them back to the seed's
    # raw values (see subject_facts.fetch_authority_ids), and future
    # subject-page widgets get property labels without closure changes.
    wd:{qid} ?claim ?claim_value .
    ?entity wikibase:directClaim ?claim .
    ?entity ?p ?o .
    FILTER(?p IN (rdfs:label, wikibase:directClaim, wdt:P31, wdt:P1630,
                  wdt:P1629))
    {lang_filter}"""

_BRANCH_SITELINK = """\
    # English Wikipedia sitelink. The article URL is the *subject* of
    # these triples, so parse_closure's entity grouping skips them;
    # extract_seed_metadata reads them into WikidataItem.wikipedia_url.
    ?article schema:about wd:{qid} .
    ?article schema:isPartOf <https://en.wikipedia.org/> ."""

_BRANCH_P131 = (
    """\
    # Administrative containment chain (region seeds only): every entity
    # the seed transitively sits in via P131 (located in the
    # administrative territorial entity). The tail is the nav profile
    # plus wdt:P131 itself, so each chain entity's own containment edge
    # lands in the mirror and Richmond -> Virginia -> USA is walkable
    # there. Chain entities' class *closures* are not walked; their
    # direct P31/P279 edges come along via the nav predicates.
    wd:{qid} wdt:P131+ ?entity .
    FILTER(?entity != wd:{qid})
"""
    + _TAIL_NAV_P131
)

_CLOSURE_BRANCHES = (
    _BRANCH_SEED,
    _BRANCH_FULL,
    _BRANCH_NAV,
    _BRANCH_PROP_DESCRIPTORS,
    _BRANCH_SITELINK,
)

_REGION_CLOSURE_BRANCHES = _CLOSURE_BRANCHES + (_BRANCH_P131,)


def _assemble_template(branches):
    """Join branch fragments into one CONSTRUCT ``str.format`` template."""
    return (
        """\
PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX wikibase: <http://wikiba.se/ontology#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX schema: <http://schema.org/>

CONSTRUCT {{
  ?entity ?p ?o .
  ?article schema:about wd:{qid} .
  ?article schema:isPartOf <https://en.wikipedia.org/> .
}} WHERE {{
  {{
"""
        + "\n  }} UNION {{\n".join(branches)
        + """
  }}
}}
"""
    )


_CLOSURE_QUERY_TEMPLATE = _assemble_template(_CLOSURE_BRANCHES)
_REGION_CLOSURE_QUERY_TEMPLATE = _assemble_template(_REGION_CLOSURE_BRANCHES)


def _language_filter():
    """Build the literal-language FILTER from ``WIKIDATA_MIRROR_LANGUAGES``.

    Language-tagged literals outside the configured set are dropped; plain
    literals (dates, external identifiers, formatter URLs) always pass.
    Built per call so the setting stays test-overridable and importing
    this module never touches Django settings.
    """
    configured_langs = settings.WIKIDATA_MIRROR_LANGUAGES
    if not configured_langs:
        raise ImproperlyConfigured("WIKIDATA_MIRROR_LANGUAGES must not be empty")
    langs = list(configured_langs)
    if "mul" not in langs:
        langs.append("mul")
    tags = ", ".join(f'"{validate_language_tag(lang)}"' for lang in langs)
    return f'FILTER(!isLiteral(?o) || lang(?o) IN ({tags}) || lang(?o) = "")'


def closure_query(qid, *, include_p131=False):
    """Build the CONSTRUCT for the seed + its closure neighbourhood.

    ``include_p131=True`` is the region-seed variant: it appends
    ``_BRANCH_P131``, which walks the seed's transitive P131 containment
    chain so the administrative hierarchy lands in the mirror. Subject
    closures (the default) are unchanged by its existence.

    Per-branch documentation lives on the ``_BRANCH_*`` fragment constants
    above and is carried into the assembled query as SPARQL comments.
    Cross-cutting notes:

    - Literals in every branch are restricted to
      ``settings.WIKIDATA_MIRROR_LANGUAGES`` (plus plain literals). Reads
      and ``extract_seed_metadata`` are English-only today, so keep ``en``
      in the list; adding languages there is the first step of any future
      localization, followed by read-side changes.
    - Reference bodies (``pr:*`` triples on reference nodes) are not
      pulled - the ``prov:wasDerivedFrom`` link comes along, but following
      it to the citation details would require another branch and a
      value-node-aware parser. Add later if a query needs it.
    - ``parse_closure`` routes the response into named graphs (statement
      nodes into their owning entity's graph, ``wd:Pxx`` property
      descriptors into per-property graphs); ``extract_seed_metadata``
      picks the sitelink triples out separately.
    """
    validate_qid(qid)
    template = (
        _REGION_CLOSURE_QUERY_TEMPLATE if include_p131 else _CLOSURE_QUERY_TEMPLATE
    )
    return template.format(qid=qid, lang_filter=_language_filter())


def fetch_closure_turtle(session, qid, timeout=60, *, include_p131=False):
    """Run the closure CONSTRUCT against WDQS, return Turtle bytes.

    Uses POST (recommended by WDQS for arbitrary query bodies) and asks
    for Turtle so we can hand it straight to pyoxigraph.
    """
    response = session.post(
        WDQS_ENDPOINT,
        data={"query": closure_query(qid, include_p131=include_p131)},
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/turtle",
        },
        timeout=timeout,
    )
    response.raise_for_status()
    return response.content


class ClosureLoadError(Exception):
    """Raised when fetch/parse of a closure doesn't return a usable seed.

    Network failures from WDQS surface as ``requests`` exceptions and
    aren't wrapped - callers that care about that distinction can catch
    them directly.
    """


# How much of the offending Turtle line a parse error quotes back.
_PARSE_ERROR_SNIPPET_CHARS = 200


def _describe_parse_error(turtle_bytes, lineno):
    """Explain where closure Turtle stopped parsing, for the error message.

    A body that doesn't end in the ``.`` that closes every Turtle
    statement was cut off in transit rather than malformed at the source:
    WDQS streams CONSTRUCT results and abandons the stream when a query
    outruns its time limit, so the client gets HTTP 200 and a document
    that ends mid-triple. Worth distinguishing - a truncated response
    means retry (or a cheaper closure), bad data means fix the data.
    """
    if turtle_bytes.rstrip()[-1:] != b".":
        return (
            f"response ends mid-statement after {len(turtle_bytes):,} bytes, "
            "so it was truncated in transit (typically a WDQS query timeout)"
        )
    lines = turtle_bytes.split(b"\n")
    if not lineno or lineno > len(lines):
        return "response is not valid Turtle"
    snippet = lines[lineno - 1][:_PARSE_ERROR_SNIPPET_CHARS].decode("utf-8", "replace")
    return f"line {lineno} reads: {snippet}"


def _parse_turtle(turtle_bytes):
    """Yield quads from closure Turtle, as ``ClosureLoadError`` on bad syntax.

    pyoxigraph reports a malformed document by raising the builtin
    ``SyntaxError`` mid-iteration, which is not an exception any caller of
    this module thinks to catch - an unparseable WDQS response would take
    down the whole refresh task, leaving ``sparql_fetch_failures``
    unincremented so the item never ages out of the rotation. Translating
    it here routes it into the same handling as any other unusable
    closure.
    """
    quads = pyoxigraph.parse(turtle_bytes, format=pyoxigraph.RdfFormat.TURTLE)
    while True:
        try:
            quad = next(quads)
        except StopIteration:
            return
        except SyntaxError as e:
            raise ClosureLoadError(
                f"WDQS returned unparseable Turtle ({e.msg}); "
                f"{_describe_parse_error(turtle_bytes, e.lineno)}"
            ) from e
        yield quad


def parse_closure(turtle_bytes):
    """Parse closure Turtle, group by Wikidata entity IRI, collect labels.

    Returns ``(groups, labels)``:
      - ``groups``: ``{entity_iri: [pyoxigraph.Triple, ...]}`` - triples
        keyed by the named graph they belong in. Entity-subject triples
        go under the entity's own IRI; statement-node triples go under
        the owning entity's IRI (parsed out of the statement IRI's
        ``Q``-prefix), so a SPARQL query can traverse
        ``?entity p:Pxxx ?stmt . ?stmt pq:Pxxx ?qval`` without crossing
        graphs. Property descriptors (``wd:Pxxx`` subjects, from the
        property-descriptor branch) go under per-property graph IRIs -
        stored once, shared by every subject that uses the property. Blank
        nodes, value nodes, and reference nodes are dropped.
      - ``labels``: ``{qid: preferred_label}`` - English when present,
        otherwise Wikidata's ``mul`` default, for populating a freshly-created
        ``WikidataItem.title`` without an extra fetch.

    Raises ``ClosureLoadError`` if the Turtle is malformed or truncated.
    """
    groups = {}
    labels = {}
    label_priorities = {"mul": 1, "en": 2}
    selected_label_priorities = {}
    for quad in _parse_turtle(turtle_bytes):
        subj = quad.subject
        if not isinstance(subj, pyoxigraph.NamedNode):
            continue
        iri = subj.value

        # Statement nodes (``entity/statement/Q{n}-{uuid}``) share the
        # entity IRI base but are routed into the owning entity's graph,
        # not their own. Check this before the entity-base branch since
        # the statement prefix is a superset of the entity prefix.
        if iri.startswith(WIKIDATA_STATEMENT_IRI_BASE):
            suffix = iri.removeprefix(WIKIDATA_STATEMENT_IRI_BASE)
            # Statements minted by older Wikibase versions carry a
            # lowercase entity segment (``statement/q123-...``); uppercase
            # it so their bodies land in the (canonical-IRI) entity graph
            # instead of being dropped as invalid.
            owning_qid = suffix.split("-", 1)[0].upper()
            try:
                validate_qid(owning_qid)
            except UnsafeSparqlInput:
                continue
            graph_iri = f"{WIKIDATA_ENTITY_IRI_BASE}{owning_qid}"
            groups.setdefault(graph_iri, []).append(
                pyoxigraph.Triple(quad.subject, quad.predicate, quad.object)
            )
            continue

        if not iri.startswith(WIKIDATA_ENTITY_IRI_BASE):
            continue
        suffix = iri.removeprefix(WIKIDATA_ENTITY_IRI_BASE)

        # Property descriptors (``wd:Pxxx`` subjects) mirror into
        # per-property groups (``:Property`` nodes downstream), refreshed
        # whenever any subject using the property refreshes. Skipped by
        # the label collection below: ``WikidataItem`` rows are
        # Q-entities only.
        if looks_like_pid(suffix):
            groups.setdefault(iri, []).append(
                pyoxigraph.Triple(quad.subject, quad.predicate, quad.object)
            )
            continue

        # Reject anything whose suffix isn't a Q-ID before it lands in
        # ``groups`` and becomes a node id downstream. Drops subjects
        # that share the entity-IRI prefix but aren't entities we want
        # to mirror.
        qid = suffix
        try:
            validate_qid(qid)
        except UnsafeSparqlInput:
            continue

        groups.setdefault(iri, []).append(
            pyoxigraph.Triple(quad.subject, quad.predicate, quad.object)
        )

        if quad.predicate.value == RDFS_LABEL_IRI and isinstance(
            quad.object, pyoxigraph.Literal
        ):
            language = quad.object.language
            priority = label_priorities.get(language, 0)
            if priority > selected_label_priorities.get(qid, 0):
                labels[qid] = quad.object.value
                selected_label_priorities[qid] = priority
    return groups, labels


def _parse_closure_date(literal):
    """Parse a ``wdt:`` time literal to a ``date``, or ``None`` if unusable.

    CE dates only: BCE literals carry a leading ``-`` and so fail the
    ``%Y-%m-%d`` parse, matching the entity-JSON path's behaviour.
    """
    try:
        return datetime.strptime(literal.value[:10], "%Y-%m-%d").date()
    except ValueError, TypeError:
        return None


def parse_wikidata_point(value):
    """Parse a ``P625`` WKT literal string to a ``Point``, or ``None``.

    Shared by both P625 read paths: the closure Turtle parsed here, and
    the synchronous WDQS SELECT the region admin runs
    (``regions.wikidata_check``).

    Rejects anything that isn't a plain terrestrial point: coordinates on
    another globe (Mars, the Moon) and out-of-range values would both be
    meaningless as a region's location, and non-point geometries aren't
    valid P625 values in the first place.
    """
    value = value.strip()
    if value.startswith("<"):
        globe, _, remainder = value.partition(">")
        if globe.removeprefix("<") != WIKIDATA_EARTH_IRI:
            return None
        value = remainder.strip()

    match = _WKT_POINT_RE.match(value)
    if match is None:
        return None
    longitude, latitude = float(match.group(1)), float(match.group(2))
    if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
        return None
    return Point(longitude, latitude, srid=4326)


def extract_seed_metadata(turtle_bytes, qid):
    """Pull ``WikidataItem`` metadata fields for ``qid`` out of the closure Turtle.

    Reads the same Turtle that ``parse_closure`` consumes - re-parses
    because pyoxigraph's parser is single-pass. The Turtle is small (a
    few thousand triples per subject), so the double-parse is cheap and
    keeps grouping and metadata extraction as independent concerns.

    Returns a dict matching the JSON-API code path it replaces:
      - ``title``: English ``rdfs:label``, falling back to the ``mul`` default
      - ``description``: English ``schema:description``
      - ``wikipedia_url``: English Wikipedia article URL (or ``""``)
      - ``architect``: comma-joined ``Wikidata:Qxxx`` strings from
        ``wdt:P84`` (or ``""``); preserves the existing storage shape
      - ``image_url``: Commons ``Special:Redirect`` URL derived from
        ``wdt:P18``, or ``""``
      - ``inception``: ``datetime.date`` from ``wdt:P571`` (CE dates
        only - matches the JSON path's behavior), or ``None``
      - ``demolished``: ``datetime.date`` from ``wdt:P576`` (dissolved,
        abolished or demolished), same parsing rules, or ``None``
      - ``coordinate_location``: ``Point`` from ``wdt:P625``, or ``None``.
        The one key here that isn't a ``WikidataItem`` field — it lands on
        ``Region.wikidata_coordinate_location`` for region seeds (see
        ``subjects.tasks._do_refresh_wikidata_item``) and is ignored for
        everything else. Extracted here anyway because this is the only
        pass over the seed's triples.

    Raises ``ClosureLoadError`` if the Turtle is malformed or truncated.
    """
    seed_iri = f"{WIKIDATA_ENTITY_IRI_BASE}{qid}"
    english_title = ""
    default_title = ""
    description = ""
    wikipedia_url = ""
    architects = []
    image_url = ""
    inception = None
    demolished = None
    coordinate_location = None

    for quad in _parse_turtle(turtle_bytes):
        subj = quad.subject
        if not isinstance(subj, pyoxigraph.NamedNode):
            continue
        pred = quad.predicate.value
        obj = quad.object

        if subj.value == seed_iri:
            if pred == RDFS_LABEL_IRI and isinstance(obj, pyoxigraph.Literal):
                if obj.language == "en" and not english_title:
                    english_title = obj.value
                elif obj.language == "mul" and not default_title:
                    default_title = obj.value
            elif pred == SCHEMA_DESCRIPTION_IRI and isinstance(obj, pyoxigraph.Literal):
                if obj.language == "en" and not description:
                    description = obj.value
            elif pred == WDT_P84_IRI and isinstance(obj, pyoxigraph.NamedNode):
                if obj.value.startswith(WIKIDATA_ENTITY_IRI_BASE):
                    architect_qid = obj.value.removeprefix(WIKIDATA_ENTITY_IRI_BASE)
                    architects.append(f"Wikidata:{architect_qid}")
            elif pred == WDT_P18_IRI and isinstance(obj, pyoxigraph.NamedNode):
                if not image_url and obj.value.startswith(COMMONS_FILEPATH_PREFIX):
                    filename = obj.value.removeprefix(COMMONS_FILEPATH_PREFIX)
                    image_url = (
                        "https://commons.wikimedia.org/w/index.php"
                        f"?title=Special:Redirect/file/{filename}&width=300"
                    )
            elif pred == WDT_P571_IRI and isinstance(obj, pyoxigraph.Literal):
                if inception is None:
                    inception = _parse_closure_date(obj)
            elif pred == WDT_P576_IRI and isinstance(obj, pyoxigraph.Literal):
                if demolished is None:
                    demolished = _parse_closure_date(obj)
            elif pred == WDT_P625_IRI and isinstance(obj, pyoxigraph.Literal):
                if coordinate_location is None:
                    coordinate_location = parse_wikidata_point(obj.value)
        elif (
            pred == SCHEMA_ABOUT_IRI
            and isinstance(obj, pyoxigraph.NamedNode)
            and obj.value == seed_iri
            and subj.value.startswith(EN_WIKIPEDIA_PREFIX)
            and not wikipedia_url
        ):
            wikipedia_url = subj.value

    return {
        "title": english_title or default_title,
        "description": description,
        "wikipedia_url": wikipedia_url,
        "architect": ", ".join(architects),
        "image_url": image_url,
        "inception": inception,
        "demolished": demolished,
        "coordinate_location": coordinate_location,
    }


def _append_value(props, key, value):
    """Append ``value`` to the list at ``props[key]``, deduping (RDF sets)."""
    values = props.setdefault(key, [])
    if value not in values:
        values.append(value)


def _language_key(base, literal):
    """Property key like ``label_en`` / ``description_en_gb`` for a literal.

    Returns ``None`` (drop) for untagged or malformed language tags —
    Wikidata labels, descriptions, and aliases are always tagged.
    """
    lang = literal.language
    if not lang:
        return None
    try:
        validate_language_tag(lang)
    except UnsafeSparqlInput:
        return None
    return f"{base}_{lang.lower().replace('-', '_')}"


def _wikidata_suffix(term):
    """The Q/P suffix of a ``wd:`` NamedNode, or ``None`` for anything else."""
    if not isinstance(term, pyoxigraph.NamedNode):
        return None
    iri = term.value
    if iri.startswith(WIKIDATA_STATEMENT_IRI_BASE):
        return None
    if not iri.startswith(WIKIDATA_ENTITY_IRI_BASE):
        return None
    return iri.removeprefix(WIKIDATA_ENTITY_IRI_BASE)


def _apply_statement_triple(record, pred, obj, add_statement_edge):
    """Fold one statement-subject triple into its ``:Statement`` record."""
    if pred == WIKIBASE_RANK_IRI:
        if isinstance(obj, pyoxigraph.NamedNode) and "rank" not in record["props"]:
            record["props"]["rank"] = obj.value.rsplit("#", 1)[-1]
        return
    if pred == PROV_DERIVED_FROM_IRI:
        if isinstance(obj, pyoxigraph.NamedNode):
            _append_value(record["props"], "derived_from", obj.value)
        return
    for base, kind, prop_prefix in (
        (PS_IRI_BASE, "VALUE", "ps_"),
        (PQ_IRI_BASE, "QUALIFIER", "pq_"),
    ):
        if pred.startswith(base):
            pid = pred.removeprefix(base)
            if not looks_like_pid(pid):
                return  # psv:/psn:/pqv:/pqn: value nodes
            if kind == "VALUE" and record["pid"] is None:
                # The main-value predicate names the statement's property;
                # normally the entity's p: link sets this first.
                record["pid"] = pid
            suffix = _wikidata_suffix(obj)
            if suffix is not None and looks_like_qid(suffix):
                add_statement_edge(record["id"], kind, pid, suffix)
            elif isinstance(obj, (pyoxigraph.Literal, pyoxigraph.NamedNode)):
                _append_value(record["props"], f"{prop_prefix}{pid}", obj.value)
            return
    logger.debug("build_graph_payload: dropping statement predicate %s", pred)


def build_graph_payload(groups):
    """Transform ``parse_closure`` groups into a property-graph payload.

    Pure function, no I/O — the unit-testable spec of the data model that
    ``apply_closure`` writes to Memgraph:

    - ``entities`` / ``properties``: one node per group (``:Entity`` for
      Q-suffixed groups, ``:Property`` for P-suffixed descriptor groups)
      carrying ``label_{lang}`` / ``description_{lang}`` (first literal
      wins, baking in the old reads' ``SAMPLE``), ``aliases_{lang}``
      lists, and a ``P{n}`` list per literal-valued direct claim.
    - ``direct_edges``: entity-valued ``wdt:`` claims, keyed by
      ``(source label, PID, target label)`` — each key becomes one batch
      of dynamically typed ``[:P{n}]`` relationships.
    - ``statements`` / ``statement_edges``: reified statements as
      ``:Statement`` nodes owned by their group's entity, carrying
      ``ps_P{n}`` / ``pq_P{n}`` literal lists, ``rank``,
      ``derived_from``, and ``VALUE`` / ``QUALIFIER`` edges (with a
      ``pid`` property) for entity-valued statement objects.

    Dropped, matching what the RDF reads never consumed: normalized
    values (``wdtn:`` / ``psv:`` / ``psn:`` / ``pqv:`` / ``pqn:``),
    ``wikibase:directClaim`` (that join becomes node-id equality),
    blank-node objects (unknown values), and unclassified ontology
    predicates (debug-logged).
    """
    entities = []
    properties = []
    direct_edges = {}
    statements = {}
    statement_edges = []
    seen_direct_edges = set()
    seen_statement_edges = set()

    def touch_statement(stmt_id, owner):
        record = statements.get(stmt_id)
        if record is None:
            record = {"id": stmt_id, "owner": owner, "pid": None, "props": {}}
            statements[stmt_id] = record
        return record

    def add_direct_edge(src_label, pid, src, dst_label, dst):
        key = (src, pid, dst)
        if key in seen_direct_edges:
            return
        seen_direct_edges.add(key)
        direct_edges.setdefault((src_label, pid, dst_label), []).append(
            {"src": src, "dst": dst}
        )

    def add_statement_edge(stmt_id, kind, pid, dst):
        key = (stmt_id, kind, pid, dst)
        if key in seen_statement_edges:
            return
        seen_statement_edges.add(key)
        statement_edges.append({"stmt": stmt_id, "kind": kind, "pid": pid, "dst": dst})

    for graph_iri, triples in groups.items():
        owner = graph_iri.removeprefix(WIKIDATA_ENTITY_IRI_BASE)
        node_label = "Property" if looks_like_pid(owner) else "Entity"
        node_props = {}

        for triple in triples:
            pred = triple.predicate.value
            obj = triple.object

            if triple.subject.value.startswith(WIKIDATA_STATEMENT_IRI_BASE):
                stmt_id = triple.subject.value.removeprefix(WIKIDATA_STATEMENT_IRI_BASE)
                _apply_statement_triple(
                    touch_statement(stmt_id, owner), pred, obj, add_statement_edge
                )
                continue

            if pred in (RDFS_LABEL_IRI, SCHEMA_DESCRIPTION_IRI):
                if not isinstance(obj, pyoxigraph.Literal):
                    continue
                base = "label" if pred == RDFS_LABEL_IRI else "description"
                key = _language_key(base, obj)
                if key is not None and key not in node_props:
                    node_props[key] = obj.value
            elif pred == SKOS_ALT_LABEL_IRI:
                if isinstance(obj, pyoxigraph.Literal):
                    key = _language_key("aliases", obj)
                    if key is not None:
                        _append_value(node_props, key, obj.value)
            elif pred == WIKIBASE_DIRECT_CLAIM_IRI or pred.startswith(
                WDT_NORM_IRI_BASE
            ):
                continue
            elif pred.startswith(WDT_IRI_BASE):
                pid = pred.removeprefix(WDT_IRI_BASE)
                if not looks_like_pid(pid):
                    continue
                suffix = _wikidata_suffix(obj)
                if suffix is not None and looks_like_qid(suffix):
                    add_direct_edge(node_label, pid, owner, "Entity", suffix)
                elif suffix is not None and looks_like_pid(suffix):
                    add_direct_edge(node_label, pid, owner, "Property", suffix)
                elif isinstance(obj, (pyoxigraph.Literal, pyoxigraph.NamedNode)):
                    # Literals and non-entity IRIs (Commons file paths,
                    # external URLs) both keep their string form.
                    _append_value(node_props, pid, obj.value)
            elif pred.startswith(PS_IRI_BASE) or pred.startswith(PQ_IRI_BASE):
                continue  # statement predicate on a non-statement subject
            elif pred.startswith(P_IRI_BASE):
                pid = pred.removeprefix(P_IRI_BASE)
                if not looks_like_pid(pid):
                    continue
                if isinstance(obj, pyoxigraph.NamedNode) and obj.value.startswith(
                    WIKIDATA_STATEMENT_IRI_BASE
                ):
                    stmt_id = obj.value.removeprefix(WIKIDATA_STATEMENT_IRI_BASE)
                    record = touch_statement(stmt_id, owner)
                    if record["pid"] is None:
                        record["pid"] = pid
            else:
                logger.debug("build_graph_payload: dropping predicate %s", pred)

        target = entities if node_label == "Entity" else properties
        target.append({"id": owner, "props": node_props})

    return {
        "entities": entities,
        "properties": properties,
        "direct_edges": direct_edges,
        "statements": list(statements.values()),
        "statement_edges": statement_edges,
    }


def apply_closure(tx, payload):
    """Apply a ``build_graph_payload`` result inside one write transaction.

    Reproduces the old per-entity ``DROP GRAPH`` + ``INSERT DATA``
    semantics: each mirrored node's *owned* data — its properties (except
    ``id``), outgoing relationships, and ``:Statement`` nodes — is wiped
    and rewritten, while the node itself, its incoming edges (written by
    other entities' closures), and its extra labels (``:ProjectSubject``)
    survive. Runs inside one managed transaction, so all entities swap
    together or none do, and the write is idempotent for the driver's
    transient-error retries.
    """
    entity_ids = [entity["id"] for entity in payload["entities"]]
    property_ids = [prop["id"] for prop in payload["properties"]]

    tx.run(
        "UNWIND $ids AS id "
        "MATCH (:Entity {id: id})-[:STATEMENT]->(st:Statement) "
        "DETACH DELETE st",
        ids=entity_ids,
    )
    for label, ids in (("Entity", entity_ids), ("Property", property_ids)):
        tx.run(
            f"UNWIND $ids AS id MATCH (:{label} {{id: id}})-[r]->() DELETE r",
            ids=ids,
        )

    for label, nodes in (
        ("Entity", payload["entities"]),
        ("Property", payload["properties"]),
    ):
        tx.run(
            f"UNWIND $nodes AS node "
            f"MERGE (n:{label} {{id: node.id}}) "
            f"SET n = {{id: node.id}} "
            f"SET n += node.props",
            nodes=nodes,
        )

    # Relationship types cannot be parameterized in Cypher, so the PID
    # (and the label pair) are validated before interpolation. MERGE on
    # the target creates propertyless stubs for entities the closure
    # references but doesn't mirror.
    for (src_label, pid, dst_label), rows in payload["direct_edges"].items():
        if src_label not in _NODE_LABELS or dst_label not in _NODE_LABELS:
            raise UnsafeSparqlInput(
                f"invalid node label pair: {src_label!r}/{dst_label!r}"
            )
        validate_pid(pid)
        tx.run(
            f"UNWIND $rows AS row "
            f"MATCH (s:{src_label} {{id: row.src}}) "
            f"MERGE (t:{dst_label} {{id: row.dst}}) "
            f"CREATE (s)-[:{pid}]->(t)",
            rows=rows,
        )

    if payload["statements"]:
        tx.run(
            "UNWIND $stmts AS stmt "
            "MATCH (e:Entity {id: stmt.owner}) "
            "CREATE (e)-[:STATEMENT {pid: stmt.pid}]->"
            "(st:Statement {id: stmt.id, pid: stmt.pid}) "
            "SET st += stmt.props",
            stmts=payload["statements"],
        )

    for kind in ("VALUE", "QUALIFIER"):
        rows = [edge for edge in payload["statement_edges"] if edge["kind"] == kind]
        if rows:
            tx.run(
                f"UNWIND $rows AS row "
                f"MATCH (st:Statement {{id: row.stmt}}) "
                f"MERGE (t:Entity {{id: row.dst}}) "
                f"CREATE (st)-[:{kind} {{pid: row.pid}}]->(t)",
                rows=rows,
            )


def iri_to_qid(iri):
    return iri.removeprefix(WIKIDATA_ENTITY_IRI_BASE)


def fetch_seed_data(qid, *, session=None, timeout=60, include_p131=False):
    """Run the WDQS closure CONSTRUCT for ``qid`` and parse the response.

    One HTTP request to WDQS, then three passes over the Turtle: grouping
    by entity for the graph load, label extraction for ancestor rows,
    metadata extraction for the seed's ``WikidataItem`` fields.
    ``include_p131`` selects the region-seed query variant (see
    ``closure_query``).

    Returns a dict with keys ``turtle``, ``groups``, ``labels``,
    ``metadata``. Raises ``ClosureLoadError`` if the response is empty,
    unparseable, or missing the seed itself; raises
    ``requests.RequestException`` if the HTTP call fails.
    """
    owns_session = session is None
    if owns_session:
        # Local import: avoids a hard dependency on Django app readiness
        # if this module gets imported during settings load.
        from .tasks import create_request_session

        session = create_request_session()
    try:
        turtle = fetch_closure_turtle(
            session, qid, timeout=timeout, include_p131=include_p131
        )
    finally:
        if owns_session:
            session.close()

    groups, labels = parse_closure(turtle)
    if not groups:
        raise ClosureLoadError(f"WDQS returned no entity triples for {qid}")
    seed_iri = f"{WIKIDATA_ENTITY_IRI_BASE}{qid}"
    if seed_iri not in groups:
        raise ClosureLoadError(f"WDQS response did not include the seed entity {qid}")

    metadata = extract_seed_metadata(turtle, qid)
    if not metadata["title"]:
        raise ClosureLoadError(
            f"WDQS returned no English or default label for the seed entity {qid}"
        )
    return {
        "turtle": turtle,
        "groups": groups,
        "labels": labels,
        "metadata": metadata,
    }


def commit_closure_to_memgraph(
    seed_qid, groups, labels, *, discovered_via=None, client=None
):
    """Push parsed closure to Memgraph and reconcile ``WikidataItem`` rows.

    - Atomically replaces each closure entity's mirrored data in one Bolt
      transaction — all entities swap together or none do.
    - Bumps ``sparql_last_loaded_at`` on already-existing ``WikidataItem``
      rows in the closure (the seed itself is skipped here; its caller -
      typically ``WikidataItem.save()`` - sets that field in-place before
      saving).
    - ``bulk_create``s ``WikidataItem`` rows for newly-encountered
      ancestors. ``bulk_create`` bypasses ``save()`` so we don't fan out
      one closure-fetch per ancestor.

    Returns the number of new ancestor rows created.
    """
    from django.utils import timezone

    from .memgraph import MemgraphClient, ensure_schema
    from .models import WikidataItem

    payload = build_graph_payload(groups)

    owns_client = client is None
    if owns_client:
        client = MemgraphClient()
    try:
        ensure_schema(client)
        client.write_tx(lambda tx: apply_closure(tx, payload))
    finally:
        if owns_client:
            client.close()

    now = timezone.now()
    # Property-descriptor nodes (:Property) live only in Memgraph;
    # WikidataItem rows track Q-entities alone.
    qids = [q for q in (iri_to_qid(iri) for iri in groups) if looks_like_qid(q)]
    existing_qids = set(
        WikidataItem.objects.filter(wikidata_id__in=qids).values_list(
            "wikidata_id", flat=True
        )
    )

    # Skip the seed: its caller (WikidataItem.save) is mid-save and will
    # write sparql_last_loaded_at in the same INSERT/UPDATE.
    refresh_qids = [q for q in existing_qids if q != seed_qid]
    if refresh_qids:
        refresh_items = list(WikidataItem.objects.filter(wikidata_id__in=refresh_qids))
        for item in refresh_items:
            if item.wikidata_id in labels:
                item.title = labels[item.wikidata_id]
            item.sparql_last_loaded_at = now
            item.sparql_fetch_failures = 0
        WikidataItem.objects.bulk_update(
            refresh_items,
            ["title", "sparql_last_loaded_at", "sparql_fetch_failures"],
        )

    new_qids = [
        q for q in qids if q not in existing_qids and q != seed_qid and q in labels
    ]
    new_items = [
        WikidataItem(
            wikidata_id=q,
            title=labels[q],
            sparql_last_loaded_at=now,
            sparql_fetch_failures=0,
            discovered_via=discovered_via,
        )
        for q in new_qids
    ]
    if new_items:
        WikidataItem.objects.bulk_create(new_items)

    return len(new_items)
