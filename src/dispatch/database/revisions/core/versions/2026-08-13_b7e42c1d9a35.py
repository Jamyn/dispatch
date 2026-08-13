"""Installs the missing full-text search trigger on core tables

The core-schema counterpart of the tenant revision c3f1a9d24b70.
`plugin_event` was added by a migration (ed0b0388fa3f) long after
`init_database` ran, so it carries a `search_vector` column with no trigger on
any deployment that was not provisioned from scratch afterwards.

Reconciles every core table carrying a `search_vector` for the same reason the
tenant revision does: which ones are missing depends on the install date.

Revision ID: b7e42c1d9a35
Revises: 903183fd9aee
Create Date: 2026-08-13 00:00:00.000000

"""

from alembic import op
from sqlalchemy import MetaData, inspect

from dispatch.database.manage import get_core_tables, sync_search_triggers

# revision identifiers, used by Alembic.
revision = "b7e42c1d9a35"
down_revision = "903183fd9aee"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()

    schema = conn.exec_driver_sql("SELECT current_schema()").scalar()
    present = set(inspect(conn).get_table_names(schema=schema))

    # get_core_tables() already carries schema="dispatch_core"; rebinding to the
    # schema actually in the search_path keeps this correct if that ever differs.
    target = MetaData()
    tables = [
        table.to_metadata(target, schema=schema)
        for table in get_core_tables()
        if table.name in present
    ]

    sync_search_triggers(conn, tables)


def downgrade():
    # See c3f1a9d24b70: init_database installs these outside alembic's
    # knowledge, so dropping them on downgrade would break a fresh install.
    pass
