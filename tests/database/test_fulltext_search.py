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

import re
from collections import Counter

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.operators import desc_op

from dispatch.database.core import get_class_by_tablename
from dispatch.database.service import search
from dispatch.enums import SearchTypes
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


def test_init_database_commits_the_core_search_triggers(session, real_engine):
    """The core schema half of the same connect()/begin() defect.

    init_schema was fixed to commit its triggers; init_database was not, so a
    fresh install rolled back all four dispatch_core triggers on the way out.
    The tenant tests above cannot see it -- their triggers come from init_schema
    -- and the sample-data suite restores a dump rather than running init.
    """
    # real_engine, not database.core.engine: the suite runs each test inside an
    # uncommitted transaction, and this asserts the triggers survived a commit.
    with real_engine.connect() as connection:
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


# `/search` accepts `list[SearchTypes]`, so the enum -- not the frontend's own
# list -- is the authoritative set of models that can be unioned; anything the
# UI sends is a subset of it. Sizing these cases from the enum rather than from
# a literal count means a new search type widens the coverage on its own.
def _search_type_models():
    """Every model the `/search` endpoint will accept, in declaration order."""
    from dispatch.database.core import get_class_by_tablename

    return [get_class_by_tablename(t.value) for t in SearchTypes]


def _compiled_sql(query, literal_binds=False):
    """Compile for PostgreSQL. Reaches the failure without a server round trip."""
    return str(
        query.statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True} if literal_binds else {},
        )
    )


@pytest.mark.parametrize("sort", [True, False])
@pytest.mark.parametrize("arm_count", range(1, len(SearchTypes) + 1))
def test_composite_search_compiles_for_any_number_of_models(session, arm_count, sort):
    """Every arm count the endpoint can be asked for must compile.

    Folding the arms with a chain of `qs.union(q)` nests each union inside the
    next. From the third arm on, the nested arm's columns are renamed while the
    newly added arm's are not, so the two stop corresponding and the combined
    columns lose the `rank` name -- the `ORDER BY` could not resolve and
    compilation raised before any SQL was sent. Three arms is the regression
    threshold; the old tests stopped at two.
    """
    from dispatch.search.fulltext.composite_search import CompositeSearch

    models = _search_type_models()[:arm_count]
    query = CompositeSearch(session, models).build_query(STEMMED, sort=sort)

    sql = _compiled_sql(query)

    assert sql.count("UNION") == arm_count - 1
    # "UNION ALL" contains "UNION", so the count above cannot see the
    # difference. Duplicate elimination is load-bearing -- see the repeated
    # model test below.
    assert "UNION ALL" not in sql
    assert ("ORDER BY" in sql) is sort


def test_union_query_combines_every_arm_at_one_level(session):
    """The arms are combined once, not folded pairwise.

    Asserts on the expression tree rather than on the rendered SQL: a nested
    Core `union(union(a, b), c)` renders with the same number of SELECT
    keywords as a flat one, so counting them cannot tell the two apart. Nesting
    is what renames the inner arm's columns and destroys the `rank`
    correspondence, and it also costs one duplicate-elimination pass per level
    instead of one overall.
    """
    from sqlalchemy.sql.selectable import CompoundSelect

    from dispatch.search.fulltext.composite_search import CompositeSearch

    models = _search_type_models()
    combined = CompositeSearch(session, models).union_query(STEMMED)

    assert isinstance(combined, CompoundSelect)
    assert len(combined.selects) == len(models)
    assert not any(isinstance(arm, CompoundSelect) for arm in combined.selects), (
        "the arms are nested inside one another rather than combined at one level"
    )


