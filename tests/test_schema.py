from app.db import models  # noqa: F401
from app.db.base import Base


def test_schema_contains_only_operational_tables() -> None:
    assert set(Base.metadata.tables) == {
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
    }


def test_schema_excludes_money_domains() -> None:
    names = set(Base.metadata.tables)
    assert not {
        "financial_opening_balances",
        "customers",
        "operational_cashflows",
        "revenues",
        "cash_statements",
        "main_cash_accounts",
        "lenders",
        "loans",
    } & names

