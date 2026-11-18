from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Query, status
from pydantic import Field, model_validator
from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.orm import selectinload

from app.core.decimal import decimal_string, quantity
from app.core.dependencies import ActiveUser, DbSession, Superadmin
from app.core.errors import conflict, not_found, validation
from app.core.pagination import PageDep, StrictModel
from app.db.models import (
    DailyRecord,
    EggHarvest,
    ExternalInventoryLine,
    ExternalInventorySource,
    FeedType,
    FeedUsage,
    InventoryAdjustment,
    InventoryOpeningBalance,
    LeleSampling,
    LeleSamplingMeasurement,
    LivestockGroup,
    PopulationAdjustment,
)
from app.features.common import audit, model_snapshot, today_local
from app.features.farm_service import (
    assert_population_nonnegative,
    available_code,
    lock_balances,
    normalize_code,
    population,
    save_daily,
    update_daily,
)
from app.features.lele_biomass_jobs import drain_in_background as rebuild_biomass_in_background
from app.features.lele_biomass_queue import enqueue as enqueue_biomass
from app.features.lele_biomass_queue import enqueue_id as enqueue_biomass_id
from app.features.lele_biomass_service import grams, refresh_group_results, result_out

router = APIRouter(tags=["Farm Operations"])


# Weight of one fish at stocking, in grams (Lele only). Used as the start of the first real ADG.
InitialWeightG = Annotated[Decimal, Field(gt=0, le=Decimal("100000"))]


class GroupCreate(StrictModel):
    species: Literal["AYAM", "LELE"]
    code: str | None = Field(default=None, max_length=255)
    name: str = Field(min_length=1, max_length=255)
    start_date: date
    initial_population: int = Field(ge=0)
    initial_weight_g: InitialWeightG | None = None
    location: str = Field(min_length=1, max_length=255)
    notes: str | None = Field(default=None, max_length=3000)


class GroupUpdate(StrictModel):
    name: str = Field(min_length=1, max_length=255)
    start_date: date
    initial_population: int = Field(ge=0)
    # Omitted: keep the stored value (so clients unaware of the field never erase it); null: clear it.
    initial_weight_g: InitialWeightG | None = None
    location: str = Field(min_length=1, max_length=255)
    notes: str | None = Field(default=None, max_length=3000)


class GroupClose(StrictModel):
    closed_at: date
    reason: str | None = Field(default=None, max_length=3000)


class PopulationAdjustmentInput(StrictModel):
    delta_ekor: int
    effective_date: date
    reason: str = Field(min_length=1, max_length=3000)

    @model_validator(mode="after")
    def nonzero(self) -> PopulationAdjustmentInput:
        if self.delta_ekor == 0:
            raise ValueError("delta_ekor must not be zero")
        return self


class FeedLine(StrictModel):
    feed_type_id: int
    morning_kg: Decimal = Field(ge=0)
    noon_kg: Decimal = Field(ge=0)
    evening_kg: Decimal = Field(default=Decimal("0"), ge=0)


class EggLine(StrictModel):
    weight_kg: Decimal = Field(ge=0)
    quantity_pieces: int = Field(ge=0)


class DailyRow(StrictModel):
    group_id: int
    record_date: date
    mortality: int = Field(ge=0)
    notes: str | None = Field(default=None, max_length=3000)
    feeds: list[FeedLine] = Field(min_length=1, max_length=30)
    eggs: dict[str, EggLine] | None = None


class DailyBulk(StrictModel):
    species: Literal["AYAM", "LELE"]
    rows: list[DailyRow] = Field(min_length=1, max_length=50)


class DailyUpdate(StrictModel):
    species: Literal["AYAM", "LELE"]
    mortality: int = Field(ge=0)
    notes: str | None = Field(default=None, max_length=3000)
    feeds: list[FeedLine] = Field(min_length=1, max_length=30)
    eggs: dict[str, EggLine] | None = None


class SamplingMeasurement(StrictModel):
    sequence: int = Field(ge=1, le=3)
    total_weight_kg: Decimal = Field(gt=0)


class SamplingInput(StrictModel):
    group_id: int
    sampling_date: date
    notes: str | None = Field(default=None, max_length=3000)
    measurements: list[SamplingMeasurement] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def sequences(self) -> SamplingInput:
        if sorted(line.sequence for line in self.measurements) != [1, 2, 3]:
            raise ValueError("measurements must contain sequences 1, 2, and 3")
        return self


