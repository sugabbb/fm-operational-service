"""ABK Web App: ABK PIN accounts and draft daily reports.

Revision ID: 20261119_0003
Revises: 20261118_0002
"""

import sqlalchemy as sa

from alembic import op

revision = "20261119_0003"
down_revision = "20261118_0002"
branch_labels = None
depends_on = None


def timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "abk_accounts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("employee_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(60), nullable=False),
        sa.Column("pin_hash", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("failed_pin_attempts", sa.Integer(), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pin_changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_authenticated_at", sa.DateTime(timezone=True), nullable=True),
        *timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_abk_accounts"),
        sa.ForeignKeyConstraint(
            ["employee_id"], ["employees.id"], name="fk_abk_accounts_employee_id_employees", ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("employee_id", name="uq_abk_accounts_employee_id"),
        sa.UniqueConstraint("username", name="uq_abk_accounts_username"),
        sa.CheckConstraint("username = lower(username)", name=op.f("ck_abk_accounts_username_lowercase")),
        sa.CheckConstraint("failed_pin_attempts >= 0", name=op.f("ck_abk_accounts_failed_pin_attempts")),
    )
    op.create_table(
        "abk_daily_reports",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("employee_id", sa.BigInteger(), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("record_date", sa.Date(), nullable=False),
        sa.Column("mortality", sa.BigInteger(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("reviewed_by", sa.BigInteger(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("daily_record_id", sa.BigInteger(), nullable=True),
        *timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_abk_daily_reports"),
        sa.ForeignKeyConstraint(
            ["employee_id"], ["employees.id"], name="fk_abk_daily_reports_employee_id_employees", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["livestock_groups.id"],
            name="fk_abk_daily_reports_group_id_livestock_groups",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by"], ["users.id"], name="fk_abk_daily_reports_reviewed_by_users", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["daily_record_id"],
            ["daily_records.id"],
            name="fk_abk_daily_reports_daily_record_id_daily_records",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("daily_record_id", name="uq_abk_daily_reports_daily_record_id"),
        sa.CheckConstraint("status IN ('DRAFT','APPROVED','REJECTED')", name=op.f("ck_abk_daily_reports_status")),
        sa.CheckConstraint("mortality >= 0", name=op.f("ck_abk_daily_reports_mortality")),
        sa.CheckConstraint(
            "(status = 'DRAFT' AND reviewed_by IS NULL AND reviewed_at IS NULL"
            " AND rejection_reason IS NULL AND daily_record_id IS NULL)"
            " OR (status = 'APPROVED' AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL"
            " AND rejection_reason IS NULL AND daily_record_id IS NOT NULL)"
            " OR (status = 'REJECTED' AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL"
            " AND rejection_reason IS NOT NULL AND daily_record_id IS NULL)",
            name=op.f("ck_abk_daily_reports_review"),
        ),
    )
    for column in ("employee_id", "group_id", "record_date", "status"):
        op.create_index(f"ix_abk_daily_reports_{column}", "abk_daily_reports", [column])
    op.create_index(
        "uq_abk_daily_reports_pending",
        "abk_daily_reports",
        ["group_id", "record_date"],
        unique=True,
        postgresql_where=sa.text("status = 'DRAFT'"),
    )
    op.create_table(
        "abk_daily_report_feeds",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("report_id", sa.BigInteger(), nullable=False),
        sa.Column("feed_type_id", sa.BigInteger(), nullable=False),
        sa.Column("morning_kg", sa.Numeric(14, 3), nullable=False),
        sa.Column("noon_kg", sa.Numeric(14, 3), nullable=False),
        sa.Column("evening_kg", sa.Numeric(14, 3), nullable=False),
        sa.Column("quantity_kg", sa.Numeric(14, 3), nullable=False),
        *timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_abk_daily_report_feeds"),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["abk_daily_reports.id"],
            name="fk_abk_daily_report_feeds_report_id_abk_daily_reports",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["feed_type_id"],
            ["feed_types.id"],
            name="fk_abk_daily_report_feeds_feed_type_id_feed_types",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("report_id", "feed_type_id", name="uq_abk_daily_report_feeds_report_id"),
        sa.CheckConstraint(
            "morning_kg >= 0 AND noon_kg >= 0 AND evening_kg >= 0 AND quantity_kg >= 0 "
            "AND quantity_kg = morning_kg + noon_kg + evening_kg",
            name=op.f("ck_abk_daily_report_feeds_quantities"),
        ),
    )
    op.create_table(
        "abk_daily_report_eggs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("report_id", sa.BigInteger(), nullable=False),
        sa.Column("egg_category", sa.String(10), nullable=False),
        sa.Column("weight_kg", sa.Numeric(14, 3), nullable=False),
        sa.Column("quantity_pieces", sa.BigInteger(), nullable=False),
        *timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_abk_daily_report_eggs"),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["abk_daily_reports.id"],
            name="fk_abk_daily_report_eggs_report_id_abk_daily_reports",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("report_id", "egg_category", name="uq_abk_daily_report_eggs_report_id"),
        sa.CheckConstraint("egg_category IN ('UTUH','PECAH','REJECT')", name=op.f("ck_abk_daily_report_eggs_category")),
        sa.CheckConstraint("weight_kg >= 0 AND quantity_pieces >= 0", name=op.f("ck_abk_daily_report_eggs_quantities")),
    )


def downgrade() -> None:
    op.drop_table("abk_daily_report_eggs")
    op.drop_table("abk_daily_report_feeds")
    op.drop_table("abk_daily_reports")
    op.drop_table("abk_accounts")
