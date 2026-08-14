"""Adds email search.

Revision ID: d1b5ed66d83d
Revises: 1dafcb9ad889
Create Date: 2023-03-27 08:57:44.499535

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.schema import MetaData

from dispatch.search.fulltext import sync_trigger


# revision identifiers, used by Alembic.
revision = "d1b5ed66d83d"
down_revision = "1dafcb9ad889"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_context().connection
    # env.py sets search_path per tenant *after* connecting, and the dialect
    # caches default_schema_name from the first connection for the engine's
    # whole lifetime, so it reads "public" here no matter which schema is
    # active. current_schema() reflects the live search_path instead.
    schema = conn.exec_driver_sql("SELECT current_schema()").scalar()
    metadata = MetaData(schema=schema)
    metadata.bind = conn
    table = sa.Table("individual_contact", metadata, autoload_with=conn)
    sync_trigger(conn, table, "search_vector", ["name", "title", "email", "company", "notes"])


def downgrade():
    pass
