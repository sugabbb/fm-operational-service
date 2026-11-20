"""Layer health program setup: vaccination schedule and medicine program (timed by the hens' age).

Revision ID: 20261123_0007
Revises: 20261122_0006
"""

import sqlalchemy as sa

from alembic import op

revision = "20261123_0007"
down_revision = "20261122_0006"
branch_labels = None
depends_on = None

# Frozen copy: a later change of the allowed methods needs its own migration.
METHODS_SQL = (
    "'EYE_DROP','NASAL_DROP','ORAL','DRINKING_WATER','FEED','SPRAY','INTRAMUSCULAR','SUBCUTANEOUS','WING_WEB','OTHER'"
)


def timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "layer_vaccinations",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("age_week", sa.Integer(), nullable=False),
        sa.Column("vaccine_name", sa.String(255), nullable=False),
        sa.Column("target_disease", sa.String(255), nullable=True),
        sa.Column("method", sa.String(20), nullable=False),
        sa.Column("dosage", sa.String(255), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        *timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_layer_vaccinations"),
        sa.CheckConstraint("age_week >= 0", name=op.f("ck_layer_vaccinations_age_week")),
        sa.CheckConstraint(f"method IN ({METHODS_SQL})", name=op.f("ck_layer_vaccinations_method")),
    )
    op.create_index("ix_layer_vaccinations_age_week", "layer_vaccinations", ["age_week"])
    op.create_index("ix_layer_vaccinations_is_active", "layer_vaccinations", ["is_active"])
    op.create_index(
        "uq_layer_vaccinations_age_vaccine",
        "layer_vaccinations",
        ["age_week", sa.text("lower(vaccine_name)")],
        unique=True,
    )
    op.create_table(
        "layer_medicine_programs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("medicine_name", sa.String(255), nullable=False),
        sa.Column("purpose", sa.String(255), nullable=True),
        sa.Column("method", sa.String(20), nullable=False),
        sa.Column("dosage", sa.String(255), nullable=True),
        sa.Column("start_age_week", sa.Integer(), nullable=False),
        sa.Column("start_day", sa.SmallInteger(), nullable=False),
        sa.Column("course_days", sa.SmallInteger(), nullable=False),
        sa.Column("interval_value", sa.Integer(), nullable=False),
        sa.Column("interval_unit", sa.String(10), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        *timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_layer_medicine_programs"),
        sa.CheckConstraint("start_age_week >= 0", name=op.f("ck_layer_medicine_programs_start_age_week")),
        sa.CheckConstraint("start_day BETWEEN 1 AND 7", name=op.f("ck_layer_medicine_programs_start_day")),
        sa.CheckConstraint("interval_value > 0", name=op.f("ck_layer_medicine_programs_interval_value")),
        sa.CheckConstraint(
            "interval_unit IN ('DAY','WEEK','MONTH')", name=op.f("ck_layer_medicine_programs_interval_unit")
        ),
        sa.CheckConstraint(
            "course_days > 0 AND course_days <= interval_value * "
            "CASE interval_unit WHEN 'DAY' THEN 1 WHEN 'WEEK' THEN 7 ELSE 28 END",
            name=op.f("ck_layer_medicine_programs_course_days"),
        ),
        sa.CheckConstraint(f"method IN ({METHODS_SQL})", name=op.f("ck_layer_medicine_programs_method")),
    )
    op.create_index("ix_layer_medicine_programs_is_active", "layer_medicine_programs", ["is_active"])
    op.create_index(
        "uq_layer_medicine_programs_name",
        "layer_medicine_programs",
        [sa.text("lower(medicine_name)")],
        unique=True,
    )


def downgrade() -> None:
    op.drop_table("layer_medicine_programs")
    op.drop_table("layer_vaccinations")
