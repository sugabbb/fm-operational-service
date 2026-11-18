"""Admin/Superadmin side of the ABK Web App (Bearer JWT): ABK accounts and review of ABK reports.

Approving a DRAFT report creates the daily record through ``save_daily``, exactly like the admin
bulk daily-record endpoint, so feed/egg inventory, population checks and the Lele biomass rebuild
all apply from that moment. Rejecting it keeps it out of every figure; the ABK may resubmit.
"""

from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Body, Query, status
from pydantic import Field, field_validator
from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import selectinload

from app.core.dependencies import ActiveUser, DbSession
from app.core.errors import AppError, conflict, not_found, validation
from app.core.pagination import PageDep, StrictModel
from app.core.security import generate_pin, hash_pin
from app.db.models import AbkAccount, AbkDailyReport, AbkDailyReportEggOutflow, Employee, LivestockGroup
from app.features import finance_client
from app.features.abk import load_report, report_out, report_query
from app.features.abk_egg_sales import MAX_CUSTOMERS, EggSaleInput, create_sale, unique_customers
from app.features.abk_egg_sales import deliver_in_background as deliver_egg_sales_in_background
from app.features.common import audit, model_snapshot, now_utc
from app.features.farm_service import lock_balances, save_daily
from app.features.lele_biomass_jobs import drain_in_background as rebuild_biomass_in_background

router = APIRouter(tags=["ABK Administration"])

USERNAME_PATTERN = r"^[a-z0-9._-]{3,60}$"


