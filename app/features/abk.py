"""ABK Web App: endpoints used by ABK (field crew) without logging in.

Every request is authenticated by the ``X-ABK-Username`` and ``X-ABK-PIN`` headers (see
``app.core.dependencies.current_abk``); there is no session or token. ABK can only read the form
options and submit daily reports. A submitted report is a DRAFT that affects no inventory,
population or biomass figure until an Admin/Superadmin approves it (``abk_admin``), which creates
the real daily record.
"""

from datetime import date
from typing import Any

from fastapi import APIRouter, Query, status
from pydantic import Field, field_validator
from sqlalchemy import Select, func, select
from sqlalchemy.orm import selectinload

from app.core.decimal import decimal_string, quantity
from app.core.dependencies import CurrentAbk, DbSession
from app.core.errors import AppError, conflict, not_found, validation
from app.core.pagination import PageDep
from app.db.models import (
    AbkAccount,
    AbkDailyReport,
    AbkDailyReportEgg,
    AbkDailyReportEggOutflow,
    AbkDailyReportFeed,
    AbkEggSale,
    DailyRecord,
    FeedType,
    LivestockGroup,
)
from app.features import finance_client
from app.features.abk_egg_sales import MAX_CUSTOMERS, EggOutflowInput, sale_out, unique_customers
from app.features.common import audit
from app.features.farm import DailyRow
from app.features.farm_service import EGG_CATEGORIES, population, validate_daily_lines

router = APIRouter(prefix="/abk", tags=["ABK Web App"])


class AbkDailyReportInput(DailyRow):
    """Same shape and rules as one admin daily-record row; the species comes from the group.

    ``egg_outflows`` (Ayam only, optional): eggs that left the farm that day, one entry per
    customer. Leave it out (or send an empty list) when no eggs left. A Superadmin adds the prices
    when approving."""

    egg_outflows: list[EggOutflowInput] | None = Field(default=None, max_length=MAX_CUSTOMERS)

    @field_validator("egg_outflows")
    @classmethod
    def one_entry_per_customer(cls, value: list[EggOutflowInput] | None) -> list[EggOutflowInput] | None:
        unique_customers(value or [])
        return value


def report_query() -> Select[tuple[AbkDailyReport]]:
    return select(AbkDailyReport).options(
        selectinload(AbkDailyReport.employee),
        selectinload(AbkDailyReport.group),
        selectinload(AbkDailyReport.reviewer),
        selectinload(AbkDailyReport.feeds).selectinload(AbkDailyReportFeed.feed_type),
        selectinload(AbkDailyReport.eggs),
        selectinload(AbkDailyReport.egg_outflows),
        selectinload(AbkDailyReport.egg_sales).selectinload(AbkEggSale.lines),
        selectinload(AbkDailyReport.egg_sales).selectinload(AbkEggSale.approver),
    )


def submission_out(row: AbkDailyReport) -> dict[str, object]:
    """The report as the ABK submitted it (shared by the staff and the ABK views)."""
    return {
        "id": row.id,
        "status": row.status,
        "group": {
            "id": row.group.id,
            "code": row.group.code,
            "name": row.group.name,
            "location": row.group.location,
            "species": row.group.species,
        },
        "record_date": row.record_date.isoformat(),
        "mortality": row.mortality,
        "notes": row.notes,
        "feeds": [
            {
                "feed_type_id": line.feed_type_id,
                "feed_name": line.feed_type.name,
                "morning_kg": decimal_string(line.morning_kg),
                "noon_kg": decimal_string(line.noon_kg),
                "evening_kg": decimal_string(line.evening_kg),
                "quantity_kg": decimal_string(line.quantity_kg),
            }
            for line in row.feeds
        ],
        "eggs": {
            line.egg_category: {"weight_kg": decimal_string(line.weight_kg), "quantity_pieces": line.quantity_pieces}
            for line in row.eggs
        },
        # As reported by the ABK, one entry per customer (no price); empty when no eggs left.
        "egg_outflows": egg_outflows_out(row),
        "submitted_at": row.created_at.isoformat(),
    }


def report_out(row: AbkDailyReport) -> dict[str, object]:
    """Staff view (Admin/Superadmin)."""
    return submission_out(row) | {
        "employee": {"id": row.employee.id, "name": row.employee.name},
        # As confirmed by the Superadmin on approval, one per customer, with prices and the delivery
        # to finance-service.
        "egg_sales": [sale_out(sale) for sale in row.egg_sales],
        "reviewed_by": {"id": row.reviewer.id, "name": row.reviewer.name} if row.reviewer else None,
        "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
        "rejection_reason": row.rejection_reason,
        "daily_record_id": row.daily_record_id,
    }


def abk_report_out(row: AbkDailyReport) -> dict[str, object]:
    """ABK view of their own report: what they submitted and the review outcome. Never the approved
    egg sales (customers, quantities, and prices as confirmed by the Superadmin), their delivery
    to finance-service, the reviewer, or internal ids."""
    return submission_out(row) | {
        "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
        # Why it was rejected, so the ABK can submit a corrected report.
        "rejection_reason": row.rejection_reason,
    }


