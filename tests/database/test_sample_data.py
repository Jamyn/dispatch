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


def test_sample_data_retains_the_rows_e2e_depends_on(sample_data_engine):
    """The e2e specs select these by name; regenerating must not drop them."""
    with sample_data_engine.connect() as connection:
        connection.exec_driver_sql(f'SET search_path TO "{TENANT_SCHEMA}"')
        types = {r[0] for r in connection.exec_driver_sql("SELECT name FROM incident_type")}
        tags = {r[0] for r in connection.exec_driver_sql("SELECT name FROM tag")}

    assert "Denial of Service" in types
    assert "ExampleTag" in tags