class InventoryAdjustmentInput(StrictModel):
    item_kind: Literal["FEED", "EGG"]
    feed_type_id: int | None = None
    egg_category: Literal["UTUH", "PECAH", "REJECT"] | None = None
    delta_kg: Decimal
    delta_pieces: int | None = None
    effective_date: date
    reason: str = Field(min_length=1, max_length=3000)

    @model_validator(mode="after")
    def shape(self) -> InventoryAdjustmentInput:
        if self.item_kind == "FEED" and (
            not self.feed_type_id or self.egg_category is not None or self.delta_pieces is not None
        ):
            raise ValueError("FEED requires feed_type_id and prohibits egg fields")
        if self.item_kind == "EGG" and (
            not self.egg_category or self.feed_type_id is not None or self.delta_pieces is None
        ):
            raise ValueError("EGG requires egg_category and delta_pieces")
        if self.delta_kg == 0 and (self.delta_pieces or 0) == 0:
            raise ValueError("adjustment must not be zero")
        return self


class FeedAdjustmentUpdate(StrictModel):
    feed_type_id: int
    delta_kg: Decimal
    effective_date: date
    reason: str = Field(min_length=1, max_length=3000)


def group_out(db: DbSession, row: LivestockGroup) -> dict[str, object]:
    return {
        "id": row.id,
        "species": row.species,
        "code": row.code,
        "name": row.name,
        "start_date": row.start_date.isoformat(),
        "initial_population": row.initial_population,
        "initial_weight_g": decimal_string(row.initial_weight_g),
        "current_population": population(db, row, row.closed_at),
        "location": row.location,
        "status": row.status,
        "closed_at": row.closed_at.isoformat() if row.closed_at else None,
        "closure_reason": row.closure_reason,
        "notes": row.notes,
    }


def daily_out(row: DailyRecord) -> dict[str, object]:
    return {
        "id": row.id,
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
                "id": line.id,
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
    }


def sampling_out(row: LeleSampling) -> dict[str, object]:
    return {
        "id": row.id,
        "group": {"id": row.group.id, "code": row.group.code, "name": row.group.name, "location": row.group.location},
        "sampling_date": row.sampling_date.isoformat(),
        "notes": row.notes,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat(),
        "measurements": [
            {
                "sequence": line.sequence,
                "fish_count": line.fish_count,
                "total_weight_kg": decimal_string(line.total_weight_kg),
            }
            for line in sorted(row.measurements, key=lambda item: item.sequence)
        ],
        "result": result_out(row.result),
    }


@router.get("/livestock-groups")
def groups(
    species: Literal["AYAM", "LELE"],
    _: ActiveUser,
    db: DbSession,
    page: PageDep,
    q: str | None = None,
    group_status: str | None = Query(default=None, alias="status", pattern="^(ACTIVE|CLOSED)$"),
) -> dict[str, object]:
    stmt = select(LivestockGroup).where(LivestockGroup.species == species)
    if q:
        stmt = stmt.where(or_(LivestockGroup.name.ilike(f"%{q}%"), LivestockGroup.code.ilike(f"%{q}%")))
    if group_status:
        stmt = stmt.where(LivestockGroup.status == group_status)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(LivestockGroup.name, LivestockGroup.id).offset(page.offset).limit(page.per_page)
    ).all()
    return {"data": [group_out(db, row) for row in rows], "meta": page.meta(total)}


@router.get("/livestock-groups/{group_id}")
def group(group_id: int, _: ActiveUser, db: DbSession) -> dict[str, object]:
    row = db.get(LivestockGroup, group_id)
    if not row:
        raise not_found("Livestock group")
    return {"data": group_out(db, row)}


@router.post("/livestock-groups", status_code=status.HTTP_201_CREATED)
def create_group(
    payload: GroupCreate, actor: ActiveUser, db: DbSession, background_tasks: BackgroundTasks
) -> dict[str, object]:
    values = payload.model_dump()
    values["initial_weight_g"] = initial_weight(payload.species, payload.initial_weight_g)
    submitted = normalize_code(values.pop("code") or "")
    base = normalize_code(
        f"{payload.location}-{payload.name}" if payload.species == "LELE" else f"{payload.name}-{payload.location}"
    )
    if submitted and submitted != base:
        if db.scalar(select(LivestockGroup.id).where(LivestockGroup.code == submitted)):
            raise conflict("GROUP_CODE_EXISTS", "Livestock group code already exists.")
        code = submitted
    else:
        code = available_code(db, base)
    row = LivestockGroup(**values, code=code, status="ACTIVE")
    db.add(row)
    db.flush()
    audit(db, row, "group_created", actor)
    enqueue_biomass(db, row, "group_created")
    db.commit()
    # The biomass read model is rebuilt after the response is sent (see lele_biomass_jobs).
    background_tasks.add_task(rebuild_biomass_in_background)
    return {"data": group_out(db, row)}


