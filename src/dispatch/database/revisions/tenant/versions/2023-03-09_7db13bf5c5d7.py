"""Adds missing tag search columns

Revision ID: 7db13bf5c5d7
Revises: ec78c132ab93
Create Date: 2023-03-09 16:31:22.963497

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.schema import MetaData

from dispatch.search.fulltext import sync_trigger


# revision identifiers, used by Alembic.
revision = "7db13bf5c5d7"
down_revision = "ec78c132ab93"
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
    table = sa.Table("tag", metadata, autoload_with=conn)
    sync_trigger(conn, table, "search_vector", ["name", "description", "external_id"])


def downgrade():
    pass
