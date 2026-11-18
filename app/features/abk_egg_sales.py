"""Egg sales approved from ABK reports, and their delivery to finance-service (an outbox).

A Superadmin approving an ABK report with egg outflows confirms one sale per customer (customer,
pieces, kg, and the price per kg). Each sale is stored here as PENDING in the approval transaction,
then delivered on its own (one revenue per customer) to finance-service (``PUT /internal/revenues/egg-sales/{source_id}``, see
``docs/finance-internal-api.yaml``), which owns the revenue from then on:

- right after the approval response (FastAPI background task, :func:`deliver_in_background`);
- by ``python -m app.scripts.finance_egg_sale_worker``, which retries failed attempts;
- after ``POST /abk-egg-sales/{id}/resend``.

A temporary failure keeps the sale PENDING with an exponential back-off; a rejection marks it
FAILED until a Superadmin corrects and resends it (a correction raises ``source_revision``).
Delivery is idempotent on finance-service's side, so a retry after a lost response is harmless.
Once SENT, the sale is never sent again: corrections are made in finance-service.

operational-service never changes the egg inventory here: finance-service reports the sale back
through the internal egg-sales inventory API, like any other egg revenue.
"""

import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Body, Query
from pydantic import Field, model_validator
from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.decimal import decimal_string, quantity
from app.core.dependencies import ActiveUser, DbSession, Superadmin
from app.core.errors import conflict, not_found
from app.core.pagination import PageDep, StrictModel
from app.db.models import AbkDailyReport, AbkEggSale, AbkEggSaleLine, User
from app.db.session import SessionLocal
from app.features import finance_client
from app.features.common import audit, model_snapshot, now_utc
from app.features.farm_service import EGG_CATEGORIES

logger = logging.getLogger(__name__)
router = APIRouter(tags=["ABK Administration"])

SOURCE_SERVICE = "operational-service"
MAX_BACKOFF = timedelta(hours=1)
MAX_CUSTOMERS = 20
DELIVERIES_PER_RUN = 50


class EggOutflowLine(StrictModel):
    egg_category: Literal["UTUH", "PECAH", "REJECT"]
    quantity_pieces: int = Field(gt=0, le=100_000_000, description="Pieces (butir).")
    weight_kg: Decimal = Field(gt=0, le=Decimal("10000000"), decimal_places=3)


def unique_categories(lines: list[EggOutflowLine]) -> None:
    categories = [line.egg_category for line in lines]
    if len(categories) != len(set(categories)):
        raise ValueError("egg categories cannot be duplicated")


def unique_customers(entries: list[EggOutflowInput] | list[EggSaleInput]) -> None:
    customers = [entry.customer_id for entry in entries]
    if len(customers) != len(set(customers)):
        raise ValueError("each customer can appear only once; put all of its egg categories in one entry")


class EggOutflowInput(StrictModel):
    """Eggs that left the farm that day for one customer, as the ABK reports them (no price)."""

    customer_id: int = Field(gt=0, description="finance-service customer id (GET /abk/customers).")
    lines: list[EggOutflowLine] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def categories(self) -> EggOutflowInput:
        unique_categories(list(self.lines))
        return self


class EggSaleLineInput(EggOutflowLine):
    price_per_kg: Decimal = Field(gt=0, le=Decimal("1000000000"), decimal_places=2, description="Rupiah per kg.")


class EggSaleInput(StrictModel):
    """The sale to one customer a Superadmin confirms: the ABK's outflow, possibly corrected, with
    prices."""

    customer_id: int = Field(gt=0, description="finance-service customer id.")
    lines: list[EggSaleLineInput] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def categories(self) -> EggSaleInput:
        unique_categories(list(self.lines))
        return self


def source_id(sale: AbkEggSale) -> str:
    return f"abk-egg-sale-{sale.id}"


def set_lines(db: Session, sale: AbkEggSale, lines: list[EggSaleLineInput]) -> None:
    sale.lines.clear()
    db.flush()
    for line in sorted(lines, key=lambda item: EGG_CATEGORIES.index(item.egg_category)):
        sale.lines.append(
            AbkEggSaleLine(
                egg_category=line.egg_category,
                quantity_pieces=line.quantity_pieces,
                weight_kg=quantity(line.weight_kg),
                price_per_kg=line.price_per_kg.quantize(Decimal("0.01")),
            )
        )
    db.flush()


def sale_snapshot(sale: AbkEggSale) -> dict[str, Any]:
    return model_snapshot(sale) | {"lines": [model_snapshot(line) for line in sale.lines]}