def test_the_union_carries_no_tsvector(session):
    """No arm may project the vector, and neither may the outer select.

    `tsvector` has no hash opclass, so a tsvector anywhere in the union's
    select list forces duplicate elimination to sort every matching row --
    including the full vector of each -- instead of hashing (#159). The
    projection is pinned by name and position, since the arms correspond
    positionally; the type sweep then catches a vector reintroduced under any
    other label.
    """
    from sqlalchemy_utils import TSVectorType

    from dispatch.search.fulltext.composite_search import CompositeSearch

    models = _search_type_models()
    composite = CompositeSearch(session, models)
    combined = composite.union_query(STEMMED)
    outer = composite.build_query(STEMMED, sort=True).statement

    for arm in combined.selects:
        assert [c.name for c in arm.selected_columns] == ["id", "type", "rank"], (
            f"an arm projects more than the union's consumers read: "
            f"{[c.name for c in arm.selected_columns]}"
        )
    projected = [
        column
        for select in (*combined.selects, outer)
        for column in select.selected_columns
        if isinstance(column.type, TSVectorType)
    ]
    assert not projected, f"the union projects a tsvector: {projected}"


def test_composite_search_orders_on_a_column_of_the_outer_query(session):
    """`ORDER BY` must name a column the outer scope actually exports.

    The per-arm label was addressed by name from outside the union, which is
    what stopped resolving. Comparing the ordering term against the outer
    query's own columns pins the fix to an explicitly declared column without
    depending on how SQLAlchemy renders aliases -- so an added outer filter or a
    tiebreaker would not spuriously fail this.
    """
    from dispatch.search.fulltext.composite_search import CompositeSearch

    models = _search_type_models()[:3]
    statement = CompositeSearch(session, models).build_query(STEMMED, sort=True).statement

    ordering, *tiebreakers = statement._order_by_clauses
    exported = {column for source in statement.get_final_froms() for column in source.c}

    assert ordering.element in exported, (
        "ORDER BY references something other than a column of the outer query"
    )
    assert ordering.element.name == "rank"
    assert ordering.modifier is desc_op
    assert all(t in exported for t in tiebreakers), (
        "a tiebreaker references something other than a column of the outer query"
    )


def test_composite_search_parses_every_arm_under_its_own_regconfig(session):
    """Each arm keeps its own regconfig, in the predicate *and* in the rank.

    This is what `e53b90a1` exists for and what the fix must not undo: the
    search models split across two configurations, so one tsquery hoisted out of
    the arms would be wrong for one half. The expected counts are derived from
    the models themselves, so a model that changes its regconfig moves both
    sides of the assertion together.
    """
    from dispatch.search.fulltext import inspect_search_vectors, search_manager
    from dispatch.search.fulltext.composite_search import CompositeSearch

    models = _search_type_models()
    sql = _compiled_sql(
        CompositeSearch(session, models).build_query(STEMMED, sort=True), literal_binds=True
    )

    expected = Counter()
    for model in models:
        regconfig = search_manager.option(inspect_search_vectors(model)[0], "regconfig")
        # once in the `@@` predicate, once inside ts_rank_cd
        expected[regconfig] += 2

    assert Counter(re.findall(r"tsq_parse\('([^']+)'", sql)) == expected
    assert len(expected) > 1, "the fixture no longer spans more than one regconfig"


@pytest.fixture
def three_regconfig_hits(session):
    """One hit per model across three arms, spanning both regconfigs.

    Document and Tag index under `simple`, Incident under `english`, so this is
    the smallest set that is both past the regression threshold and
    heterogeneous. STEMMED is the discriminator: `simple` stores it verbatim as
    'policy' and `english` stems it to 'polici', and neither prefix-matches the
    other -- so hoisting a single tsquery out of the arms drops one half of
    these hits whichever configuration it picks. A token no stemmer alters
    cannot detect that.

    STEMMED goes in a column each model's vector actually covers: Document
    indexes `name` only, Tag and Incident cover `description`. Incident also
    carries it in `title` (weight B) and twice over, so the ranks genuinely
    differ rather than collapsing to a single value.
    """
    from tests.factories import DocumentFactory, IncidentFactory, TagFactory

    hits = {
        "Document": DocumentFactory(name=f"doc-{STEMMED}"),
        "Tag": TagFactory(name="tag-zeta", description=f"owned by the {STEMMED}"),
        "Incident": IncidentFactory(
            name="incident-zeta",
            title=f"{STEMMED} breach",
            description=f"{STEMMED} {STEMMED} repeatedly violated",
        ),
    }
    session.flush()
    return hits


