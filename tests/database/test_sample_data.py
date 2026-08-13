"""Guards data/dispatch-sample-data.dump against schema drift.

The dump carries its own alembic stamp, so `database upgrade` believes every
revision up to that stamp already ran and skips it. A dump whose tables are
older than its stamp therefore restores and upgrades reporting "Success." while
leaving a schema the ORM cannot query, and regenerating it (restore, upgrade,
dump) round-trips the damage instead of repairing it.
"""

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import MetaData, create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy_utils import create_database, database_exists, drop_database

import dispatch.main  # noqa: F401  registers every model, so the snapshot below is complete
from dispatch import config
from dispatch.database.core import Base
from dispatch.database.manage import get_core_tables, get_tenant_tables

DUMP = Path(__file__).parents[2] / "data" / "dispatch-sample-data.dump"
SAMPLE_DB = "dispatch-sample-data-test"
TENANT_SCHEMA = "dispatch_organization_default"
CORE_SCHEMA = "dispatch_core"


def _sample_uri() -> str:
    return str(config.SQLALCHEMY_DATABASE_URI).rsplit("/", 1)[0] + f"/{SAMPLE_DB}"


def _load_dump(engine, dump: Path) -> None:
    """Applies a plain-format pg_dump without shelling out to psql.

    Only COPY FROM stdin blocks are split out, because they need the copy
    protocol; everything between them is handed to the server verbatim. Parsing
    statements here instead would have to cope with the $$-quoted function
    bodies the dump installs for full-text search.
    """

    def flush(cursor, buffer: list[str]) -> list[str]:
        # Consecutive COPY blocks are separated by nothing but comments and
        # blank lines, and psycopg2 rejects a query with no statement in it.
        body = "".join(buffer)
        if any(line.strip() and not line.startswith("--") for line in buffer):
            cursor.execute(body)
        return []

    connection = engine.raw_connection()
    try:
        cursor = connection.cursor()
        buffer: list[str] = []
        lines = iter(dump.read_text().splitlines(keepends=True))
        for line in lines:
            # psql meta-commands, not SQL. pg_dump 18 brackets its output in
            # \restrict/\unrestrict; update-example-data.sh strips them, but a
            # hand-rolled pg_dump would leave them and the failure is cryptic.
            if line.startswith("\\restrict ") or line.startswith("\\unrestrict "):
                continue
            if line.startswith("COPY ") and line.rstrip().endswith("FROM stdin;"):
                buffer = flush(cursor, buffer)
                rows = []
                for row in lines:
                    if row.rstrip("\n") == "\\.":
                        break
                    rows.append(row)
                cursor.copy_expert(line, io.StringIO("".join(rows)))
            else:
                buffer.append(line)
        flush(cursor, buffer)
        connection.commit()
    finally:
        connection.close()


@pytest.fixture(scope="module")
def sample_data_engine():
    """Restores the committed dump into its own database and upgrades it.

    Deliberately not the `db` fixture's database: this one is built from the
    fixture file rather than from the models, which is the whole point.
    """
    uri = _sample_uri()
    if database_exists(uri):
        drop_database(uri)
    create_database(uri)

    loader = create_engine(uri)
    try:
        _load_dump(loader, DUMP)
    finally:
        loader.dispose()

    # A subprocess so DATABASE_NAME can be redirected without mutating this
    # process's already-imported config, which the rest of the suite shares.
    # `database upgrade` prompts for hostname and database name; piping them
    # back answers both.
    subprocess.run(
        [sys.executable, "-m", "dispatch.cli", "database", "upgrade"],
        check=True,
        capture_output=True,
        input=f"{config.DATABASE_HOSTNAME}\n{SAMPLE_DB}\n",
        text=True,
        env={**os.environ, "DATABASE_NAME": SAMPLE_DB},
    )

    engine = create_engine(uri)
    yield engine
    engine.dispose()
    drop_database(uri)


def _build_model_metadata() -> MetaData:
    """The models, rebound to the schemas a real deployment puts them in.

    Rebinding through to_metadata rather than assigning Table.schema: mutating
    it in place leaves MetaData.tables keyed on the old names, and
    compare_metadata then raises KeyError.
    """
    target = MetaData()
    for table in get_core_tables():
        table.to_metadata(target, schema=CORE_SCHEMA)
    for table in get_tenant_tables():
        table.to_metadata(target, schema=TENANT_SCHEMA)
    return target


# Snapshotted at import, which happens during collection and so before any
# fixture runs. The db fixture calls init_schema, which assigns Table.schema in
# place on this shared metadata; after that get_tenant_tables() -- tables with
# no schema -- reports nothing, and the unqualified foreign key targets no
# longer resolve. Both failures look like drift in the fixture rather than in
# this comparison.
_MODEL_METADATA = _build_model_metadata()