def create_sale(
    db: Session, report: AbkDailyReport, data: EggSaleInput, customer: finance_client.Customer, actor: User
) -> AbkEggSale:
    """Stores the confirmed sale as PENDING, in the caller's (approval) transaction."""
    sale = AbkEggSale(
        report_id=report.id,
        transaction_date=report.record_date,
        customer_id=customer.id,
        customer_name=customer.name,
        approved_by=actor.id,
        approved_at=now_utc(),
        source_revision=1,
        finance_status="PENDING",
        attempts=0,
    )
    db.add(sale)
    db.flush()
    set_lines(db, sale, list(data.lines))
    audit(db, sale, "abk_egg_sale_approved", actor, after=sale_snapshot(sale))
    return sale


def finance_payload(sale: AbkEggSale) -> dict[str, Any]:
    """Request body of the delivery. Built only from stored, immutable-per-revision values, so every
    retry of a revision sends identical content (finance-service treats it as a no-op)."""
    report = sale.report
    if report.daily_record_id is None:
        raise RuntimeError(f"egg sale {sale.id} belongs to a report without a daily record")
    return {
        "source_service": SOURCE_SERVICE,
        "source_revision": sale.source_revision,
        "transaction_date": sale.transaction_date.isoformat(),
        "customer_id": sale.customer_id,
        "lines": [
            {
                "source_line_id": line.egg_category,
                "egg_category": line.egg_category,
                "quantity_pieces": line.quantity_pieces,
                "weight_kg": decimal_string(line.weight_kg),
                "price_per_kg": decimal_string(line.price_per_kg, 2),
            }
            for line in sorted(sale.lines, key=lambda item: EGG_CATEGORIES.index(item.egg_category))
        ],
        "origin": {
            "abk_report_id": report.id,
            "daily_record_id": report.daily_record_id,
            "submitted_by_employee_id": report.employee_id,
            "approved_by_user_id": sale.approved_by,
            "approved_at": sale.approved_at.isoformat(),
        },
    }


def backoff(attempts: int) -> timedelta:
    """1, 2, 4, 8, ... minutes, at most MAX_BACKOFF."""
    return min(timedelta(minutes=2 ** min(max(attempts - 1, 0), 10)), MAX_BACKOFF)


def deliver_next(db: Session) -> bool:
    """Delivers the oldest due PENDING sale in one transaction; False when none is due.

    The row stays locked during the call, so concurrent workers never send the same sale twice
    at once (they skip it), and a resend waits for the attempt to finish."""
    now = now_utc()
    sale = db.scalar(
        select(AbkEggSale)
        .where(
            AbkEggSale.finance_status == "PENDING",
            or_(AbkEggSale.next_attempt_at.is_(None), AbkEggSale.next_attempt_at <= now),
        )
        .order_by(AbkEggSale.id)
        .limit(1)
        .with_for_update(skip_locked=True)
        .options(selectinload(AbkEggSale.lines), selectinload(AbkEggSale.report))
    )
    if sale is None:
        db.commit()
        return False
    sale.attempts += 1
    sale.last_attempt_at = now
    try:
        revenue_id = finance_client.put_egg_sale_revenue(source_id(sale), finance_payload(sale))
    except finance_client.FinanceUnavailable as exc:
        sale.last_error = str(exc)[:2000]
        sale.next_attempt_at = now_utc() + backoff(sale.attempts)
        logger.warning("egg sale %s not delivered (attempt %s), retrying later: %s", sale.id, sale.attempts, exc)
    except finance_client.FinanceRejected as exc:
        sale.finance_status = "FAILED"
        sale.last_error = str(exc)[:2000] + (f" {exc.errors}" if exc.errors else "")
        sale.next_attempt_at = None
        logger.error("egg sale %s rejected by finance-service: %s", sale.id, exc)
    else:
        sale.finance_status = "SENT"
        sale.finance_revenue_id = revenue_id
        sale.sent_at = now_utc()
        sale.last_error = None
        sale.next_attempt_at = None
        audit(db, sale, "abk_egg_sale_delivered", None, reason=f"finance revenue {revenue_id}")
    db.commit()
    return True


def deliver_pending(*, session: Session | None = None, max_sales: int | None = DELIVERIES_PER_RUN) -> int:
    """Delivers due PENDING sales until none is due (or ``max_sales`` were tried)."""
    tried = 0
    owned = session is None
    db = session or SessionLocal()
    try:
        while max_sales is None or tried < max_sales:
            if not deliver_next(db):
                break
            tried += 1
    finally:
        if owned:
            db.close()
    return tried


def deliver_in_background() -> None:
    """``BackgroundTasks`` entry point. Never raises: undelivered sales stay PENDING for the worker."""
    try:
        deliver_pending()
    except Exception:
        logger.exception("delivering egg sales to finance-service failed; they stay queued")


# --- Endpoints ------------------------------------------------------------------------------


def sale_query() -> Select[tuple[AbkEggSale]]:
    return select(AbkEggSale).options(
        selectinload(AbkEggSale.lines),
        selectinload(AbkEggSale.approver),
        selectinload(AbkEggSale.report).selectinload(AbkDailyReport.employee),
    )


