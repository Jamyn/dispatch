"""Installs the missing full-text search triggers on tenant tables

`setup_fulltext_search` only ever runs from `init_schema`, which provisions a
brand new organization. Every tenant table created by a migration since then
got its `search_vector` column and GIN index but no trigger to populate it, so
full-text search on those tables silently matched nothing -- masked on most of
them by the `name ILIKE` fallback in `dispatch.database.service.search`.

Which tables are affected depends on when the deployment was first installed,
so this reconciles every tenant table carrying a `search_vector` rather than a
list frozen at authoring time. `sync_trigger` drops and recreates, so tables
that already have a correct trigger are unaffected.

This backfills by rewriting every row of each repaired table. env.py wraps a
whole schema in one transaction, so the ACCESS EXCLUSIVE that each DROP TRIGGER
takes is held -- blocking reads as well as writes -- until every table in that
schema has been rewritten, not just for its own statement. Treat it as a
maintenance window, not an incidental upgrade step.

Reading the models rather than a frozen column list is what keeps weights and
regconfig correct, but it costs the usual guarantee that a revision means the
same thing forever: if a model later drops an indexed column, replaying this
against a database old enough to predate that column emits a trigger naming a
column it does not have, and the upgrade fails here.

Revision ID: c3f1a9d24b70
Revises: ff08d822ef2c
Create Date: 2026-08-13 00:00:00.000000

"""

from alembic import op
from sqlalchemy import MetaData, inspect

from dispatch.database.manage import get_tenant_tables, sync_search_triggers

# revision identifiers, used by Alembic.
revision = "c3f1a9d24b70"
down_revision = "ff08d822ef2c"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()

    # env.py sets search_path per tenant *after* connecting, so
    # conn.dialect.default_schema_name is still "public" here.
    schema = conn.exec_driver_sql("SELECT current_schema()").scalar()
    present = set(inspect(conn).get_table_names(schema=schema))

    # Rebound through to_metadata rather than by assigning Table.schema:
    # mutating it in place leaves the shared Base.metadata keyed on the old
    # names for every later tenant this migration visits.
    target = MetaData()
    tables = [
        table.to_metadata(target, schema=schema)
        for table in get_tenant_tables()
        if table.name in present
    ]

    sync_search_triggers(conn, tables)


def downgrade():
    # Dropping the triggers would leave a fresh install worse off than before:
    # init_schema created them outside alembic's knowledge, and there is no way
    # to tell those apart from the ones this revision installed.
    pass