@router.patch("/livestock-groups/{group_id}")
def update_group(
    group_id: int, payload: GroupUpdate, actor: ActiveUser, db: DbSession, background_tasks: BackgroundTasks
) -> dict[str, object]:
    # Same lock order as sampling writes: the initial weight feeds the group's sampling results.
    lock_balances(db)
    row = db.scalar(select(LivestockGroup).where(LivestockGroup.id == group_id).with_for_update())
    if not row:
        raise not_found("Livestock group")
    if payload.start_date != row.start_date or payload.initial_population != row.initial_population:
        raise conflict(
            "IMMUTABLE_GROUP_FIELDS",
            "Start date and initial population cannot be changed; use a population adjustment.",
        )
    before = model_snapshot(row)
    row.name, row.location, row.notes = payload.name, payload.location, payload.notes
    weight_changed = False
    if "initial_weight_g" in payload.model_fields_set:
        weight = initial_weight(row.species, payload.initial_weight_g)
        weight_changed = weight != row.initial_weight_g
        row.initial_weight_g = weight
    if weight_changed:
        db.flush()
        refresh_group_results(db, row.id)
    audit(db, row, "group_updated", actor, before=before)
    enqueue_biomass(db, row, "group_updated")
    db.commit()
    background_tasks.add_task(rebuild_biomass_in_background)
    return {"data": group_out(db, row)}


