"""Layer health activities (done / skipped) and reminder snoozes.

- ``layer_health_daily_reminder_tasks.occurrence_key``: what is done once (a vaccination's whole
  age week, a medicine's course day), backfilled for the reminders already stored;
- ``layer_health_reminder_snoozes``: occurrences no longer reminded (reminder context);
- ``layer_health_daily_activities``: what the ABK recorded, with the task details copied (activity
  context).

Revision ID: 20261126_0010
Revises: 20261125_0009
"""

import sqlalchemy as sa

from alembic import op

revision = "20261126_0010"
down_revision = "20261125_0009"
branch_labels = None
depends_on = None


def timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    op.add_column("layer_health_daily_reminder_tasks", sa.Column("occurrence_key", sa.String(120), nullable=True))
    # Same format as app.features.layer_health_schedule.Task.occurrence_key.
    op.execute(
        "UPDATE layer_health_daily_reminder_tasks t SET occurrence_key = CASE"
        " WHEN t.type = 'VACCINATION'"
        " THEN 'VACCINATION-' || t.vaccination_id || '-' || r.group_id || '-' || to_char(t.window_start, 'YYYY-MM-DD')"
        " ELSE t.task_key END"
        " FROM layer_health_daily_reminders r WHERE r.id = t.reminder_id"
    )
    op.alter_column("layer_health_daily_reminder_tasks", "occurrence_key", nullable=False)
    op.create_index(
        "ix_layer_health_daily_reminder_tasks_occurrence_key", "layer_health_daily_reminder_tasks", ["occurrence_key"]
    )

    op.create_table(
        "layer_health_reminder_snoozes",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("occurrence_key", sa.String(120), nullable=False),
        sa.Column("snoozed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("snoozed_by_employee_id", sa.BigInteger(), nullable=True),
        sa.Column("source", sa.String(60), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_layer_health_reminder_snoozes"),
        sa.ForeignKeyConstraint(
            ["snoozed_by_employee_id"],
            ["employees.id"],
            name="fk_layer_health_reminder_snoozes_snoozed_by_employee_id",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("occurrence_key", name="uq_layer_health_reminder_snoozes_occurrence_key"),
    )

    op.create_table(
        "layer_health_daily_activities",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("activity_date", sa.Date(), nullable=False),
        sa.Column("age_week", sa.Integer(), nullable=False),
        sa.Column("age_day", sa.SmallInteger(), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("skip_reason", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("type", sa.String(12), nullable=False),
        sa.Column("vaccination_id", sa.BigInteger(), nullable=True),
        sa.Column("medicine_id", sa.BigInteger(), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("target_disease", sa.String(255), nullable=True),
        sa.Column("purpose", sa.String(255), nullable=True),
        sa.Column("method", sa.String(20), nullable=False),
        sa.Column("dosage", sa.String(255), nullable=True),
        sa.Column("program_notes", sa.Text(), nullable=True),
        sa.Column("window_start", sa.Date(), nullable=True),
        sa.Column("window_end", sa.Date(), nullable=True),
        sa.Column("course_day", sa.SmallInteger(), nullable=True),
        sa.Column("course_days", sa.SmallInteger(), nullable=True),
        sa.Column("reminder_task_key", sa.String(120), nullable=False),
        sa.Column("occurrence_key", sa.String(120), nullable=False),
        sa.Column("recorded_by_employee_id", sa.BigInteger(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        *timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_layer_health_daily_activities"),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["livestock_groups.id"],
            name="fk_layer_health_daily_activities_group_id_livestock_groups",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["vaccination_id"],
            ["layer_vaccinations.id"],
            name="fk_layer_health_daily_activities_vaccination_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["medicine_id"],
            ["layer_medicine_programs.id"],
            name="fk_layer_health_daily_activities_medicine_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["recorded_by_employee_id"],
            ["employees.id"],
            name="fk_layer_health_daily_activities_recorded_by_employee_id",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("occurrence_key", name="uq_layer_health_daily_activities_occurrence_key"),
        sa.CheckConstraint("status IN ('DONE','SKIPPED')", name=op.f("ck_layer_health_daily_activities_status")),
        sa.CheckConstraint(
            "(status = 'DONE' AND skip_reason IS NULL) OR (status = 'SKIPPED' AND skip_reason IS NOT NULL)",
            name=op.f("ck_layer_health_daily_activities_skip_reason"),
        ),
        sa.CheckConstraint("type IN ('VACCINATION','MEDICINE')", name=op.f("ck_layer_health_daily_activities_type")),
        sa.CheckConstraint(
            "age_week >= 0 AND age_day BETWEEN 1 AND 7", name=op.f("ck_layer_health_daily_activities_age")
        ),
        sa.CheckConstraint(
            "(type = 'VACCINATION' AND vaccination_id IS NOT NULL AND medicine_id IS NULL"
            " AND window_start IS NOT NULL AND window_end = window_start + 6"
            " AND activity_date BETWEEN window_start AND window_end"
            " AND course_day IS NULL AND course_days IS NULL)"
            " OR (type = 'MEDICINE' AND medicine_id IS NOT NULL AND vaccination_id IS NULL"
            " AND window_start IS NULL AND window_end IS NULL"
            " AND course_day BETWEEN 1 AND course_days)",
            name=op.f("ck_layer_health_daily_activities_shape"),
        ),
    )
    for column in ("group_id", "activity_date", "status", "vaccination_id", "medicine_id", "recorded_by_employee_id"):
        op.create_index(f"ix_layer_health_daily_activities_{column}", "layer_health_daily_activities", [column])


def downgrade() -> None:
    op.drop_table("layer_health_daily_activities")
    op.drop_table("layer_health_reminder_snoozes")
    op.drop_index("ix_layer_health_daily_reminder_tasks_occurrence_key", "layer_health_daily_reminder_tasks")
    op.drop_column("layer_health_daily_reminder_tasks", "occurrence_key")
