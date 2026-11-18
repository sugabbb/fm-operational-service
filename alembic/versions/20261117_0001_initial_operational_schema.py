"""Create the independent operational-service schema.

Revision ID: 20261117_0001
Revises: None
Create Date: 2026-11-17
"""

from alembic import op
from app.db import models  # noqa: F401
from app.db.base import Base

revision = "20261117_0001"
down_revision = None
branch_labels = None
depends_on = None

TABLE_NAMES = [
    "users",
    "auth_login_attempts",
    "employees",
    "livestock_groups",
    "feed_types",
    "daily_records",
    "feed_usages",
    "egg_harvests",
    "inventory_opening_balances",
    "inventory_adjustments",
    "population_adjustments",
    "external_inventory_sources",
    "external_inventory_lines",
    "lele_samplings",
    "lele_sampling_measurements",
    "lele_sampling_results",
    "audit_events",
    "lele_biomass_jobs",
    "lele_biomass_snapshots",
    "lele_biomass_chart_dates",
    "lele_biomass_chart_series",
    "lele_biomass_chart_values",
]


def tables() -> list:
    return [Base.metadata.tables[name] for name in TABLE_NAMES]


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), tables=tables(), checkfirst=False)


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), tables=tables(), checkfirst=False)