def own_reports(account: AbkAccount) -> Select[tuple[AbkDailyReport]]:
    """Only the reports submitted by this ABK (their employee)."""
    return (
        select(AbkDailyReport)
        .where(AbkDailyReport.employee_id == account.employee_id)
        .options(
            selectinload(AbkDailyReport.group),
            selectinload(AbkDailyReport.feeds).selectinload(AbkDailyReportFeed.feed_type),
            selectinload(AbkDailyReport.eggs),
            selectinload(AbkDailyReport.egg_outflows),
        )
    )


def load_own_report(db: DbSession, account: AbkAccount, report_id: int) -> AbkDailyReport:
    row = db.scalar(own_reports(account).where(AbkDailyReport.id == report_id))
    if not row:  # another ABK's report is "not found" too: its existence is not revealed
        raise not_found("ABK daily report")
    return row


def egg_outflows_out(row: AbkDailyReport) -> list[dict[str, object]]:
    """The ABK's outflow lines grouped by customer, in the order they were entered."""
    customers: dict[int, dict[str, Any]] = {}
    for line in row.egg_outflows:
        entry = customers.setdefault(
            line.customer_id, {"customer": {"id": line.customer_id, "name": line.customer_name}, "lines": []}
        )
        entry["lines"].append(line)
    for entry in customers.values():
        entry["lines"] = [
            {
                "egg_category": line.egg_category,
                "quantity_pieces": line.quantity_pieces,
                "weight_kg": decimal_string(line.weight_kg),
            }
            for line in sorted(entry["lines"], key=lambda item: EGG_CATEGORIES.index(item.egg_category))
        ]
    return list(customers.values())


SCOPE_LABELS = {"AYAM": "chicken (Ayam)", "LELE": "fish (Lele)"}


def require_scope(account: AbkAccount, species: str, detail: str | None = None) -> None:
    if species not in account.scopes:
        raise AppError(
            403,
            "ABK_SCOPE_FORBIDDEN",
            detail or f"Your ABK account is not assigned to {SCOPE_LABELS[species]} reports.",
            extra={"scopes": account.scopes},
        )


def load_report(db: DbSession, report_id: int) -> AbkDailyReport:
    row = db.scalar(report_query().where(AbkDailyReport.id == report_id))
    if not row:
        raise not_found("ABK daily report")
    return row


@router.get("/form-options")
def form_options(account: CurrentAbk, db: DbSession) -> dict[str, object]:
    """Checks the ABK credentials and returns what the daily-report form needs: the ABK's identity and
    scopes, and the active livestock groups and feed types of those scopes. ``can_report_egg_sales``
    tells whether to show the egg-outflow section (chicken scope). With no scope the lists are empty:
    show that the ABK must ask an Admin for an assignment."""
    scopes = account.scopes
    groups = db.scalars(
        select(LivestockGroup)
        .where(LivestockGroup.status == "ACTIVE", LivestockGroup.species.in_(scopes))
        .order_by(LivestockGroup.species, LivestockGroup.name, LivestockGroup.id)
    ).all()
    feeds = db.scalars(
        select(FeedType)
        .where(FeedType.is_active.is_(True), FeedType.species.in_(scopes))
        .order_by(FeedType.species, FeedType.name)
    ).all()
    return {
        "data": {
            "scopes": scopes,
            "can_report_egg_sales": "AYAM" in scopes,
            "employee": {
                "id": account.employee.id,
                "name": account.employee.name,
                "job_title": account.employee.job_title,
                "username": account.username,
            },
            "livestock_groups": [
                {
                    "id": group.id,
                    "species": group.species,
                    "code": group.code,
                    "name": group.name,
                    "location": group.location,
                    "start_date": group.start_date.isoformat(),
                }
                for group in groups
            ],
            "feed_types": [
                {"id": feed.id, "species": feed.species, "code": feed.code, "name": feed.name} for feed in feeds
            ],
        }
    }


@router.get("/customers")
def customers(
    account: CurrentAbk, page: PageDep, q: str | None = Query(default=None, max_length=100)
) -> dict[str, object]:
    """Active customers for the egg-outflow customer picker, read from finance-service (which owns
    customers). Only ids and names are exposed to ABK, and only to ABK with the chicken scope."""
    require_scope(account, "AYAM", "Egg sales are reported only by ABK assigned to chicken (Ayam).")
    try:
        rows, meta = finance_client.list_active_customers(q, page.page, page.per_page)
    except finance_client.FinanceUnavailable as exc:
        raise finance_client.unavailable() from exc
    except finance_client.FinanceRejected as exc:
        raise AppError(502, "FINANCE_ERROR", "The finance service rejected the request.") from exc
    return {"data": [{"id": row.id, "name": row.name} for row in rows], "meta": meta}


