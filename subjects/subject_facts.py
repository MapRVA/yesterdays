"""Read per-Subject Wikidata facts from the Memgraph mirror.

Counterpart to ``wikidata_closure.py``, which writes them. Both queries
anchor on the entity node's indexed ``id``, so lookups stay bounded no
matter how large the mirror grows.
"""

import logging
from datetime import datetime
from urllib.parse import urlsplit

from .memgraph import GRAPH_ERRORS, MemgraphClient
from .sparql_safety import UnsafeSparqlInput, validate_qid

logger = logging.getLogger(__name__)


# ``description_en`` holds the first English literal the closure load saw
# (the write bakes in the ``SAMPLE`` the old SPARQL read did); ``P571``
# (inception) and ``P576`` (dissolved, abolished or demolished) are list
# properties, so ``head()`` picks the first value of each.
_SUBJECT_FACTS_QUERY = """\
MATCH (e:Entity {id: $qid})
RETURN e.description_en AS description,
       head(e.P571) AS inception,
       head(e.P576) AS dissolved
"""


# The old cross-graph ``wikibase:directClaim`` join becomes node-id
# equality: literal-valued claims are stored as PID-keyed list
# properties, and only PID keys can match a ``:Property`` node's id
# (``label_en`` and friends filter out naturally). Q18618628 marks
# cultural-heritage authority-control properties.
_AUTHORITY_IDS_QUERY = """\
MATCH (e:Entity {id: $qid})
WITH properties(e) AS claims
UNWIND keys(claims) AS pid
MATCH (p:Property {id: pid})-[:P31]->(:Entity {id: 'Q18618628'})
OPTIONAL MATCH (p)-[:P1629]->(item:Entity)
UNWIND claims[pid] AS value
RETURN p.id AS prop, p.label_en AS propLabel, value,
       head(p.P1630) AS formatter, item.label_en AS itemLabel
ORDER BY propLabel, value
"""


_SAFE_URL_SCHEMES = frozenset({"http", "https"})


def _authority_url(formatter, value):
    """
    Build the external-identifier URL, or ``None`` if unsafe.

    ``formatter`` is a Wikidata ``P1630`` template with a ``$1`` placeholder
    for ``value``. A vandalized formatter could yield a non-``http(s)``
    scheme (e.g. ``javascript:``) that would be rendered straight into an
    ``href``; such URLs are dropped.
    """
    if not formatter:
        return None
    url = formatter.replace("$1", value)
    if urlsplit(url).scheme.lower() not in _SAFE_URL_SCHEMES:
        return None
    return url


def fetch_authority_ids(qid):
    """
    Return authority-control external identifiers for one Subject.

    Pairs each cultural-heritage authority-control property the subject
    uses (found via the property descriptors ``wikidata_closure``
    mirrors) with the subject's own identifier value(s), yielding
    display-ready external links. Returns a list of
    ``{property, value, url, item_label}`` dicts, ordered by property
    label; ``url`` is ``None`` when the property has no ``P1630`` formatter
    URL (or the formatter would produce a non-``http(s)`` link), and
    ``item_label`` is ``None`` unless the property has a P1629
    ("Wikidata item of this property") target with a mirrored English
    label. Returns ``[]`` on Q-ID validation failure or Memgraph error,
    so callers can render the page without the section.
    """
    try:
        validate_qid(qid)
    except UnsafeSparqlInput:
        return []

    try:
        with MemgraphClient() as client:
            rows = client.read(_AUTHORITY_IDS_QUERY, qid=qid)
    except GRAPH_ERRORS as e:
        logger.warning("fetch_authority_ids(%s) failed: %s", qid, e)
        return []

    results = []
    for row in rows:
        value = row.get("value")
        if not value:
            continue
        formatter = row.get("formatter")
        # Fall back to the bare P-ID if a property somehow lacks an English
        # label, so the row still renders with an identifier prefix.
        label = row.get("propLabel") or row.get("prop")
        results.append(
            {
                "property": label,
                "value": value,
                "url": _authority_url(formatter, value),
                "item_label": row.get("itemLabel") or None,
            }
        )
    return results


def _parse_wikidata_date(value):
    """
    Parse a Wikidata time literal like ``"1895-01-01T00:00:00Z"`` to a date.

    Returns ``None`` for missing, BCE (leading ``-``), or otherwise
    unparseable values, matching the leniency of ``extract_seed_metadata``
    in ``wikidata_closure.py``. Sub-day precision is not recoverable here —
    the mirror drops the value nodes carrying ``wikibase:timePrecision`` —
    so year-precision values arrive as ``YYYY-01-01`` and callers are
    expected to render only the year.
    """
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError, TypeError:
        return None


def fetch_subject_facts(qid):
    """
    Return ``{description, inception, dissolved}`` for one Subject.

    ``description`` is the English description literal (str, omitted if
    absent). ``inception`` and ``dissolved`` are ``datetime.date`` values
    parsed from the first ``P571`` / ``P576`` value respectively, each
    omitted if absent or unparseable. Returns ``{}`` on Q-ID validation
    failure or Memgraph error so callers can render the page without the
    Wikidata fields.
    """
    try:
        validate_qid(qid)
    except UnsafeSparqlInput:
        return {}

    try:
        with MemgraphClient() as client:
            rows = client.read(_SUBJECT_FACTS_QUERY, qid=qid)
    except GRAPH_ERRORS as e:
        logger.warning("fetch_subject_facts(%s) failed: %s", qid, e)
        return {}

    if not rows:
        return {}

    facts = {}
    row = rows[0]
    if row.get("description"):
        facts["description"] = row["description"]
    for key in ("inception", "dissolved"):
        parsed = _parse_wikidata_date(row.get(key))
        if parsed:
            facts[key] = parsed
    return facts
