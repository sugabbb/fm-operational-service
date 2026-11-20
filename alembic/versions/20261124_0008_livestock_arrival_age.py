"""Age of the hens on arrival (Ayam groups), in whole weeks.

Existing Ayam groups get 13 weeks; Lele groups have none. The layer health program is timed by the
age that follows from it.

Revision ID: 20261124_0008
Revises: 20261123_0007
"""

import sqlalchemy as sa

from alembic import op

revision = "20261124_0008"
down_revision = "20261123_0007"
branch_labels = None
depends_on = None

DEFAULT_ARRIVAL_AGE_WEEK = 13


CONSTRAINT = "ck_livestock_groups_arrival_age_week"


def upgrade() -> None:
    # Idempotent like 20261118_0002: on a new database, 20261117_0001 already creates
    # livestock_groups from the current models, with this column and constraint.
    op.execute("ALTER TABLE livestock_groups ADD COLUMN IF NOT EXISTS arrival_age_week integer")
    op.execute(
        sa.text(
            "UPDATE livestock_groups SET arrival_age_week = :age WHERE species = 'AYAM' AND arrival_age_week IS NULL"
        ).bindparams(age=DEFAULT_ARRIVAL_AGE_WEEK)
    )
    op.execute(f"ALTER TABLE livestock_groups DROP CONSTRAINT IF EXISTS {CONSTRAINT}")
    op.execute(
        f"ALTER TABLE livestock_groups ADD CONSTRAINT {CONSTRAINT} CHECK ("
        "(species = 'AYAM' AND arrival_age_week IS NOT NULL AND arrival_age_week >= 0)"
        " OR (species <> 'AYAM' AND arrival_age_week IS NULL))"
    )


def downgrade() -> None:
    op.execute(f"ALTER TABLE livestock_groups DROP CONSTRAINT IF EXISTS {CONSTRAINT}")
    op.execute("ALTER TABLE livestock_groups DROP COLUMN IF EXISTS arrival_age_week")