@router.post("/daily-reports", status_code=status.HTTP_201_CREATED)
def submit_daily_report(payload: AbkDailyReportInput, account: CurrentAbk, db: DbSession) -> dict[str, object]:
    """Submits a daily report as a DRAFT. It changes no inventory, population or biomass figure
    until an Admin/Superadmin approves it."""
    group = db.get(LivestockGroup, payload.group_id)
    if not group or group.status != "ACTIVE":
        raise validation("group_id", "Group must be active.")
    require_scope(account, group.species)
    if payload.record_date < group.start_date:
        raise validation("record_date", "Record date cannot be before group start date.")
    if db.scalar(
        select(DailyRecord.id).where(DailyRecord.group_id == group.id, DailyRecord.record_date == payload.record_date)
    ):
        raise conflict("DAILY_RECORD_EXISTS", "A daily record already exists for this group and date.")
    if db.scalar(
        select(AbkDailyReport.id).where(
            AbkDailyReport.group_id == group.id,
            AbkDailyReport.record_date == payload.record_date,
            AbkDailyReport.status == "DRAFT",
        )
    ):
        raise conflict("ABK_REPORT_PENDING", "A report for this group and date is already waiting for approval.")
    feed_lines, egg_lines = validate_daily_lines(db, group.species, payload.model_dump(exclude={"egg_outflows"}))
    if payload.mortality > population(db, group, payload.record_date):
        raise validation("mortality", "Mortality exceeds the population on the record date.")
    outflows = payload.egg_outflows or []
    if outflows and group.species != "AYAM":
        raise validation("egg_outflows", "Egg outflows are only valid for Ayam.")
    # Checked last: the only part that needs finance-service.
    customers = [
        finance_client.active_customer(outflow.customer_id, f"egg_outflows.{index}.customer_id")
        for index, outflow in enumerate(outflows)
    ]
    report = AbkDailyReport(
        employee_id=account.employee_id,
        group_id=group.id,
        record_date=payload.record_date,
        mortality=payload.mortality,
        notes=payload.notes,
        status="DRAFT",
    )
    db.add(report)
    db.flush()
    for line in feed_lines:
        db.add(
            AbkDailyReportFeed(
                report_id=report.id,
                feed_type_id=line.feed_type_id,
                morning_kg=line.morning_kg,
                noon_kg=line.noon_kg,
                evening_kg=line.evening_kg,
                quantity_kg=line.quantity_kg,
            )
        )
    for egg in egg_lines:
        db.add(
            AbkDailyReportEgg(
                report_id=report.id,
                egg_category=egg.egg_category,
                weight_kg=egg.weight_kg,
                quantity_pieces=egg.quantity_pieces,
            )
        )
    for outflow, customer in zip(outflows, customers, strict=True):
        for outflow_line in outflow.lines:
            db.add(
                AbkDailyReportEggOutflow(
                    report_id=report.id,
                    customer_id=customer.id,
                    customer_name=customer.name,
                    egg_category=outflow_line.egg_category,
                    quantity_pieces=outflow_line.quantity_pieces,
                    weight_kg=quantity(outflow_line.weight_kg),
                )
            )
    audit(db, report, "abk_daily_report_submitted", None, reason=f"abk:{account.username}")
    db.commit()
    return {"data": abk_report_out(load_own_report(db, account, report.id))}


@router.get("/daily-reports")
def my_daily_reports(
    account: CurrentAbk,
    db: DbSession,
    page: PageDep,
    report_status: str | None = Query(default=None, alias="status", pattern="^(DRAFT|APPROVED|REJECTED)$"),
    species: str | None = Query(default=None, pattern="^(AYAM|LELE)$"),
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
) -> dict[str, object]:
    """The reports this ABK submitted (never anyone else's), newest record date first, with their
    review outcome. Reports of a species the ABK is no longer assigned to stay visible."""
    stmt = own_reports(account)
    if report_status:
        stmt = stmt.where(AbkDailyReport.status == report_status)
    if species:
        stmt = stmt.join(AbkDailyReport.group).where(LivestockGroup.species == species)
    if from_date:
        stmt = stmt.where(AbkDailyReport.record_date >= from_date)
    if to_date:
        stmt = stmt.where(AbkDailyReport.record_date <= to_date)
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    rows = db.scalars(
        stmt.order_by(AbkDailyReport.record_date.desc(), AbkDailyReport.id.desc())
        .offset(page.offset)
        .limit(page.per_page)
    ).all()
    return {"data": [abk_report_out(row) for row in rows], "meta": page.meta(total)}


@router.get("/daily-reports/{report_id}")
def my_daily_report(report_id: int, account: CurrentAbk, db: DbSession) -> dict[str, object]:
    """One report this ABK submitted; another ABK's report answers 404 like a missing one."""
    return {"data": abk_report_out(load_own_report(db, account, report_id))}
