"""Regression test for issue #95.

Four tenant migrations install full-text-search triggers by reflecting a
table with `sa.Table(name, metadata, autoload_with=conn)`, resolving the
schema to reflect from `conn.dialect.default_schema_name`. env.py creates one
engine and loops over every tenant schema on it, calling `SET search_path`
per iteration -- but the dialect resolves and caches `default_schema_name`
once, from the first connection made, before any `SET search_path` runs. It
is "public" for the rest of the process, for every schema visited. So each of
these migrations reflects `public.<table>` instead of the tenant's own table,
which does not exist there.

Only `61a861559de9` was reported (issue #95): it also called two SQLAlchemy
1.x APIs removed in 2.0 (`MetaData(bind=...)`, `autoload=True`), which raise
immediately and independently of the schema bug. The other three
(`7ddae3ba7822`, `7db13bf5c5d7`, `d1b5ed66d83d`) use 2.0-compatible syntax, so
they raised no error under a syntax check and were believed to work -- but
fail with NoSuchTableError the moment they run against a real tenant schema,
which nothing in CI does (the sample dump is stamped well past all four).
"""

import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.schema import CreateSchema
from sqlalchemy_utils import create_database, database_exists, drop_database

from dispatch import config
from dispatch.database.core import Base
from dispatch.database.manage import get_core_tables

# Worker-suffixed for the same reason as tests/conftest.py: this database is
# created and dropped by name, so it cannot be shared. --dist loadfile keeps
# this module on one worker today, but the name should not depend on that.
_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "")

DB_NAME = f"dispatch-test-tenant-migration-reflection{f'-{_WORKER}' if _WORKER else ''}"
SCHEMA_NAME = "dispatch_organization_default"


def _uri(name: str = DB_NAME) -> str:
    return str(config.SQLALCHEMY_DATABASE_URI).rsplit("/", 1)[0] + f"/{name}"


@pytest.fixture(scope="module")
def reflection_migration_engine():
    """A tenant schema built from the current models, alembic-tracked.

    Not the shared session-scoped `db` fixture: these tests rewind and replay
    individual migrations in place, which would corrupt the stamp and trigger
    state the rest of the suite relies on being at head.
    """
    uri = _uri()
    if database_exists(uri):
        drop_database(uri)
    create_database(uri)

    engine = create_engine(uri)
    with engine.begin() as conn:
        conn.execute(CreateSchema("dispatch_core", if_not_exists=True))
    Base.metadata.create_all(engine, tables=get_core_tables())

    with engine.begin() as conn:
        conn.execute(CreateSchema(SCHEMA_NAME, if_not_exists=True))
    # Not get_tenant_tables() (selects on schema is None): the autouse `db`
    # fixture elsewhere in the suite calls init_schema, which assigns
    # Table.schema on the shared Base.metadata in place, so by the time this
    # fixture runs every tenant table already carries a schema and that
    # selector reports nothing. "not dispatch_core" survives the mutation.
    tenant_tables = [t for t in Base.metadata.tables.values() if t.schema != "dispatch_core"]
    assert tenant_tables, "tenant table selection collapsed to empty"
    for table in tenant_tables:
        table.schema = SCHEMA_NAME
    Base.metadata.create_all(engine, tables=tenant_tables)

    with engine.begin() as conn:
        conn.execute(text(f'set search_path to "{SCHEMA_NAME}"'))
        conn.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num varchar(32) not null)")
        )

    yield engine
    engine.dispose()
    drop_database(uri)


def _stamp(engine, revision: str) -> None:
    with engine.begin() as conn:
        conn.execute(text(f'set search_path to "{SCHEMA_NAME}"'))
        conn.execute(text("DELETE FROM alembic_version"))
        conn.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:rev)"), {"rev": revision}
        )


def _upgrade(revision: str) -> subprocess.CompletedProcess:
    """Runs `dispatch database upgrade` in a subprocess against the scratch db.

    Not `alembic.command.upgrade()` in-process: alembic's env.py builds its own
    engine from `dispatch.config.SQLALCHEMY_DATABASE_URI`, the module-global
    config this process already imported (and the autouse `session` fixture
    forces to the shared `dispatch-test` database) -- it ignores the
    script_location-only AlembicConfig a caller passes in. An in-process call
    would silently migrate the wrong database and report success. A
    subprocess with DATABASE_NAME overridden is the same fix
    test_sample_data.py uses for the identical problem.
    """
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "dispatch.cli",
            "database",
            "upgrade",
            "--revision",
            revision,
            "--revision-type",
            "tenant",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "DATABASE_NAME": DB_NAME},
    )


# (revision immediately before the one under test, revision under test, table
# it reflects and installs a trigger on)
CASES = [
    ("65db2acae3ea", "7ddae3ba7822", "signal"),
    ("ec78c132ab93", "7db13bf5c5d7", "tag"),
    ("1dafcb9ad889", "d1b5ed66d83d", "individual_contact"),
    ("d089d1d110f0", "61a861559de9", "entity_type"),
    ("d089d1d110f0", "61a861559de9", "signal_filter"),
]


@pytest.mark.parametrize(
    "down_revision,revision,table_name",
    CASES,
    ids=[f"{rev}:{table}" for _, rev, table in CASES],
)
def test_reflection_based_trigger_migration_targets_the_tenant_schema(
    reflection_migration_engine, down_revision, revision, table_name
):
    """The migration must apply, and must install a trigger on its table.

    Before the fix, every one of these migrations resolved its reflection
    schema from `conn.dialect.default_schema_name` and raised (NoSuchTableError
    for three of them, TypeError from removed 1.x kwargs for 61a861559de9) the
    moment it ran here, against a real tenant schema rather than "public".
    """
    _stamp(reflection_migration_engine, down_revision)

    result = _upgrade(revision)
    assert result.returncode == 0, (
        f"`dispatch database upgrade --revision {revision}` failed:\n{result.stdout}\n{result.stderr}"
    )

    with reflection_migration_engine.connect() as conn:
        conn.execute(text(f'set search_path to "{SCHEMA_NAME}"'))
        trigger = conn.execute(
            text(
                "SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                "WHERE c.relname = :table AND NOT t.tgisinternal"
            ),
            {"table": table_name},
        ).first()

    assert trigger is not None, f"{table_name} has no search_vector trigger installed"