def initial_weight(species: str, value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    if species != "LELE":
        raise validation("initial_weight_g", "Initial weight is only valid for Lele.")
    return grams(value)


@router.post("/livestock-groups/{group_id}/close")
def close_group(
    group_id: int, payload: GroupClose, actor: ActiveUser, db: DbSession, background_tasks: BackgroundTasks
) -> dict[str, object]:
    row = db.get(LivestockGroup, group_id)
    if not row:
        raise not_found("Livestock group")
    if payload.closed_at < row.start_date:
        raise validation("closed_at", "Closure date cannot be before start date.")
    if row.species == "LELE" and not payload.reason:
        raise validation("reason", "A closure reason is required for Lele.")
    latest = db.scalar(select(func.max(LeleSampling.sampling_date)).where(LeleSampling.group_id == row.id))
    if latest and payload.closed_at < latest:
        raise validation("closed_at", "Closure date cannot be before the latest sampling.")
    before = model_snapshot(row)
    row.status, row.closed_at, row.closure_reason = "CLOSED", payload.closed_at, payload.reason
    audit(db, row, "group_closed", actor, before=before, reason=payload.reason)
    enqueue_biomass(db, row, "group_closed")
    db.commit()
    background_tasks.add_task(rebuild_biomass_in_background)
    return {"data": group_out(db, row)}


@router.post("/livestock-groups/{group_id}/population-adjustments", status_code=status.HTTP_201_CREATED)
def adjust_population(
    group_id: int,
    payload: PopulationAdjustmentInput,
    actor: Superadmin,
    db: DbSession,
    background_tasks: BackgroundTasks,
) -> dict[str, object]:
    lock_balances(db)
    group = db.scalar(select(LivestockGroup).where(LivestockGroup.id == group_id).with_for_update())
    if not group:
        raise not_found("Livestock group")
    if payload.effective_date < group.start_date:
        raise validation("effective_date", "Adjustment date cannot be before group start date.")
    row = PopulationAdjustment(group_id=group.id, created_by=actor.id, **payload.model_dump())
    db.add(row)
    db.flush()
    assert_population_nonnegative(db, group, payload.effective_date)
    audit(db, row, "population_adjusted", actor, reason=payload.reason)
    enqueue_biomass(db, group, "population_adjusted")
    db.commit()
    background_tasks.add_task(rebuild_biomass_in_background)
    return {"data": model_snapshot(row)}


def daily_query() -> Any:
    return select(DailyRecord).options(
        selectinload(DailyRecord.group),
        selectinload(DailyRecord.feeds).selectinload(FeedUsage.feed_type),
        selectinload(DailyRecord.eggs),
    )


@router.get("/daily-records")
def daily_records(
    species: Literal["AYAM", "LELE"],
    _: ActiveUser,
    db: DbSession,
    page: PageDep,
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
    group_id: int | None = None,
) -> dict[str, object]:
    stmt = daily_query().join(DailyRecord.group).where(LivestockGroup.species == species)
    if from_date:
        stmt = stmt.where(DailyRecord.record_date >= from_date)
    if to_date:
        stmt = stmt.where(DailyRecord.record_date <= to_date)
    if group_id:
        stmt = stmt.where(DailyRecord.group_id == group_id)
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    rows = db.scalars(
        stmt.order_by(DailyRecord.record_date.desc(), DailyRecord.id.desc()).offset(page.offset).limit(page.per_page)
    ).all()
    return {"data": [daily_out(row) for row in rows], "meta": page.meta(total)}


@router.get("/daily-records/{record_id}")
def get_daily(record_id: int, _: ActiveUser, db: DbSession) -> dict[str, object]:
    row = db.scalar(daily_query().where(DailyRecord.id == record_id))
    if not row:
        raise not_found("Daily record")
    return {"data": daily_out(row)}


@router.post("/daily-records/bulk", status_code=status.HTTP_201_CREATED)
def create_daily(
    payload: DailyBulk, actor: ActiveUser, db: DbSession, background_tasks: BackgroundTasks
) -> dict[str, object]:
    rows = save_daily(db, payload.species, [item.model_dump() for item in payload.rows], actor)
    db.commit()
    background_tasks.add_task(rebuild_biomass_in_background)  # Lele mortality changes the biomass
    loaded = db.scalars(daily_query().where(DailyRecord.id.in_([row.id for row in rows]))).all()
    return {"data": [daily_out(row) for row in loaded]}


@router.patch("/daily-records/{record_id}")
def patch_daily(
    record_id: int, payload: DailyUpdate, actor: ActiveUser, db: DbSession, background_tasks: BackgroundTasks
) -> dict[str, object]:
    row = update_daily(db, record_id, payload.species, payload.model_dump(exclude={"species"}), actor)
    db.commit()
    background_tasks.add_task(rebuild_biomass_in_background)
    loaded = db.scalar(daily_query().where(DailyRecord.id == row.id))
    if not loaded:
        raise not_found("Daily record")
    return {"data": daily_out(loaded)}


@router.get("/lele-samplings")
def samplings(
    _: ActiveUser,
    db: DbSession,
    page: PageDep,
    group_id: int | None = None,
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
) -> dict[str, object]:
    stmt = select(LeleSampling).options(
        selectinload(LeleSampling.group), selectinload(LeleSampling.measurements), selectinload(LeleSampling.result)
    )
    if group_id:
        stmt = stmt.where(LeleSampling.group_id == group_id)
    if from_date:
        stmt = stmt.where(LeleSampling.sampling_date >= from_date)
    if to_date:
        stmt = stmt.where(LeleSampling.sampling_date <= to_date)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(LeleSampling.sampling_date.desc(), LeleSampling.id.desc())
        .offset(page.offset)
        .limit(page.per_page)
    ).all()
    return {"data": [sampling_out(row) for row in rows], "meta": page.meta(total)}


@router.get("/lele-samplings/{sampling_id}")
def sampling(sampling_id: int, _: ActiveUser, db: DbSession) -> dict[str, object]:
    row = db.scalar(
        select(LeleSampling)
        .where(LeleSampling.id == sampling_id)
        .options(
            selectinload(LeleSampling.group),
            selectinload(LeleSampling.measurements),
            selectinload(LeleSampling.result),
        )
    )
    if not row:
        raise not_found("Lele sampling")
    return {"data": sampling_out(row)}