def test_composite_search_returns_results_across_three_models(session, three_regconfig_hits):
    """The production path itself, past the arm count that used to fail.

    `composite_search` passes `sort=True` unconditionally, so every real search
    spanning three or more types raised and the endpoint returned HTTP 500.
    """
    from dispatch.database.core import get_class_by_tablename
    from dispatch.database.service import composite_search

    models = [get_class_by_tablename(t) for t in ("Document", "Tag", "Incident")]

    results = composite_search(
        db_session=session, query_str=STEMMED, models=models, current_user=None
    )

    for type_name, hit in three_regconfig_hits.items():
        assert hit.id in {x.id for x in results[type_name]}, (
            f"the {type_name} arm returned nothing from a three-model union"
        )


def test_composite_search_ranks_the_combined_result(session, three_regconfig_hits):
    """Ordering applies to the union as a whole, not to one arm.

    `composite_search` buckets its return value by type, which discards the
    combined order, so this asserts on the query rows directly. Ranks must also
    actually differ: under a mismatched regconfig `ts_rank_cd` scores every row
    0, and a constant list satisfies "sorted descending" vacuously.
    """
    from dispatch.database.core import get_class_by_tablename
    from dispatch.search.fulltext.composite_search import CompositeSearch

    models = [get_class_by_tablename(t) for t in ("Document", "Tag", "Incident")]

    rows = list(CompositeSearch(session, models).build_query(STEMMED, sort=True))

    assert {r.type for r in rows} == {"Document", "Tag", "Incident"}, "the union dropped an arm"
    ranks = [r.rank for r in rows]
    assert len(set(ranks)) > 1, f"every rank collapsed to the same value: {ranks}"
    assert ranks == sorted(ranks, reverse=True), f"combined result is not ranked: {ranks}"


def test_composite_search_deduplicates_a_repeated_model(session, three_regconfig_hits):
    """A repeated search type must not double its rows.

    `type[]` is a repeatable query parameter and nothing de-duplicates it, so
    `?type[]=Document&type[]=Document` really does build two identical arms.
    That is the one case where the union's duplicate elimination is
    load-bearing; under `UNION ALL` each document would come back twice.
    """
    from dispatch.database.core import get_class_by_tablename
    from dispatch.search.fulltext.composite_search import CompositeSearch

    document = get_class_by_tablename("Document")

    rows = list(CompositeSearch(session, [document, document]).build_query(STEMMED, sort=True))

    ids = [r.id for r in rows]
    assert three_regconfig_hits["Document"].id in ids
    assert len(ids) == len(set(ids)), f"a repeated search type returned duplicates: {ids}"


def test_composite_search_executes_at_full_arm_width(session, three_regconfig_hits):
    """Postgres must accept the widest union the endpoint can be asked for.

    The compile cases above stop at SQLAlchemy. This is the only test that sends
    every supported arm to the server, so it is what would catch a server-side
    objection to the shape -- mismatched column types across arms, or the
    untyped `type` literals failing to resolve -- at full width.
    """
    from dispatch.search.fulltext.composite_search import CompositeSearch

    models = _search_type_models()

    rows = list(CompositeSearch(session, models).build_query(STEMMED, sort=True))

    found = {r.type for r in rows}
    assert {"Document", "Tag", "Incident"} <= found, (
        f"a {len(models)}-arm union lost hits that a three-arm union returns: {found}"
    )
    ranks = [r.rank for r in rows]
    assert ranks == sorted(ranks, reverse=True), f"combined result is not ranked: {ranks}"


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


# --- ordering is total, not just ranked (#160) -------------------------------


