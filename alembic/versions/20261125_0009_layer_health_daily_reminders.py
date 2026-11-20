"""Layer health reminders prepared by the nightly job (read model).

Empty after the upgrade: run ``python -m app.scripts.layer_health_reminder_job`` once.

Revision ID: 20261125_0009
Revises: 20261124_0008
"""

import sqlalchemy as sa

from alembic import op

revision = "20261125_0009"
down_revision = "20261124_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "layer_health_daily_reminders",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("reminder_date", sa.Date(), nullable=False),
        sa.Column("age_week", sa.Integer(), nullable=False),
        sa.Column("age_day", sa.SmallInteger(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_layer_health_daily_reminders"),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["livestock_groups.id"],
            name="fk_layer_health_daily_reminders_group_id_livestock_groups",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("group_id", "reminder_date", name="uq_layer_health_daily_reminders_group_id"),
        sa.CheckConstraint(
            "age_week >= 0 AND age_day BETWEEN 1 AND 7", name=op.f("ck_layer_health_daily_reminders_age")
        ),
    )
    op.create_index("ix_layer_health_daily_reminders_reminder_date", "layer_health_daily_reminders", ["reminder_date"])
    op.create_table(
        "layer_health_daily_reminder_tasks",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("reminder_id", sa.BigInteger(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("task_key", sa.String(120), nullable=False),
        sa.Column("type", sa.String(12), nullable=False),
        sa.Column("vaccination_id", sa.BigInteger(), nullable=True),
        sa.Column("medicine_id", sa.BigInteger(), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("target_disease", sa.String(255), nullable=True),
        sa.Column("purpose", sa.String(255), nullable=True),
        sa.Column("method", sa.String(20), nullable=False),
        sa.Column("dosage", sa.String(255), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("window_start", sa.Date(), nullable=True),
        sa.Column("window_end", sa.Date(), nullable=True),
        sa.Column("course_day", sa.SmallInteger(), nullable=True),
        sa.Column("course_days", sa.SmallInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_layer_health_daily_reminder_tasks"),
        sa.ForeignKeyConstraint(
            ["reminder_id"],
            ["layer_health_daily_reminders.id"],
            name="fk_layer_health_daily_reminder_tasks_reminder_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["vaccination_id"],
            ["layer_vaccinations.id"],
            name="fk_layer_health_daily_reminder_tasks_vaccination_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["medicine_id"],
            ["layer_medicine_programs.id"],
            name="fk_layer_health_daily_reminder_tasks_medicine_id",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("reminder_id", "position", name="uq_layer_health_daily_reminder_tasks_reminder_id"),
        sa.UniqueConstraint("task_key", name="uq_layer_health_daily_reminder_tasks_task_key"),
        sa.CheckConstraint(
            "type IN ('VACCINATION','MEDICINE')", name=op.f("ck_layer_health_daily_reminder_tasks_type")
        ),
        sa.CheckConstraint(
            "(type = 'VACCINATION' AND vaccination_id IS NOT NULL AND medicine_id IS NULL"
            " AND window_start IS NOT NULL AND window_end = window_start + 6"
            " AND course_day IS NULL AND course_days IS NULL)"
            " OR (type = 'MEDICINE' AND medicine_id IS NOT NULL AND vaccination_id IS NULL"
            " AND window_start IS NULL AND window_end IS NULL"
            " AND course_day BETWEEN 1 AND course_days)",
            name=op.f("ck_layer_health_daily_reminder_tasks_shape"),
        ),
    )
    for column in ("reminder_id", "vaccination_id", "medicine_id"):
        op.create_index(f"ix_layer_health_daily_reminder_tasks_{column}", "layer_health_daily_reminder_tasks", [column])


def downgrade() -> None:
    op.drop_table("layer_health_daily_reminder_tasks")
    op.drop_table("layer_health_daily_reminders")