def save_sampling(
    db: DbSession, payload: SamplingInput, actor: ActiveUser, sampling_id: int | None = None
) -> LeleSampling:
    lock_balances(db)
    row = (
        db.scalar(select(LeleSampling).where(LeleSampling.id == sampling_id).with_for_update()) if sampling_id else None
    )
    if sampling_id and not row:
        raise not_found("Lele sampling")
    group = db.scalar(select(LivestockGroup).where(LivestockGroup.id == payload.group_id).with_for_update())
    if not group or group.species != "LELE":
        raise validation("group_id", "Sampling is valid only for a Lele group.")
    if (row is None or row.group_id != group.id) and group.status != "ACTIVE":
        raise validation("group_id", "Lele group must be active.")
    if payload.sampling_date < group.start_date or (group.closed_at and payload.sampling_date > group.closed_at):
        raise validation("sampling_date", "Sampling date is outside the group lifecycle.")
    if population(db, group, payload.sampling_date) < 30:
        raise validation("group_id", "Population on the sampling date must be at least 30.")
    duplicate = db.scalar(
        select(LeleSampling.id).where(
            LeleSampling.group_id == group.id,
            LeleSampling.sampling_date == payload.sampling_date,
            LeleSampling.id != (sampling_id or 0),
        )
    )
    if duplicate:
        raise conflict("SAMPLING_EXISTS", "A sampling already exists for this group and date.")
    before = model_snapshot(row) if row else None
    previous_group_id = row.group_id if row else None
    row = row or LeleSampling(created_by=actor.id)
    row.group_id, row.sampling_date, row.notes = group.id, payload.sampling_date, payload.notes
    db.add(row)
    db.flush()
    db.execute(delete(LeleSamplingMeasurement).where(LeleSamplingMeasurement.sampling_id == row.id))
    for line in payload.measurements:
        db.add(
            LeleSamplingMeasurement(
                sampling_id=row.id,
                sequence=line.sequence,
                fish_count=10,
                total_weight_kg=quantity(line.total_weight_kg),
            )
        )
    db.flush()
    # Per-fish weight and ADG depend on the neighbouring samplings, so rebuild the whole group
    # (and the group the sampling moved away from, if any).
    refresh_group_results(db, group.id)
    enqueue_biomass_id(db, group.id, "sampling_saved")
    if previous_group_id is not None and previous_group_id != group.id:
        refresh_group_results(db, previous_group_id)
        enqueue_biomass_id(db, previous_group_id, "sampling_moved")
    audit(db, row, "lele_sampling_updated" if before else "lele_sampling_created", actor, before=before)
    return row


@router.post("/lele-samplings", status_code=status.HTTP_201_CREATED)
def create_sampling(
    payload: SamplingInput, actor: ActiveUser, db: DbSession, background_tasks: BackgroundTasks
) -> dict[str, object]:
    row = save_sampling(db, payload, actor)
    db.commit()
    background_tasks.add_task(rebuild_biomass_in_background)
    return sampling(row.id, actor, db)


@router.patch("/lele-samplings/{sampling_id}")
def update_sampling(
    sampling_id: int, payload: SamplingInput, actor: ActiveUser, db: DbSession, background_tasks: BackgroundTasks
) -> dict[str, object]:
    row = save_sampling(db, payload, actor, sampling_id)
    db.commit()
    background_tasks.add_task(rebuild_biomass_in_background)
    return sampling(row.id, actor, db)


@router.post("/inventory-adjustments", status_code=status.HTTP_201_CREATED)
def create_inventory_adjustment(
    payload: InventoryAdjustmentInput, actor: Superadmin, db: DbSession
) -> dict[str, object]:
    lock_balances(db)
    values = payload.model_dump()
    values["delta_kg"] = quantity(values["delta_kg"])
    row = InventoryAdjustment(**values, created_by=actor.id)
    db.add(row)
    db.flush()
    audit(db, row, "inventory_adjusted", actor, reason=row.reason)
    db.commit()
    return {"data": model_snapshot(row)}


@router.patch("/inventory-adjustments/{adjustment_id}")
def update_inventory_adjustment(
    adjustment_id: int, payload: FeedAdjustmentUpdate, actor: Superadmin, db: DbSession
) -> dict[str, object]:
    lock_balances(db)
    row = db.scalar(select(InventoryAdjustment).where(InventoryAdjustment.id == adjustment_id).with_for_update())
    if not row or row.item_kind != "FEED":
        raise not_found("Feed inventory adjustment")
    old_feed, new_feed = db.get(FeedType, row.feed_type_id), db.get(FeedType, payload.feed_type_id)
    if not old_feed or not new_feed:
        raise validation("feed_type_id", "Feed type does not exist.")
    if old_feed.species != new_feed.species:
        raise validation("feed_type_id", "Replacement feed type must have the same species.")
    before = model_snapshot(row)
    row.feed_type_id, row.delta_kg, row.effective_date, row.reason = (
        payload.feed_type_id,
        quantity(payload.delta_kg),
        payload.effective_date,
        payload.reason,
    )
    audit(db, row, "inventory_adjustment_updated", actor, before=before, reason=row.reason)
    db.commit()
    return {"data": model_snapshot(row)}


