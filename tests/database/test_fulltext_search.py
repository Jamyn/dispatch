"""Behavioural coverage for the Postgres full-text search triggers.

`init_schema` installs a per-table trigger that maintains `search_vector` on
write. It ran under `engine.connect()` -- which never commits -- so the
triggers were silently rolled away and every vector stayed NULL; search still
"worked" because `database.service.search` ORs the vector match with a
`name ILIKE` fallback, so nothing failed loudly.

These tests therefore search on a token that appears **only in a column the
ILIKE fallback does not cover**. A test that searched by name would pass with
the triggers entirely absent.
"""

import pytest
from sqlalchemy import select

from dispatch.database.service import search
from dispatch.incident.models import Incident

# Distinctive enough that no factory-generated fuzzy text can collide with it.
TOKEN = "zzarquux"


_UNTRIGGERED_CORE_TABLES = """
SELECT c.relname
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid
                   AND a.attname = 'search_vector'
                   AND a.attnum > 0
                   AND NOT a.attisdropped
LEFT JOIN pg_trigger t ON t.tgrelid = c.oid AND NOT t.tgisinternal
WHERE n.nspname = 'dispatch_core' AND c.relkind = 'r'
GROUP BY 1
HAVING count(t.oid) = 0
"""


def test_init_database_commits_the_core_search_triggers(session):
    """The core schema half of the same connect()/begin() defect.

    init_schema was fixed to commit its triggers; init_database was not, so a
    fresh install rolled back all four dispatch_core triggers on the way out.
    The tenant tests above cannot see it -- their triggers come from init_schema
    -- and the sample-data suite restores a dump rather than running init.
    """
    from dispatch.database.core import engine

    with engine.connect() as connection:
        missing = [row[0] for row in connection.exec_driver_sql(_UNTRIGGERED_CORE_TABLES)]

    assert not missing, f"init_database left these core triggers uncommitted: {missing}"


@pytest.fixture
def incident_described_as(session):
    """Creates an incident whose TOKEN appears only in `description`."""

    def _make(**kwargs):
        from tests.factories import IncidentFactory

        incident = IncidentFactory(**kwargs)
        session.flush()
        return incident

    return _make


def test_search_vector_is_populated_on_insert(session, incident_described_as):
    """The trigger must fill search_vector on write.

    Asserts the stored vector directly, so it fails if the trigger was never
    committed -- the exact state the `engine.connect()`/`engine.begin()` bug
    left the schema in.
    """
    incident = incident_described_as(description=f"an outage caused by {TOKEN}")

    vector = session.execute(
        select(Incident.search_vector).where(Incident.id == incident.id)
    ).scalar_one()

    assert vector, "search_vector is empty; the full-text trigger did not fire"
    assert TOKEN in str(vector)


def test_search_matches_a_description_only_token(session, incident_described_as):
    """Searching a description-only token must go through the vector.

    `search()` ORs the vector match with `name ILIKE` and `name ==`. TOKEN is
    kept out of `name` so neither fallback can satisfy this query -- only a
    populated, queryable search_vector can.
    """
    match = incident_described_as(
        name="incident-alpha", title="unrelated title", description=f"root cause was {TOKEN}"
    )
    other = incident_described_as(
        name="incident-beta", title="unrelated title", description="something else entirely"
    )

    results = search(query_str=TOKEN, query=session.query(Incident), model="incident").all()

    ids = {i.id for i in results}
    assert match.id in ids, "description-only token was not matched by the search vector"
    assert other.id not in ids


def test_search_ranking_orders_by_vector_relevance(session, incident_described_as):
    """`sort=True` ranks with ts_rank_cd, which needs a real tsvector.

    Guards the ordering path separately: ts_rank_cd over an empty vector
    returns 0 for every row, so ranking degrades silently to insertion order.
    """
    weak = incident_described_as(
        name="incident-weak", title="unrelated", description=f"mentions {TOKEN} once"
    )
    strong = incident_described_as(
        name="incident-strong",
        title=f"{TOKEN} in the title",
        description=f"{TOKEN} {TOKEN} repeated",
    )

    ranked = search(
        query_str=TOKEN, query=session.query(Incident), model="incident", sort=True
    ).all()

    ids = [i.id for i in ranked]
    assert strong.id in ids and weak.id in ids
    assert ids.index(strong.id) < ids.index(weak.id), (
        "higher-weighted match did not rank first; ts_rank_cd saw an empty vector"
    )
