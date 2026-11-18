"""Store effective dates on external inventory lines.

Revision ID: 20261118_0002
Revises: 20261117_0001
"""

from alembic import op

revision = "20261118_0002"
down_revision = "20261117_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE external_inventory_lines ADD COLUMN IF NOT EXISTS effective_date date")
    op.execute(
        "UPDATE external_inventory_lines l SET effective_date=s.effective_date "
        "FROM external_inventory_sources s WHERE l.source_id=s.id AND l.effective_date IS NULL"
    )
    op.execute("ALTER TABLE external_inventory_lines ALTER COLUMN effective_date SET NOT NULL")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_external_inventory_lines_effective_date "
        "ON external_inventory_lines (effective_date)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_external_inventory_lines_effective_date")
    op.execute("ALTER TABLE external_inventory_lines DROP COLUMN IF EXISTS effective_date")