def test_sample_data_schema_matches_models(sample_data_engine):
    """A restored and upgraded dump must leave no schema drift behind."""
    with sample_data_engine.connect() as connection:
        context = MigrationContext.configure(
            connection,
            opts={
                "include_schemas": True,
                "include_name": lambda name, type_, parent: (
                    name in (CORE_SCHEMA, TENANT_SCHEMA) if type_ == "schema" else True
                ),
            },
        )
        diffs = compare_metadata(context, _MODEL_METADATA)

    # alembic's own bookkeeping table is not a model, so it is always reported.
    drift = [d for d in diffs if "'alembic_version'" not in repr(d)]
    assert not drift, "sample data schema differs from the models:\n" + "\n".join(
        repr(d) for d in drift
    )


def test_sample_data_is_queryable(sample_data_engine):
    """Every mapped model must select against the restored database.

    compare_metadata catches more than this does, but this is the failure an
    operator actually sees: a 500 on the first request touching the table.
    """
    engine = sample_data_engine.execution_options(
        schema_translate_map={None: TENANT_SCHEMA, CORE_SCHEMA: CORE_SCHEMA}
    )
    failures = []
    with Session(engine) as session:
        for mapper in sorted(Base.registry.mappers, key=lambda m: m.class_.__name__):
            try:
                session.execute(select(mapper.class_).limit(1)).unique().all()
            except Exception as exc:
                failures.append(f"{mapper.class_.__name__}: {str(exc).splitlines()[0]}")
                session.rollback()

    assert not failures, "models that cannot query the sample data:\n" + "\n".join(failures)


# Sequences reachable from the column they feed, so a sequence that is merely
# OWNED BY a client-assigned column is not compared. deptype 'a' is a serial
# default, 'i' an identity column; only the former appears here today.
_OWNED_SEQUENCES = """
SELECT seq_ns.nspname AS sequence_schema,
       seq.relname    AS sequence_name,
       tab_ns.nspname AS table_schema,
       tab.relname    AS table_name,
       att.attname    AS column_name,
       pg_sequence_last_value(seq.oid) AS last_value,
       s.seqstart     AS start_value,
       s.seqincrement AS increment
FROM pg_class seq
JOIN pg_namespace seq_ns ON seq_ns.oid = seq.relnamespace
JOIN pg_sequence s ON s.seqrelid = seq.oid
JOIN pg_depend dep ON dep.classid = 'pg_class'::regclass
                 AND dep.objid = seq.oid
                 AND dep.refclassid = 'pg_class'::regclass
                 AND dep.deptype IN ('a', 'i')
JOIN pg_class tab ON tab.oid = dep.refobjid
JOIN pg_namespace tab_ns ON tab_ns.oid = tab.relnamespace
JOIN pg_attribute att ON att.attrelid = tab.oid AND att.attnum = dep.refobjsubid
WHERE seq.relkind = 'S'
  AND tab.relkind IN ('r', 'p')
  AND att.atttypid IN ('smallint'::regtype, 'integer'::regtype, 'bigint'::regtype)
  AND s.seqincrement > 0
  AND starts_with(tab_ns.nspname, 'dispatch')
ORDER BY seq_ns.nspname, seq.relname
"""


def test_sample_data_sequences_lead_their_tables(sample_data_engine):
    """No sequence may hand out an id that already exists in its table.

    A sequence left behind its table's data is invisible until the first insert,
    which then fails on the primary key -- and because each collision still
    consumes a value, it recovers by itself after as many attempts as the gap is
    wide. That makes it read as flakiness rather than as a broken fixture, so
    assert on the catalog rather than by inserting a row.
    """
    preparer = sample_data_engine.dialect.identifier_preparer
    collisions = []

    with sample_data_engine.connect() as connection:
        for seq in connection.exec_driver_sql(_OWNED_SEQUENCES).mappings():
            table = f"{preparer.quote(seq['table_schema'])}.{preparer.quote(seq['table_name'])}"
            highest = connection.exec_driver_sql(
                f"SELECT max({preparer.quote(seq['column_name'])}) FROM {table}"
            ).scalar()
            if highest is None:
                continue  # an empty table constrains nothing

            # pg_sequence_last_value is NULL until the sequence is first read,
            # which is the is_called=false case: nothing has been handed out yet.
            last = seq["last_value"]
            if last is None:
                last = seq["start_value"] - seq["increment"]
            following = last + seq["increment"]
            if following <= highest:
                collisions.append(
                    f"{seq['sequence_schema']}.{seq['sequence_name']}: next value "
                    f"{following} collides with {table}.{seq['column_name']} "
                    f"(max {highest})"
                )

    assert not collisions, "sequences behind their table's data:\n" + "\n".join(collisions)


