"""ABK egg outflows to several customers per report, one egg sale per customer.

The customer moves from the report (one per report) to each outflow line, and a report may have
one egg sale per customer instead of a single sale. Existing rows keep their single customer.

Revision ID: 20261121_0005
Revises: 20261120_0004
"""

import sqlalchemy as sa

from alembic import op

revision = "20261121_0005"
down_revision = "20261120_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("abk_daily_report_egg_outflows", sa.Column("customer_id", sa.BigInteger(), nullable=True))
    op.add_column("abk_daily_report_egg_outflows", sa.Column("customer_name", sa.String(255), nullable=True))
    op.execute(
        "UPDATE abk_daily_report_egg_outflows o"
        " SET customer_id = r.egg_outflow_customer_id, customer_name = r.egg_outflow_customer_name"
        " FROM abk_daily_reports r WHERE r.id = o.report_id"
    )
    op.alter_column("abk_daily_report_egg_outflows", "customer_id", nullable=False)
    op.alter_column("abk_daily_report_egg_outflows", "customer_name", nullable=False)
    # Same constraint name (named after the first column), now per customer.
    op.drop_constraint("uq_abk_daily_report_egg_outflows_report_id", "abk_daily_report_egg_outflows", type_="unique")
    op.create_unique_constraint(
        "uq_abk_daily_report_egg_outflows_report_id",
        "abk_daily_report_egg_outflows",
        ["report_id", "customer_id", "egg_category"],
    )
    op.drop_constraint(op.f("ck_abk_daily_reports_egg_outflow_customer"), "abk_daily_reports", type_="check")
    op.drop_column("abk_daily_reports", "egg_outflow_customer_name")
    op.drop_column("abk_daily_reports", "egg_outflow_customer_id")

    op.drop_constraint("uq_abk_egg_sales_report_id", "abk_egg_sales", type_="unique")
    op.create_unique_constraint("uq_abk_egg_sales_report_id", "abk_egg_sales", ["report_id", "customer_id"])


def downgrade() -> None:
    bind = op.get_bind()
    several = (
        bind.execute(
            sa.text(
                "SELECT report_id FROM abk_daily_report_egg_outflows GROUP BY report_id"
                " HAVING COUNT(DISTINCT customer_id) > 1"
                " UNION SELECT report_id FROM abk_egg_sales GROUP BY report_id HAVING COUNT(*) > 1"
            )
        )
        .scalars()
        .all()
    )
    if several:
        raise RuntimeError(
            f"Cannot downgrade: ABK reports {sorted(several)} have egg outflows or sales for several customers."
        )
    op.drop_constraint("uq_abk_egg_sales_report_id", "abk_egg_sales", type_="unique")
    op.create_unique_constraint("uq_abk_egg_sales_report_id", "abk_egg_sales", ["report_id"])

    op.add_column("abk_daily_reports", sa.Column("egg_outflow_customer_id", sa.BigInteger(), nullable=True))
    op.add_column("abk_daily_reports", sa.Column("egg_outflow_customer_name", sa.String(255), nullable=True))
    op.execute(
        "UPDATE abk_daily_reports r SET egg_outflow_customer_id = o.customer_id, egg_outflow_customer_name = o.customer_name"
        " FROM (SELECT DISTINCT report_id, customer_id, customer_name FROM abk_daily_report_egg_outflows) o"
        " WHERE o.report_id = r.id"
    )
    op.create_check_constraint(
        op.f("ck_abk_daily_reports_egg_outflow_customer"),
        "abk_daily_reports",
        "(egg_outflow_customer_id IS NULL) = (egg_outflow_customer_name IS NULL)",
    )
    op.drop_constraint("uq_abk_daily_report_egg_outflows_report_id", "abk_daily_report_egg_outflows", type_="unique")
    op.create_unique_constraint(
        "uq_abk_daily_report_egg_outflows_report_id", "abk_daily_report_egg_outflows", ["report_id", "egg_category"]
    )
    op.drop_column("abk_daily_report_egg_outflows", "customer_name")
    op.drop_column("abk_daily_report_egg_outflows", "customer_id")
