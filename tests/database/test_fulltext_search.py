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


# `english` stems this to 'polici'; `simple` stores it verbatim as 'policy'.
# Neither prefix-matches the other, so it exposes a config mismatch that the
# `:*` prefix operator cannot paper over.
STEMMED = "policy"


def test_search_matches_a_stemmed_token_on_a_simple_configured_model(session):
    """A `simple`-indexed table must be queried with `simple`, not `english`.

    `Tag` declares `regconfig="pg_catalog.simple"`, so its vector stores
    'policy'. Parsing the query as `english` yields 'polici':*, which cannot
    match. STEMMED is kept out of `name` so neither `ILIKE` nor `==` can
    satisfy the query -- only a correctly-configured tsquery can.
    """
    from dispatch.tag.models import Tag
    from tests.factories import TagFactory

    match = TagFactory(name="tag-alpha", description=f"covers the {STEMMED} for on-call")
    other = TagFactory(name="tag-beta", description="something else entirely")
    session.flush()

    results = search(query_str=STEMMED, query=session.query(Tag), model="tag").all()

    ids = {t.id for t in results}
    assert match.id in ids, (
        "a 'simple'-indexed row did not match a stemming-sensitive token; "
        "the query was parsed under a different regconfig than the vector"
    )
    assert other.id not in ids


def test_search_ranks_a_simple_configured_model_under_its_own_regconfig(session):
    """`ts_rank_cd` must parse the query with the vector's regconfig too.

    Both rows carry STEMMED in `name`, so the `ILIKE` fallback returns them
    regardless -- this isolates ordering from matching. Under a mismatched
    regconfig `ts_rank_cd` scores every row 0 and the ordering silently
    degrades to insertion order, so the weaker row is inserted first.
    """
    from dispatch.tag.models import Tag
    from tests.factories import TagFactory

    weak = TagFactory(name=f"{STEMMED}-weak", description="no further mention")
    strong = TagFactory(
        name=f"{STEMMED}-strong",
        description=f"{STEMMED} {STEMMED} {STEMMED} repeated",
    )
    session.flush()

    ranked = search(query_str=STEMMED, query=session.query(Tag), model="tag", sort=True).all()

    ids = [t.id for t in ranked]
    assert strong.id in ids and weak.id in ids
    assert ids.index(strong.id) < ids.index(weak.id), (
        "ts_rank_cd scored a 'simple'-indexed row under the wrong regconfig; "
        "every rank collapsed to 0 and ordering fell back to insertion order"
    )


def test_composite_search_honours_each_models_regconfig(session):
    """The `/search` endpoint unions models with *different* regconfigs.

    `SearchTypes` mixes 9 `simple`-indexed models (tag, document, service,
    task, term, ...) with 7 `english` ones in a single UNION, then applies one
    tsquery to the combined vector column. A single regconfig cannot be right
    for both halves, so the predicate has to be pushed into each arm of the
    union with that arm's own configuration.
    """
    from dispatch.database.service import composite_search
    from dispatch.incident.models import Incident
    from dispatch.tag.models import Tag
    from tests.factories import IncidentFactory, TagFactory

    simple_hit = TagFactory(name="tag-gamma", description=f"the {STEMMED} owner is on-call")
    english_hit = IncidentFactory(
        name="incident-gamma", title="unrelated", description=f"breached the {STEMMED} again"
    )
    session.flush()

    results = composite_search(
        db_session=session,
        query_str=STEMMED,
        models=[Tag, Incident],
        current_user=None,
    )

    assert english_hit.id in {i.id for i in results["Incident"]}, (
        "the english-indexed arm of the union stopped matching"
    )
    assert simple_hit.id in {t.id for t in results["Tag"]}, (
        "the simple-indexed arm of the union was parsed as english; "
        "composite_search applies one global regconfig to heterogeneous vectors"
    )


def test_search_matches_a_stemmed_token_on_an_english_configured_model(session):
    """The `english` half must keep stemming -- the fix must not flatten it.

    `Incident` declares no regconfig, so it indexes under the `english`
    default and stores 'polici'. Mirrors the `simple` case above so a change
    that hardcodes either configuration is caught from both directions; the
    other tests here use a token no stemmer alters, which cannot detect this.
    """
    from dispatch.incident.models import Incident
    from tests.factories import IncidentFactory

    match = IncidentFactory(
        name="incident-delta", title="unrelated", description=f"violated the {STEMMED}"
    )
    other = IncidentFactory(
        name="incident-epsilon", title="unrelated", description="nothing relevant here"
    )
    session.flush()

    results = search(query_str=STEMMED, query=session.query(Incident), model="incident").all()

    ids = {i.id for i in results}
    assert match.id in ids, (
        "an 'english'-indexed row stopped matching its own stemmed token; "
        "the query side is no longer using the model's regconfig"
    )
    assert other.id not in ids