# Triggers are per-table objects, so a table with no row in pg_trigger has no
# trigger at all. tgisinternal is load-bearing: without it every table carrying
# a foreign key looks like it has one.
_SEARCH_VECTOR_TRIGGERS = """
SELECT n.nspname AS schema_name,
       c.relname AS table_name,
       count(t.oid) FILTER (WHERE NOT t.tgisinternal) AS triggers
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid
                   AND a.attname = 'search_vector'
                   AND a.attnum > 0
                   AND NOT a.attisdropped
LEFT JOIN pg_trigger t ON t.tgrelid = c.oid
WHERE c.relkind = 'r'
  AND starts_with(n.nspname, 'dispatch')
GROUP BY 1, 2
"""


def _searchable_tables() -> set[tuple[str, str]]:
    """(schema, table) for every model column the trigger machinery covers."""
    expected = set()
    for schema, tables in ((CORE_SCHEMA, get_core_tables()), (TENANT_SCHEMA, get_tenant_tables())):
        for table in tables:
            for column in table.columns:
                # setup_fulltext_search only installs a trigger when the
                # TSVectorType names the columns to index.
                if column.name.endswith("search_vector") and hasattr(column.type, "columns"):
                    expected.add((schema, table.name))
    return expected


def test_upgraded_sample_data_has_every_search_vector_trigger(sample_data_engine):
    """`database upgrade` must leave no search_vector column unpopulated.

    Trigger installation used to happen only in init_schema, so a table added by
    a migration got its column and its GIN index but nothing to fill them. The
    result is invisible: `database.service.search` ORs the vector match with a
    `name ILIKE` fallback, so search keeps returning plausible rows.
    """
    with sample_data_engine.connect() as connection:
        found = {
            (row.schema_name, row.table_name): row.triggers
            for row in connection.exec_driver_sql(_SEARCH_VECTOR_TRIGGERS)
        }

    untriggered = sorted(
        f"{schema}.{table}"
        for schema, table in _searchable_tables()
        if found.get((schema, table)) == 0
    )
    assert not untriggered, "search_vector columns with no trigger to populate them:\n" + "\n".join(
        untriggered
    )


def test_upgraded_sample_data_search_matches_on_description(sample_data_engine):
    """The repaired triggers must actually make full-text search work.

    Searches a token carried only by `description`. Every case_priority is named
    Low/Medium/High/Critical, so `name ILIKE '%priority%'` cannot satisfy this
    query and only a populated search_vector can -- a search by name would pass
    with the triggers entirely absent.
    """
    from dispatch.case.priority.models import CasePriority
    from dispatch.database.service import search

    engine = sample_data_engine.execution_options(
        schema_translate_map={None: TENANT_SCHEMA, CORE_SCHEMA: CORE_SCHEMA}
    )
    with Session(engine) as session:
        backfilled = search(
            query_str="priority",
            query=session.query(CasePriority),
            model="CasePriority",
            sort=False,
        ).all()
        assert {row.name for row in backfilled} == {"Low", "Medium", "High", "Critical"}, (
            "existing rows were not backfilled by the repaired trigger"
        )

        # Insert and update go through the same BEFORE INSERT OR UPDATE trigger,
        # but only the insert path is exercised above. Rolled back so the
        # module-scoped fixture stays as the other tests found it.
        session.add(
            CasePriority(
                name="Zzz Fixture Priority",
                description="quarklike escalation",
                project_id=backfilled[0].project_id,
            )
        )
        session.flush()
        inserted = search(
            query_str="quarklike",
            query=session.query(CasePriority),
            model="CasePriority",
            sort=False,
        ).all()
        assert [row.name for row in inserted] == ["Zzz Fixture Priority"], (
            "an inserted row's search_vector was not populated"
        )

        inserted[0].description = "gluonic escalation"
        session.flush()
        updated = search(
            query_str="gluonic",
            query=session.query(CasePriority),
            model="CasePriority",
            sort=False,
        ).all()
        assert [row.name for row in updated] == ["Zzz Fixture Priority"], (
            "an updated row's search_vector was not recomputed"
        )
        session.rollback()


def test_sample_data_retains_the_rows_e2e_depends_on(sample_data_engine):
    """The e2e specs select these by name; regenerating must not drop them."""
    with sample_data_engine.connect() as connection:
        connection.exec_driver_sql(f'SET search_path TO "{TENANT_SCHEMA}"')
        types = {r[0] for r in connection.exec_driver_sql("SELECT name FROM incident_type")}
        tags = {r[0] for r in connection.exec_driver_sql("SELECT name FROM tag")}

    assert "Denial of Service" in types
    assert "ExampleTag" in tags
