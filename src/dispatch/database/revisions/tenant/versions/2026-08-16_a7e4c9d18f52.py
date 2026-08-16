"""Indexes the project label ordering the Slack project selector uses

`project_service.get_all_enabled` orders by
`lower(coalesce(nullif(display_name, ''), name)), id` over enabled projects.
That expression was unindexed, so every call was a sequential scan of the
project table plus a top-N sort -- including the `limit=MAX_SELECT_OPTIONS + 1`
probe `project_select` runs on every modal build that offers a project, which
is most of them.

The index expression has to stay identical to the ordering expression in
`get_all_enabled`, or Postgres will not use it. Both live in this repository;
change them together.

CONCURRENTLY is deliberately not used: env.py runs each tenant schema in one
transaction and CREATE INDEX CONCURRENTLY cannot run inside one. `project` is
small (thousands of rows at the outside) and near-static, so the brief lock is
not worth splitting the migration's transaction handling for.

Revision ID: a7e4c9d18f52
Revises: c3f1a9d24b70
Create Date: 2026-08-16 00:00:00.000000

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "a7e4c9d18f52"
down_revision = "c3f1a9d24b70"
branch_labels = None
depends_on = None

INDEX_NAME = "project_label_idx"


def upgrade():
    # IF NOT EXISTS rather than a plain create: a schema provisioned by
    # `init_schema` after this revision was written already has the index from
    # the model, and is then stamped rather than migrated -- but an older
    # tenant in the same database still has to be upgraded, and the two are not
    # distinguishable from here.
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} ON project "
        "(lower(coalesce(nullif(display_name, ''), name)), id) WHERE enabled"
    )


def downgrade():
    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
