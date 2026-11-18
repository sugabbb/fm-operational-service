"""ABK egg outflows and the approved egg sales delivered to finance-service.

Revision ID: 20261120_0004
Revises: 20261119_0003
"""

import sqlalchemy as sa

from alembic import op

revision = "20261120_0004"
down_revision = "20261119_0003"
branch_labels = None
depends_on = None


def timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    op.add_column("abk_daily_reports", sa.Column("egg_outflow_customer_id", sa.BigInteger(), nullable=True))
    op.add_column("abk_daily_reports", sa.Column("egg_outflow_customer_name", sa.String(255), nullable=True))
    op.create_check_constraint(
        op.f("ck_abk_daily_reports_egg_outflow_customer"),
        "abk_daily_reports",
        "(egg_outflow_customer_id IS NULL) = (egg_outflow_customer_name IS NULL)",
    )
    op.create_table(
        "abk_daily_report_egg_outflows",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("report_id", sa.BigInteger(), nullable=False),
        sa.Column("egg_category", sa.String(10), nullable=False),
        sa.Column("weight_kg", sa.Numeric(14, 3), nullable=False),
        sa.Column("quantity_pieces", sa.BigInteger(), nullable=False),
        *timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_abk_daily_report_egg_outflows"),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["abk_daily_reports.id"],
            name="fk_abk_daily_report_egg_outflows_report_id_abk_daily_reports",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("report_id", "egg_category", name="uq_abk_daily_report_egg_outflows_report_id"),
        sa.CheckConstraint(
            "egg_category IN ('UTUH','PECAH','REJECT')", name=op.f("ck_abk_daily_report_egg_outflows_category")
        ),
        sa.CheckConstraint(
            "weight_kg > 0 AND quantity_pieces > 0", name=op.f("ck_abk_daily_report_egg_outflows_quantities")
        ),
    )
    op.create_table(
        "abk_egg_sales",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("report_id", sa.BigInteger(), nullable=False),
        sa.Column("transaction_date", sa.Date(), nullable=False),
        sa.Column("customer_id", sa.BigInteger(), nullable=False),
        sa.Column("customer_name", sa.String(255), nullable=False),
        sa.Column("approved_by", sa.BigInteger(), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("finance_status", sa.String(10), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("finance_revenue_id", sa.String(255), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        *timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_abk_egg_sales"),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["abk_daily_reports.id"],
            name="fk_abk_egg_sales_report_id_abk_daily_reports",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["approved_by"], ["users.id"], name="fk_abk_egg_sales_approved_by_users", ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("report_id", name="uq_abk_egg_sales_report_id"),
        sa.CheckConstraint(
            "finance_status IN ('PENDING','SENT','FAILED')", name=op.f("ck_abk_egg_sales_finance_status")
        ),
        sa.CheckConstraint("source_revision > 0 AND attempts >= 0", name=op.f("ck_abk_egg_sales_counters")),
        sa.CheckConstraint(
            "finance_status <> 'SENT' OR (sent_at IS NOT NULL AND finance_revenue_id IS NOT NULL)",
            name=op.f("ck_abk_egg_sales_sent"),
        ),
    )
    for column in ("transaction_date", "customer_id", "finance_status"):
        op.create_index(f"ix_abk_egg_sales_{column}", "abk_egg_sales", [column])
    op.create_table(
        "abk_egg_sale_lines",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("sale_id", sa.BigInteger(), nullable=False),
        sa.Column("egg_category", sa.String(10), nullable=False),
        sa.Column("weight_kg", sa.Numeric(14, 3), nullable=False),
        sa.Column("quantity_pieces", sa.BigInteger(), nullable=False),
        sa.Column("price_per_kg", sa.Numeric(14, 2), nullable=False),
        *timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_abk_egg_sale_lines"),
        sa.ForeignKeyConstraint(
            ["sale_id"], ["abk_egg_sales.id"], name="fk_abk_egg_sale_lines_sale_id_abk_egg_sales", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("sale_id", "egg_category", name="uq_abk_egg_sale_lines_sale_id"),
        sa.CheckConstraint("egg_category IN ('UTUH','PECAH','REJECT')", name=op.f("ck_abk_egg_sale_lines_category")),
        sa.CheckConstraint(
            "weight_kg > 0 AND quantity_pieces > 0 AND price_per_kg > 0", name=op.f("ck_abk_egg_sale_lines_values")
        ),
    )


def downgrade() -> None:
    op.drop_table("abk_egg_sale_lines")
    op.drop_table("abk_egg_sales")
    op.drop_table("abk_daily_report_egg_outflows")
    op.drop_constraint(op.f("ck_abk_daily_reports_egg_outflow_customer"), "abk_daily_reports", type_="check")
    op.drop_column("abk_daily_reports", "egg_outflow_customer_name")
    op.drop_column("abk_daily_reports", "egg_outflow_customer_id")
