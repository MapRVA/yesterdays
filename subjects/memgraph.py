"""Bolt client for the Memgraph store holding our Wikidata subject mirror.

Memgraph is a sidecar service holding the mirror as a property graph:
one ``:Entity`` node per Q-item, ``:Property`` nodes for property
descriptors, ``:Statement`` nodes for reified statements, dynamically
typed ``[:P{n}]`` relationships for entity-valued direct claims, and a
``:ProjectSubject`` label marking entities that are Subjects with images
in this project. ``wikidata_closure.build_graph_payload`` documents the
full data model.

The neo4j driver is not fork-safe, and every runtime here forks after
import (Celery prefork workers, gunicorn, runserver's autoreloader) — so
the module keeps one lazily created driver per process, keyed on PID.
"""

import logging
import os
import threading

import neo4j
import neo4j.exceptions
from django.conf import settings

logger = logging.getLogger(__name__)

# The graph-failure surface callers should catch — successor to the
# ``requests.RequestException`` the old HTTP client raised. Neo4jError
# covers server-side failures (including Cypher errors, which the old
# client likewise folded into RequestException via ``raise_for_status``);
# DriverError covers client-side transport failures (ServiceUnavailable,
# SessionExpired, ...). Genuine programming errors still propagate.
GRAPH_ERRORS = (neo4j.exceptions.Neo4jError, neo4j.exceptions.DriverError)

# Managed transactions retry transient failures (connection loss,
# constraint-conflict aborts between concurrent closure commits) for up
# to this long — successor to the old urllib3 ``Retry(3, backoff 0.5)``.
_RETRY_SECONDS = 10.0

_driver = None
_driver_pid = None
_driver_lock = threading.Lock()


def get_driver():
    """Return the process-wide Bolt driver, (re)creating it after a fork.

    A driver inherited across ``fork()`` holds sockets shared with the
    parent, so a mismatched PID means this copy must be abandoned (not
    closed — closing would disrupt the parent's connections) and replaced.
    """
    global _driver, _driver_pid
    pid = os.getpid()
    if _driver is None or _driver_pid != pid:
        with _driver_lock:
            if _driver is None or _driver_pid != pid:
                _driver = neo4j.GraphDatabase.driver(
                    settings.MEMGRAPH_URL,
                    auth=None,
                    connection_timeout=float(settings.MEMGRAPH_TIMEOUT),
                    max_transaction_retry_time=_RETRY_SECONDS,
                )
                _driver_pid = pid
    return _driver


class MemgraphClient:
    """Thin veneer over the shared driver, keeping the old client's shape.

    Construct fresh per task / request and either call ``close()`` or use
    as a context manager, exactly like the Oxigraph client it replaces.
    Sessions are opened per call; connection pooling lives in the shared
    driver, so a client threaded through many calls (e.g.
    ``rebuild_all_subject_ancestors``) reuses connections automatically.
    """

    def __init__(self, driver=None):
        self._injected_driver = driver

    @property
    def driver(self):
        return self._injected_driver or get_driver()

    def close(self):
        """No-op: the process-wide driver outlives individual clients."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def read(self, query, **params):
        """Run a read query, return a list of ``{column: value}`` dicts.

        Unlike the old SPARQL-JSON flattening, unbound values come back
        as ``None`` rather than missing keys — consumers use ``.get()``
        either way.
        """
        with self.driver.session() as session:
            return session.execute_read(lambda tx: tx.run(query, **params).data())

    def write(self, query, **params):
        """Run a single write statement in a managed (retrying) transaction."""
        with self.driver.session() as session:
            session.execute_write(lambda tx: tx.run(query, **params).consume())

    def write_tx(self, fn):
        """Run ``fn(tx)`` in one managed write transaction.

        ``fn`` may issue multiple ``tx.run(...)`` calls; they commit or
        roll back together. Managed transactions re-run ``fn`` on
        transient errors, so it must be idempotent — every write here is
        (whole-mirror replacements and MERGEs).
        """
        with self.driver.session() as session:
            return session.execute_write(fn)


# Indexes as (label, property); a ``None`` property means a label index.
# The :Entity(id)/:Property(id) uniqueness constraints are load-bearing,
# not hygiene: two concurrent closure commits can both MERGE a missing
# shared node under snapshot isolation and create duplicates. With the
# constraint one transaction aborts and the managed-transaction retry
# re-runs it against the now-existing node.
_INDEXES = (
    ("Entity", "id"),
    ("Property", "id"),
    ("Statement", "id"),
    ("ProjectSubject", None),
)
_UNIQUE_CONSTRAINTS = (
    ("Entity", "id"),
    ("Property", "id"),
)

_schema_pid = None


def _property_key(value):
    """Normalize a SHOW ... INFO property column for set comparison.

    Memgraph reports single properties as strings in older versions and
    as lists since composite-index support; a label-only index reports
    ``None``.
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return (value,)


def ensure_schema(client=None):
    """Create any missing indexes / constraints; runs once per process.

    Called lazily from the write entry points (closure commit, project
    rebuild) so a fresh Memgraph volume self-heals on the first write.
    Schema DDL cannot run inside the data transactions, hence separate
    autocommit statements here. Failures propagate to the caller's
    ``GRAPH_ERRORS`` handling and leave the once-per-process flag unset,
    so the next write retries.
    """
    global _schema_pid
    if _schema_pid == os.getpid():
        return
    driver = client.driver if client is not None else get_driver()
    with driver.session() as session:
        have_indexes = {
            (row.get("label"), _property_key(row.get("property")))
            for row in session.run("SHOW INDEX INFO").data()
        }
        for label, prop in _INDEXES:
            if (label, _property_key(prop)) not in have_indexes:
                target = f":{label}({prop})" if prop else f":{label}"
                session.run(f"CREATE INDEX ON {target}").consume()
                logger.info("Created Memgraph index on %s", target)

        have_constraints = {
            (row.get("label"), _property_key(row.get("properties")))
            for row in session.run("SHOW CONSTRAINT INFO").data()
        }
        for label, prop in _UNIQUE_CONSTRAINTS:
            if (label, (prop,)) not in have_constraints:
                session.run(
                    f"CREATE CONSTRAINT ON (n:{label}) ASSERT n.{prop} IS UNIQUE"
                ).consume()
                logger.info(
                    "Created Memgraph unique constraint on :%s(%s)", label, prop
                )

        for row in session.run("SHOW STORAGE INFO").data():
            if (
                row.get("storage info") == "storage_mode"
                and row.get("value") != "IN_MEMORY_TRANSACTIONAL"
            ):
                # Analytical mode drops transaction isolation, which the
                # atomic closure swap depends on.
                logger.warning(
                    "Memgraph storage mode is %s; the mirror expects "
                    "IN_MEMORY_TRANSACTIONAL for atomic closure swaps",
                    row.get("value"),
                )
    _schema_pid = os.getpid()
