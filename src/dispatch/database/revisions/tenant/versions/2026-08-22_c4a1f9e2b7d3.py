"""One conference per incident

Revision ID: c4a1f9e2b7d3
Revises: b8d2f61c07ae
Create Date: 2026-08-22

"""

import logging

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "c4a1f9e2b7d3"
down_revision = "b8d2f61c07ae"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")


def upgrade():
    # Duplicates are reachable on a database that ran the racing flow, and only
    # one of them was ever reachable through `incident.conference` anyway. The
    # rest are detached rather than deleted -- the row is the only surviving
    # record of a live provider meeting, and deleting it would destroy the id an
    # operator needs to shut that meeting down.
    detached = (
        op.get_bind()
        .execute(
            sa.text(
                """
            UPDATE conference SET incident_id = NULL
            WHERE incident_id IS NOT NULL
              AND id NOT IN (
                  SELECT MIN(id) FROM conference
                  WHERE incident_id IS NOT NULL
                  GROUP BY incident_id
              )
            RETURNING id, resource_id
            """
            )
        )
        .fetchall()
    )

    for row in detached:
        # Which duplicate `incident.conference` resolved to was never defined --
        # the lazy load has no ORDER BY -- so MIN(id) is chosen to make it
        # deterministic, keeping the oldest. This log line is the only trace the
        # rest will get.
        log.warning(
            "Conference %s (provider meeting %s) duplicated its incident's bridge and was "
            "detached. Nothing in Dispatch referenced it: close it at the provider.",
            row.id,
            row.resource_id,
        )

    op.create_unique_constraint("conference_incident_id_key", "conference", ["incident_id"])


def downgrade():
    op.drop_constraint("conference_incident_id_key", "conference", type_="unique")
