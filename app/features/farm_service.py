import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session, selectinload

from app.core.decimal import quantity
from app.core.errors import conflict, not_found, validation
from app.db.models import (
    DailyRecord,
    EggHarvest,
    FeedType,
    FeedUsage,
    LivestockGroup,
    PopulationAdjustment,
    User,
)
from app.features.common import audit, model_snapshot, today_local
from app.features.lele_biomass_queue import enqueue as enqueue_biomass

ADVISORY_LOCK = 20261009
EGG_CATEGORIES = ("UTUH", "PECAH", "REJECT")


def lock_balances(db: Session) -> None:
    db.execute(text("select pg_advisory_xact_lock(:key)"), {"key": ADVISORY_LOCK})


def population(db: Session, group: LivestockGroup, as_of: date | None = None) -> int:
    as_of = as_of or today_local()
    if as_of < group.start_date:
        return 0
    mortality = (
        db.scalar(
            select(func.coalesce(func.sum(DailyRecord.mortality), 0)).where(
                DailyRecord.group_id == group.id, DailyRecord.record_date <= as_of
            )
        )
        or 0
    )
    adjustments = (
        db.scalar(
            select(func.coalesce(func.sum(PopulationAdjustment.delta_ekor), 0)).where(
                PopulationAdjustment.group_id == group.id, PopulationAdjustment.effective_date <= as_of
            )
        )
        or 0
    )
    return int(group.initial_population) - int(mortality) + int(adjustments)


def assert_population_nonnegative(db: Session, group: LivestockGroup, from_date: date) -> None:
    dates = {from_date, today_local()}
    dates.update(
        db.scalars(
            select(DailyRecord.record_date).where(
                DailyRecord.group_id == group.id, DailyRecord.record_date >= from_date
            )
        ).all()
    )
    dates.update(
        db.scalars(
            select(PopulationAdjustment.effective_date).where(
                PopulationAdjustment.group_id == group.id, PopulationAdjustment.effective_date >= from_date
            )
        ).all()
    )
    for check_date in sorted(dates):
        if population(db, group, check_date) < 0:
            raise validation("population", f"Population becomes negative on {check_date.isoformat()}.")


def normalize_code(value: str) -> str:
    plain = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().upper()
    return re.sub(r"[^A-Z0-9]+", "-", plain).strip("-")[:60]


def available_code(db: Session, base: str) -> str:
    base = base or "GROUP"
    candidate, suffix = base, 2
    while db.scalar(select(LivestockGroup.id).where(LivestockGroup.code == candidate)):
        tail = f"-{suffix}"
        suffix += 1
        candidate = base[: 60 - len(tail)] + tail
    return candidate


@dataclass(frozen=True)
class DailyFeedLine:
    feed_type_id: int
    morning_kg: Decimal
    noon_kg: Decimal
    evening_kg: Decimal

    @property
    def quantity_kg(self) -> Decimal:
        return self.morning_kg + self.noon_kg + self.evening_kg


@dataclass(frozen=True)
class DailyEggLine:
    egg_category: str
    weight_kg: Decimal
    quantity_pieces: int


def validate_daily_lines(
    db: Session, species: str, data: dict[str, Any]
) -> tuple[list[DailyFeedLine], list[DailyEggLine]]:
    """Validates the feed and egg lines of one daily record and returns them normalized.

    Shared by admin daily records and ABK daily reports so both follow the same rules."""
    seen: set[int] = set()
    feeds = data.get("feeds") or []
    if not feeds:
        raise validation("feeds", "At least one feed type is required.")
    feed_lines: list[DailyFeedLine] = []
    for index, item in enumerate(feeds):
        feed_id = int(item["feed_type_id"])
        if feed_id in seen:
            raise validation(f"feeds.{index}.feed_type_id", "Feed types cannot be duplicated.")
        seen.add(feed_id)
        feed = db.get(FeedType, feed_id)
        if not feed or feed.species != species:
            raise validation(f"feeds.{index}.feed_type_id", "Feed type does not match the livestock species.")
        morning = quantity(item.get("morning_kg", 0))
        noon = quantity(item.get("noon_kg", 0))
        evening = quantity(item.get("evening_kg", 0))
        if species == "AYAM" and evening != 0:
            raise validation(f"feeds.{index}.evening_kg", "Ayam has only morning and noon feed periods.")
        feed_lines.append(DailyFeedLine(feed_id, morning, noon, evening))
    egg_lines: list[DailyEggLine] = []
    if species == "AYAM":
        eggs = data.get("eggs") or {}
        for category in EGG_CATEGORIES:
            egg = eggs.get(category)
            if egg is None:
                raise validation(f"eggs.{category}", "All egg categories are required for Ayam.")
            egg_lines.append(DailyEggLine(category, quantity(egg["weight_kg"]), int(egg["quantity_pieces"])))
    elif data.get("eggs"):
        raise validation("eggs", "Egg harvests are not valid for Lele.")
    return feed_lines, egg_lines


