"""Adds signal triggers

Revision ID: 7ddae3ba7822
Revises: 65db2acae3ea
Create Date: 2023-03-03 10:16:57.890859

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.schema import MetaData

from dispatch.search.fulltext import sync_trigger

# revision identifiers, used by Alembic.
revision = "7ddae3ba7822"
down_revision = "65db2acae3ea"
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
    table = sa.Table("signal", metadata, autoload_with=conn)
    sync_trigger(conn, table, "search_vector", ["name", "description", "variant"])


def downgrade():
    pass