class AbkUsernameInput(StrictModel):
    username: str = Field(pattern=USERNAME_PATTERN, description="3-60 of a-z 0-9 . _ -, case-insensitive.")

    @field_validator("username", mode="before")
    @classmethod
    def lower(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value


Scope = Literal["AYAM", "LELE"]
SCOPES_DESCRIPTION = (
    "What the ABK may report on: AYAM (chicken, including egg sales) and/or LELE (fish). An empty list is "
    "allowed: the ABK can sign in but submit nothing."
)


def unique_scopes(value: list[Scope] | None) -> list[Scope] | None:
    if value is not None and len(value) != len(set(value)):
        raise ValueError("scopes cannot be duplicated")
    return value


class AbkAccountCreate(AbkUsernameInput):
    employee_id: int
    scopes: list[Scope] = Field(max_length=2, description=SCOPES_DESCRIPTION)

    _unique_scopes = field_validator("scopes")(unique_scopes)


class AbkAccountUpdate(AbkUsernameInput):
    # The employee of an account never changes; another employee gets an account of their own.
    is_active: bool
    scopes: list[Scope] | None = Field(
        default=None, max_length=2, description=SCOPES_DESCRIPTION + " Omitted: unchanged."
    )

    _unique_scopes = field_validator("scopes")(unique_scopes)


def set_scopes(row: AbkAccount, scopes: list[Scope]) -> None:
    row.scope_ayam, row.scope_lele = "AYAM" in scopes, "LELE" in scopes


class AbkReportApprove(StrictModel):
    # Required (Superadmin only) when the ABK reported egg outflows; prohibited otherwise. One entry
    # per customer; the Superadmin may correct, add, or remove customers.
    egg_sales: list[EggSaleInput] | None = Field(default=None, min_length=1, max_length=MAX_CUSTOMERS)

    @field_validator("egg_sales")
    @classmethod
    def one_sale_per_customer(cls, value: list[EggSaleInput] | None) -> list[EggSaleInput] | None:
        unique_customers(value or [])
        return value


class AbkReportReject(StrictModel):
    reason: str = Field(min_length=1, max_length=3000)


def account_audit(row: AbkAccount) -> dict[str, Any]:
    snapshot = model_snapshot(row)
    snapshot.pop("pin_hash")
    return snapshot


def account_out(row: AbkAccount) -> dict[str, object]:
    return {
        "id": row.id,
        "employee": {
            "id": row.employee.id,
            "name": row.employee.name,
            "job_title": row.employee.job_title,
            "status": row.employee.status,
        },
        "username": row.username,
        "is_active": row.is_active,
        "scopes": row.scopes,
        "is_locked": bool(row.locked_until and row.locked_until > now_utc()),
        "locked_until": row.locked_until.isoformat() if row.locked_until else None,
        "pin_changed_at": row.pin_changed_at.isoformat(),
        "last_authenticated_at": row.last_authenticated_at.isoformat() if row.last_authenticated_at else None,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def load_account(db: DbSession, account_id: int) -> AbkAccount:
    row = db.scalar(select(AbkAccount).where(AbkAccount.id == account_id).options(selectinload(AbkAccount.employee)))
    if not row:
        raise not_found("ABK account")
    return row


def assert_username_free(db: DbSession, username: str, account_id: int | None = None) -> None:
    if db.scalar(select(AbkAccount.id).where(AbkAccount.username == username, AbkAccount.id != (account_id or 0))):
        raise conflict("ABK_USERNAME_EXISTS", "An ABK account with this username already exists.")


@router.get("/abk-accounts")
def list_accounts(
    _: ActiveUser,
    db: DbSession,
    page: PageDep,
    q: str | None = None,
    is_active: bool | None = None,
    scope: Literal["AYAM", "LELE", "NONE"] | None = Query(
        default=None, description="Accounts with this scope; NONE: accounts without any scope."
    ),
) -> dict[str, object]:
    stmt = select(AbkAccount).join(AbkAccount.employee).options(selectinload(AbkAccount.employee))
    if q:
        stmt = stmt.where(or_(AbkAccount.username.ilike(f"%{q}%"), Employee.name.ilike(f"%{q}%")))
    if is_active is not None:
        stmt = stmt.where(AbkAccount.is_active.is_(is_active))
    if scope == "AYAM":
        stmt = stmt.where(AbkAccount.scope_ayam.is_(True))
    elif scope == "LELE":
        stmt = stmt.where(AbkAccount.scope_lele.is_(True))
    elif scope == "NONE":
        stmt = stmt.where(AbkAccount.scope_ayam.is_(False), AbkAccount.scope_lele.is_(False))
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    rows = db.scalars(stmt.order_by(AbkAccount.username).offset(page.offset).limit(page.per_page)).all()
    return {"data": [account_out(row) for row in rows], "meta": page.meta(total)}


@router.get("/abk-accounts/{account_id}")
def get_account(account_id: int, _: ActiveUser, db: DbSession) -> dict[str, object]:
    return {"data": account_out(load_account(db, account_id))}


@router.post("/abk-accounts", status_code=status.HTTP_201_CREATED)
def create_account(payload: AbkAccountCreate, actor: ActiveUser, db: DbSession) -> dict[str, object]:
    """Gives an active employee ABK access. The generated PIN is returned only in this response."""
    employee = db.get(Employee, payload.employee_id)
    if not employee:
        raise validation("employee_id", "Employee does not exist.")
    if employee.status != "ACTIVE":
        raise validation("employee_id", "Employee must be active.")
    if db.scalar(select(AbkAccount.id).where(AbkAccount.employee_id == employee.id)):
        raise conflict("ABK_ACCOUNT_EXISTS", "This employee already has an ABK account.")
    assert_username_free(db, payload.username)
    pin = generate_pin()
    row = AbkAccount(
        employee_id=employee.id,
        username=payload.username,
        pin_hash=hash_pin(pin),
        is_active=True,
        failed_pin_attempts=0,
        pin_changed_at=now_utc(),
    )
    set_scopes(row, payload.scopes)
    db.add(row)
    db.flush()
    audit(db, row, "abk_account_created", actor, after=account_audit(row))
    db.commit()
    return {"data": account_out(load_account(db, row.id)) | {"pin": pin}}


@router.patch("/abk-accounts/{account_id}")
def update_account(account_id: int, payload: AbkAccountUpdate, actor: ActiveUser, db: DbSession) -> dict[str, object]:
    row = load_account(db, account_id)
    assert_username_free(db, payload.username, row.id)
    before = account_audit(row)
    row.username, row.is_active = payload.username, payload.is_active
    if payload.scopes is not None:
        set_scopes(row, payload.scopes)
    audit(db, row, "abk_account_updated", actor, before=before, after=account_audit(row))
    db.commit()
    return {"data": account_out(load_account(db, row.id))}


@router.post("/abk-accounts/{account_id}/reset-pin")
def reset_pin(account_id: int, actor: ActiveUser, db: DbSession) -> dict[str, object]:
    """Issues a new PIN (returned only in this response) and unlocks the account."""
    row = load_account(db, account_id)
    before = account_audit(row)
    pin = generate_pin()
    row.pin_hash = hash_pin(pin)
    row.pin_changed_at = now_utc()
    row.failed_pin_attempts = 0
    row.locked_until = None
    audit(db, row, "abk_pin_reset", actor, before=before, after=account_audit(row))
    db.commit()
    return {"data": account_out(load_account(db, row.id)) | {"pin": pin}}


@router.get("/abk-daily-reports")
def list_reports(
    _: ActiveUser,
    db: DbSession,
    page: PageDep,
    report_status: str | None = Query(default=None, alias="status", pattern="^(DRAFT|APPROVED|REJECTED)$"),
    species: str | None = Query(default=None, pattern="^(AYAM|LELE)$"),
    group_id: int | None = None,
    employee_id: int | None = None,
    has_egg_outflow: bool | None = None,
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
) -> dict[str, object]:
    """Review queue; ``status=DRAFT`` lists the reports waiting for approval, and
    ``status=DRAFT&has_egg_outflow=true`` the ones only a Superadmin can approve (they have egg
    outflows)."""
    stmt = report_query().join(AbkDailyReport.group)
    if report_status:
        stmt = stmt.where(AbkDailyReport.status == report_status)
    if species:
        stmt = stmt.where(LivestockGroup.species == species)
    if group_id:
        stmt = stmt.where(AbkDailyReport.group_id == group_id)
    if employee_id:
        stmt = stmt.where(AbkDailyReport.employee_id == employee_id)
    if has_egg_outflow is not None:
        has = exists().where(AbkDailyReportEggOutflow.report_id == AbkDailyReport.id)
        stmt = stmt.where(has if has_egg_outflow else ~has)
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
    return {"data": [report_out(row) for row in rows], "meta": page.meta(total)}


@router.get("/abk-daily-reports/{report_id}")
def get_report(report_id: int, _: ActiveUser, db: DbSession) -> dict[str, object]:
    return {"data": report_out(load_report(db, report_id))}


def lock_draft(db: DbSession, report_id: int) -> AbkDailyReport:
    row = db.scalar(
        report_query()
        .where(AbkDailyReport.id == report_id)
        .with_for_update(of=AbkDailyReport)
        .execution_options(populate_existing=True)
    )
    if not row:
        raise not_found("ABK daily report")
    if row.status != "DRAFT":
        raise conflict("ABK_REPORT_NOT_DRAFT", "Only a draft report can be reviewed.", status=row.status)
    return row


@router.post("/abk-daily-reports/{report_id}/approve")
def approve_report(
    report_id: int,
    actor: ActiveUser,
    db: DbSession,
    background_tasks: BackgroundTasks,
    payload: Annotated[AbkReportApprove | None, Body()] = None,
) -> dict[str, object]:
    """Creates the daily record from the report (same rules as the admin daily-record endpoint)
    and marks the report APPROVED. On any rule failure nothing changes and the report stays DRAFT.

    A report with egg outflows needs a Superadmin and ``egg_sales``, one per customer: the customer,
    pieces and kg (pre-fill them from the ABK's ``egg_outflows`` and correct, add, or remove
    customers if needed) with ``price_per_kg`` for each line. Each sale is then delivered to
    finance-service on its own, which records one revenue per customer."""
    sales_input = payload.egg_sales if payload else None
    # Checked again under the lock below; read first to answer before calling finance-service.
    preview = db.execute(
        select(AbkDailyReport.status, exists().where(AbkDailyReportEggOutflow.report_id == AbkDailyReport.id)).where(
            AbkDailyReport.id == report_id
        )
    ).first()
    if preview is None:
        raise not_found("ABK daily report")
    report_status, has_outflow = preview
    if report_status != "DRAFT":
        raise conflict("ABK_REPORT_NOT_DRAFT", "Only a draft report can be reviewed.", status=report_status)
    customers: list[finance_client.Customer] = []
    if has_outflow:
        if actor.role != "SUPERADMIN":
            raise AppError(
                403,
                "SUPERADMIN_REQUIRED",
                "Only a Superadmin can approve a report with egg outflows, because it needs the egg prices.",
            )
        if not sales_input:
            raise validation("egg_sales", "The egg sales with prices are required to approve this report.")
        db.rollback()  # end the read transaction: no transaction stays open while finance-service answers
        customers = [
            finance_client.active_customer(sale.customer_id, f"egg_sales.{index}.customer_id")
            for index, sale in enumerate(sales_input)
        ]
    elif sales_input is not None:
        raise validation("egg_sales", "This report has no egg outflow.")

    lock_balances(db)  # same lock order as save_daily: balances, then rows
    row = lock_draft(db, report_id)
    data: dict[str, Any] = {
        "group_id": row.group_id,
        "record_date": row.record_date,
        "mortality": row.mortality,
        "notes": row.notes,
        "feeds": [
            {
                "feed_type_id": line.feed_type_id,
                "morning_kg": line.morning_kg,
                "noon_kg": line.noon_kg,
                "evening_kg": line.evening_kg,
            }
            for line in row.feeds
        ],
        "eggs": {
            line.egg_category: {"weight_kg": line.weight_kg, "quantity_pieces": line.quantity_pieces}
            for line in row.eggs
        }
        or None,
    }
    try:
        (record,) = save_daily(db, row.group.species, [data], actor)
    except AppError as error:
        # save_daily reports fields of a bulk row ("rows.0.group_id"); here there is a single report.
        error.errors = {key.removeprefix("rows.0."): messages for key, messages in error.errors.items()}
        raise
    before = model_snapshot(row)
    row.status, row.reviewed_by, row.reviewed_at, row.daily_record_id = "APPROVED", actor.id, now_utc(), record.id
    db.flush()
    for sale_input, customer in zip(sales_input or [], customers, strict=True):
        create_sale(db, row, sale_input, customer, actor)
    if customers:
        background_tasks.add_task(deliver_egg_sales_in_background)
    audit(db, row, "abk_daily_report_approved", actor, before=before)
    db.commit()
    background_tasks.add_task(rebuild_biomass_in_background)  # Lele mortality changes the biomass
    db.expire(row)
    return {"data": report_out(load_report(db, row.id))}


@router.post("/abk-daily-reports/{report_id}/reject")
def reject_report(report_id: int, payload: AbkReportReject, actor: ActiveUser, db: DbSession) -> dict[str, object]:
    row = lock_draft(db, report_id)
    before = model_snapshot(row)
    row.status, row.reviewed_by, row.reviewed_at = "REJECTED", actor.id, now_utc()
    row.rejection_reason = payload.reason
    audit(db, row, "abk_daily_report_rejected", actor, before=before, reason=payload.reason)
    db.commit()
    db.expire(row)
    return {"data": report_out(load_report(db, row.id))}