@pytest.fixture
def equally_ranked_hits(session):
    """Several rows that all score exactly the same rank.

    `ts_rank_cd` returns a float4, and a single occurrence of one token in one
    indexed column scores the same value every time -- which is the ordinary
    shape of a search hit, not a contrived one. Documents index `name` only, so
    every one of these matches once, in one column, at one weight.
    """
    from tests.factories import DocumentFactory

    hits = [DocumentFactory(name=f"{STEMMED}-{i:02d}") for i in range(6)]
    session.flush()
    return hits


def rows_for(session, models):
    from dispatch.search.fulltext.composite_search import CompositeSearch

    return list(CompositeSearch(session, models).build_query(STEMMED, sort=True))


def test_equally_ranked_rows_really_do_tie(session, equally_ranked_hits):
    """Precondition. Without ties there is nothing for a tiebreaker to break."""
    from dispatch.database.core import get_class_by_tablename

    ranks = {r.rank for r in rows_for(session, [get_class_by_tablename("Document")])}

    assert len(ranks) == 1, f"expected one rank shared by every row, got {ranks}"


def test_the_ordering_is_total(session):
    """Every column of the sort key, asserted on the statement itself.

    Not left to a behavioural check alone: with no tiebreaker Postgres is free
    to return tied rows in any order, and for a small set it usually returns
    them in physical order -- which is the right answer by luck. A test that
    only compared the rows would pass while the bug was still there.
    """
    from dispatch.search.fulltext.composite_search import CompositeSearch

    models = _search_type_models()[:3]
    statement = CompositeSearch(session, models).build_query(STEMMED, sort=True).statement

    rank, *tiebreakers = statement._order_by_clauses

    assert rank.element.name == "rank"
    assert rank.modifier is desc_op
    # `id` alone is not unique across a union of different models.
    assert [t.name for t in tiebreakers] == ["type", "id"]


def test_the_order_is_the_same_across_models_at_equal_rank(session, three_regconfig_hits):
    """The tiebreaker has to be total across the whole union, not per arm."""
    from dispatch.database.core import get_class_by_tablename

    models = [get_class_by_tablename(t) for t in ("Document", "Tag", "Incident")]

    rows = rows_for(session, models)

    assert [(-r.rank, r.type, r.id) for r in rows] == sorted((-r.rank, r.type, r.id) for r in rows)


# --- the result set is bounded (#158) ----------------------------------------


def test_composite_search_is_bounded(session):
    """Unlimited, this returns every matching row in the tenant.

    The bound has to be on the combined query, not per arm, or the highest
    ranked rows of one type could be displaced by lower ranked rows of another.
    Asserted against the real limit rather than a small stubbed one so that
    raising `MAX_SEARCH_RESULTS` past what Postgres will do cheaply is a
    deliberate act.
    """
    from dispatch.database.service import MAX_SEARCH_RESULTS, composite_search
    from tests.factories import DocumentFactory

    for i in range(MAX_SEARCH_RESULTS + 10):
        DocumentFactory(name=f"{STEMMED}-bounded-{i:04d}")
    session.flush()

    results = composite_search(
        db_session=session,
        query_str=STEMMED,
        models=[get_class_by_tablename("Document")],
        current_user=None,
    )

    assert len(results["Document"]) == MAX_SEARCH_RESULTS


def test_the_bound_keeps_the_highest_ranked_rows(session, three_regconfig_hits):
    """A cap that dropped the top of the ranking would be worse than none."""
    from dispatch.database.service import MAX_SEARCH_RESULTS
    from dispatch.search.fulltext.composite_search import CompositeSearch

    models = [get_class_by_tablename(t) for t in ("Document", "Tag", "Incident")]

    unbounded = list(CompositeSearch(session, models).build_query(STEMMED, sort=True))
    bounded = list(
        CompositeSearch(session, models).build_query(STEMMED, sort=True).limit(MAX_SEARCH_RESULTS)
    )

    assert [(r.type, r.id) for r in bounded] == [
        (r.type, r.id) for r in unbounded[:MAX_SEARCH_RESULTS]
    ]