def sale_out(sale: AbkEggSale) -> dict[str, object]:
    return {
        "id": sale.id,
        "report_id": sale.report_id,
        "transaction_date": sale.transaction_date.isoformat(),
        "customer": {"id": sale.customer_id, "name": sale.customer_name},
        "lines": [
            {
                "egg_category": line.egg_category,
                "quantity_pieces": line.quantity_pieces,
                "weight_kg": decimal_string(line.weight_kg),
                "price_per_kg": decimal_string(line.price_per_kg, 2),
            }
            for line in sorted(sale.lines, key=lambda item: EGG_CATEGORIES.index(item.egg_category))
        ],
        "approved_by": {"id": sale.approver.id, "name": sale.approver.name},
        "approved_at": sale.approved_at.isoformat(),
        "finance": {
            "status": sale.finance_status,
            "source_id": source_id(sale),
            "source_revision": sale.source_revision,
            "revenue_id": sale.finance_revenue_id,
            "attempts": sale.attempts,
            "last_attempt_at": sale.last_attempt_at.isoformat() if sale.last_attempt_at else None,
            "next_attempt_at": sale.next_attempt_at.isoformat() if sale.next_attempt_at else None,
            "last_error": sale.last_error,
            "sent_at": sale.sent_at.isoformat() if sale.sent_at else None,
        },
    }


def load_sale(db: Session, sale_id: int) -> AbkEggSale:
    sale = db.scalar(sale_query().where(AbkEggSale.id == sale_id))
    if not sale:
        raise not_found("ABK egg sale")
    return sale


@router.get("/abk-egg-sales")
def list_sales(
    _: ActiveUser,
    db: DbSession,
    page: PageDep,
    finance_status: str | None = Query(default=None, pattern="^(PENDING|SENT|FAILED)$"),
    customer_id: int | None = None,
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
) -> dict[str, object]:
    """Approved egg sales and their delivery to finance-service (``finance_status=FAILED`` lists the
    ones waiting for a correction)."""
    stmt = sale_query()
    if finance_status:
        stmt = stmt.where(AbkEggSale.finance_status == finance_status)
    if customer_id:
        stmt = stmt.where(AbkEggSale.customer_id == customer_id)
    if from_date:
        stmt = stmt.where(AbkEggSale.transaction_date >= from_date)
    if to_date:
        stmt = stmt.where(AbkEggSale.transaction_date <= to_date)
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    rows = db.scalars(
        stmt.order_by(AbkEggSale.transaction_date.desc(), AbkEggSale.id.desc()).offset(page.offset).limit(page.per_page)
    ).all()
    return {"data": [sale_out(row) for row in rows], "meta": page.meta(total)}


@router.get("/abk-egg-sales/{sale_id}")
def get_sale(sale_id: int, _: ActiveUser, db: DbSession) -> dict[str, object]:
    return {"data": sale_out(load_sale(db, sale_id))}


@router.post("/abk-egg-sales/{sale_id}/resend")
def resend_sale(
    sale_id: int,
    actor: Superadmin,
    db: DbSession,
    background_tasks: BackgroundTasks,
    payload: Annotated[EggSaleInput | None, Body()] = None,
) -> dict[str, object]:
    """Sends a sale that finance-service has not confirmed yet again, now. With a body, first
    replaces its customer and lines (a new revision), e.g. after finance-service rejected it."""
    customer = finance_client.active_customer(payload.customer_id, "customer_id") if payload else None
    sale = db.scalar(
        sale_query()
        .where(AbkEggSale.id == sale_id)
        .with_for_update(of=AbkEggSale)
        .execution_options(populate_existing=True)
    )
    if not sale:
        raise not_found("ABK egg sale")
    if sale.finance_status == "SENT":
        raise conflict("EGG_SALE_ALREADY_SENT", "finance-service already recorded this sale; correct it there.")
    if customer and db.scalar(
        select(AbkEggSale.id).where(
            AbkEggSale.report_id == sale.report_id, AbkEggSale.customer_id == customer.id, AbkEggSale.id != sale.id
        )
    ):
        raise conflict("EGG_SALE_CUSTOMER_EXISTS", "This report already has a sale to this customer.")
    before = sale_snapshot(sale)
    if payload and customer:
        sale.customer_id, sale.customer_name = customer.id, customer.name
        set_lines(db, sale, list(payload.lines))
        sale.source_revision += 1
    sale.finance_status = "PENDING"
    sale.next_attempt_at = None
    audit(db, sale, "abk_egg_sale_resent", actor, before=before, after=sale_snapshot(sale))
    db.commit()
    background_tasks.add_task(deliver_in_background)
    return {"data": sale_out(load_sale(db, sale.id))}