def feed_balance(db: DbSession, feed_type_id: int, as_of: date) -> Decimal:
    opening = (
        db.scalar(
            select(func.coalesce(func.sum(InventoryOpeningBalance.quantity_kg), 0)).where(
                InventoryOpeningBalance.item_kind == "FEED",
                InventoryOpeningBalance.feed_type_id == feed_type_id,
                InventoryOpeningBalance.effective_date <= as_of,
            )
        )
        or 0
    )
    received = (
        db.scalar(
            select(func.coalesce(func.sum(ExternalInventoryLine.quantity_kg), 0))
            .join(ExternalInventorySource)
            .where(
                ExternalInventoryLine.feed_type_id == feed_type_id,
                ExternalInventorySource.source_type == "FEED_RECEIPT",
                ExternalInventorySource.status == "ACTIVE",
                ExternalInventoryLine.effective_date <= as_of,
            )
        )
        or 0
    )
    used = (
        db.scalar(
            select(func.coalesce(func.sum(FeedUsage.quantity_kg), 0))
            .join(DailyRecord)
            .where(FeedUsage.feed_type_id == feed_type_id, DailyRecord.record_date <= as_of)
        )
        or 0
    )
    adjusted = (
        db.scalar(
            select(func.coalesce(func.sum(InventoryAdjustment.delta_kg), 0)).where(
                InventoryAdjustment.item_kind == "FEED",
                InventoryAdjustment.feed_type_id == feed_type_id,
                InventoryAdjustment.effective_date <= as_of,
            )
        )
        or 0
    )
    return Decimal(opening) + Decimal(received) - Decimal(used) + Decimal(adjusted)


@router.get("/inventory/feed")
def feed_inventory(
    species: Literal["AYAM", "LELE"], _: ActiveUser, db: DbSession, as_of: date | None = None
) -> dict[str, object]:
    as_of = as_of or today_local()
    feeds = db.scalars(select(FeedType).where(FeedType.species == species).order_by(FeedType.name)).all()
    movements = (
        db.execute(
            text("""
        SELECT * FROM (
          SELECT 'OPENING' type, o.id source_id, o.feed_type_id, f.name feed_name, o.effective_date movement_date, o.created_at recorded_at, o.quantity_kg delta, NULL reason
          FROM inventory_opening_balances o JOIN feed_types f ON f.id=o.feed_type_id WHERE o.item_kind='FEED' AND f.species=:species AND o.effective_date<=:as_of AND o.quantity_kg<>0
          UNION ALL
          SELECT 'RECEIPT', l.id, l.feed_type_id, f.name, l.effective_date, s.updated_at, l.quantity_kg, NULL
          FROM external_inventory_lines l JOIN feed_types f ON f.id=l.feed_type_id JOIN external_inventory_sources s ON s.id=l.source_id
          WHERE f.species=:species AND s.source_type='FEED_RECEIPT' AND s.status='ACTIVE' AND l.effective_date<=:as_of AND l.quantity_kg<>0
          UNION ALL
          SELECT 'ADJUSTMENT', a.id, a.feed_type_id, f.name, a.effective_date, a.created_at, a.delta_kg, a.reason
          FROM inventory_adjustments a JOIN feed_types f ON f.id=a.feed_type_id WHERE a.item_kind='FEED' AND f.species=:species AND a.effective_date<=:as_of AND a.delta_kg<>0
          UNION ALL
          SELECT 'USAGE', u.id, u.feed_type_id, f.name, r.record_date, r.created_at, -u.quantity_kg, NULL
          FROM feed_usages u JOIN feed_types f ON f.id=u.feed_type_id JOIN daily_records r ON r.id=u.daily_record_id JOIN livestock_groups g ON g.id=r.group_id
          WHERE g.species=:species AND r.record_date<=:as_of AND u.quantity_kg<>0
        ) m ORDER BY movement_date, recorded_at, source_id
    """),
            {"species": species, "as_of": as_of},
        )
        .mappings()
        .all()
    )
    balances: dict[int, Decimal] = {}
    output = []
    for movement in movements:
        feed_id = int(movement["feed_type_id"])
        balances[feed_id] = balances.get(feed_id, Decimal("0")) + Decimal(movement["delta"])
        output.append(
            {
                "key": f"{str(movement['type']).lower()}-{movement['source_id']}",
                "movement_type": movement["type"],
                "movement_date": movement["movement_date"].isoformat(),
                "recorded_at": movement["recorded_at"].isoformat(),
                "feed_type_id": feed_id,
                "feed_name": movement["feed_name"],
                "quantity_delta": decimal_string(movement["delta"]),
                "balance_after": decimal_string(balances[feed_id]),
                "reason": movement["reason"],
            }
        )
    return {
        "data": {
            "as_of": as_of.isoformat(),
            "stock": [
                {
                    "feed_type_id": feed.id,
                    "name": feed.name,
                    "is_active": feed.is_active,
                    "quantity_kg": decimal_string(feed_balance(db, feed.id, as_of)),
                }
                for feed in feeds
            ],
            "movements": list(reversed(output)),
        }
    }


