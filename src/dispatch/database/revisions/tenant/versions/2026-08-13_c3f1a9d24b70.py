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

Note this backfills by rewriting every row of each repaired table, which on a
large `case`, `incident` or `entity` is not free -- treat it as a maintenance
window rather than an incidental upgrade step.

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