def replace_daily_children(db: Session, record: DailyRecord, species: str, data: dict[str, Any]) -> None:
    feed_lines, egg_lines = validate_daily_lines(db, species, data)
    db.execute(delete(FeedUsage).where(FeedUsage.daily_record_id == record.id))
    db.execute(delete(EggHarvest).where(EggHarvest.daily_record_id == record.id))
    for line in feed_lines:
        db.add(
            FeedUsage(
                daily_record_id=record.id,
                feed_type_id=line.feed_type_id,
                morning_kg=line.morning_kg,
                noon_kg=line.noon_kg,
                evening_kg=line.evening_kg,
                quantity_kg=line.quantity_kg,
            )
        )
    for egg in egg_lines:
        db.add(
            EggHarvest(
                daily_record_id=record.id,
                egg_category=egg.egg_category,
                weight_kg=egg.weight_kg,
                quantity_pieces=egg.quantity_pieces,
            )
        )


def save_daily(db: Session, species: str, rows: list[dict[str, Any]], actor: User) -> list[DailyRecord]:
    seen: set[tuple[int, date]] = set()
    for index, data in enumerate(rows):
        key = (data["group_id"], data["record_date"])
        if key in seen:
            raise validation(f"rows.{index}.record_date", "The same group and date can appear only once.")
        seen.add(key)
    lock_balances(db)
    created: list[DailyRecord] = []
    checks: dict[int, tuple[LivestockGroup, date]] = {}
    for index, data in enumerate(rows):
        group = db.scalar(select(LivestockGroup).where(LivestockGroup.id == data["group_id"]).with_for_update())
        if not group or group.species != species or group.status != "ACTIVE":
            raise validation(f"rows.{index}.group_id", "Group must be active and match the selected species.")
        if data["record_date"] < group.start_date:
            raise validation(f"rows.{index}.record_date", "Record date cannot be before group start date.")
        if db.scalar(
            select(DailyRecord.id).where(
                DailyRecord.group_id == group.id, DailyRecord.record_date == data["record_date"]
            )
        ):
            raise conflict("DAILY_RECORD_EXISTS", "A daily record already exists for this group and date.")
        record = DailyRecord(
            group_id=group.id,
            record_date=data["record_date"],
            mortality=data["mortality"],
            notes=data.get("notes"),
            created_by=actor.id,
        )
        db.add(record)
        db.flush()
        replace_daily_children(db, record, species, data)
        audit(db, record, "daily_created", actor)
        created.append(record)
        checks[group.id] = (group, min(checks.get(group.id, (group, data["record_date"]))[1], data["record_date"]))
    db.flush()
    for group, from_date in checks.values():
        assert_population_nonnegative(db, group, from_date)
        enqueue_biomass(db, group, "daily_created")  # mortality changes the population/biomass
    return created


def update_daily(db: Session, record_id: int, species: str, data: dict[str, Any], actor: User) -> DailyRecord:
    lock_balances(db)
    record = db.scalar(
        select(DailyRecord)
        .where(DailyRecord.id == record_id)
        .options(selectinload(DailyRecord.group))
        .with_for_update()
    )
    if not record or record.group.species != species:
        raise not_found("Daily record")
    before = model_snapshot(record)
    record.mortality = data["mortality"]
    record.notes = data.get("notes")
    replace_daily_children(db, record, species, data)
    db.flush()
    assert_population_nonnegative(db, record.group, record.record_date)
    enqueue_biomass(db, record.group, "daily_updated")
    audit(db, record, "daily_updated", actor, before=before)
    return record