@router.get("/inventory/eggs")
def egg_inventory(_: ActiveUser, db: DbSession, as_of: date | None = None) -> dict[str, object]:
    as_of = as_of or today_local()
    result = []
    for category in ("UTUH", "PECAH", "REJECT"):
        opening_kg, opening_pieces = db.execute(
            select(
                func.coalesce(func.sum(InventoryOpeningBalance.quantity_kg), 0),
                func.coalesce(func.sum(InventoryOpeningBalance.quantity_pieces), 0),
            ).where(
                InventoryOpeningBalance.item_kind == "EGG",
                InventoryOpeningBalance.egg_category == category,
                InventoryOpeningBalance.effective_date <= as_of,
            )
        ).one()
        harvest_kg, harvest_pieces = db.execute(
            select(
                func.coalesce(func.sum(EggHarvest.weight_kg), 0), func.coalesce(func.sum(EggHarvest.quantity_pieces), 0)
            )
            .join(DailyRecord)
            .where(EggHarvest.egg_category == category, DailyRecord.record_date <= as_of)
        ).one()
        sold_kg, sold_pieces = db.execute(
            select(
                func.coalesce(func.sum(ExternalInventoryLine.quantity_kg), 0),
                func.coalesce(func.sum(ExternalInventoryLine.quantity_pieces), 0),
            )
            .join(ExternalInventorySource)
            .where(
                ExternalInventoryLine.egg_category == category,
                ExternalInventorySource.source_type == "EGG_SALE",
                ExternalInventorySource.status == "ACTIVE",
                ExternalInventoryLine.effective_date <= as_of,
            )
        ).one()
        adjusted_kg, adjusted_pieces = db.execute(
            select(
                func.coalesce(func.sum(InventoryAdjustment.delta_kg), 0),
                func.coalesce(func.sum(InventoryAdjustment.delta_pieces), 0),
            ).where(
                InventoryAdjustment.item_kind == "EGG",
                InventoryAdjustment.egg_category == category,
                InventoryAdjustment.effective_date <= as_of,
            )
        ).one()
        result.append(
            {
                "egg_category": category,
                "weight_kg": decimal_string(
                    Decimal(opening_kg) + Decimal(harvest_kg) - Decimal(sold_kg) + Decimal(adjusted_kg)
                ),
                "quantity_pieces": int(opening_pieces) + int(harvest_pieces) - int(sold_pieces) + int(adjusted_pieces),
            }
        )
    harvests = (
        db.execute(
            select(
                DailyRecord.id,
                DailyRecord.record_date,
                LivestockGroup.id.label("group_id"),
                LivestockGroup.name.label("group_name"),
                EggHarvest.egg_category,
                EggHarvest.weight_kg,
                EggHarvest.quantity_pieces,
            )
            .join(LivestockGroup)
            .join(EggHarvest)
            .where(LivestockGroup.species == "AYAM", DailyRecord.record_date <= as_of)
            .order_by(DailyRecord.record_date.desc(), DailyRecord.id.desc())
        )
        .mappings()
        .all()
    )
    return {
        "data": {
            "as_of": as_of.isoformat(),
            "stock": result,
            "harvests": [
                {
                    **dict(row),
                    "record_date": row["record_date"].isoformat(),
                    "weight_kg": decimal_string(row["weight_kg"]),
                }
                for row in harvests
            ],
        }
    }
