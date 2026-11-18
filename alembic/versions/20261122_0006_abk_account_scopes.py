"""ABK account scopes: chicken (Ayam) and/or fish (Lele), or none.

Existing accounts could report on both species, so they get both scopes; new accounts get the
scopes an Admin sets when creating them.

Revision ID: 20261122_0006
Revises: 20261121_0005
"""

import sqlalchemy as sa

from alembic import op

revision = "20261122_0006"
down_revision = "20261121_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for column in ("scope_ayam", "scope_lele"):
        op.add_column("abk_accounts", sa.Column(column, sa.Boolean(), nullable=False, server_default=sa.true()))
        op.alter_column("abk_accounts", column, server_default=None)


def downgrade() -> None:
    op.drop_column("abk_accounts", "scope_lele")
    op.drop_column("abk_accounts", "scope_ayam")
