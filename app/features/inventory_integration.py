import hashlib
import json
from datetime import date
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, Path
from pydantic import Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.decimal import decimal_string, quantity
from app.core.dependencies import DbSession, require_internal_api_key
from app.core.errors import conflict, validation
from app.core.pagination import StrictModel
from app.db.models import ExternalInventoryLine, ExternalInventorySource, FeedType
from app.features.common import audit
from app.features.farm_service import lock_balances

router = APIRouter(
    prefix="/internal/inventory",
    tags=["Internal Inventory"],
    dependencies=[Depends(require_internal_api_key)],
)


class SourceBase(StrictModel):
    source_service: str = Field(min_length=1, max_length=100)
    source_revision: int = Field(gt=0)
    status: Literal["ACTIVE", "VOIDED"]


class FeedReceiptLine(StrictModel):
    source_line_id: str = Field(min_length=1, max_length=255)
    feed_type_code: str = Field(min_length=1, max_length=60)
    quantity_kg: Decimal = Field(ge=0)
    received_date: date | None = None


class FeedReceiptInput(SourceBase):
    received_date: date | None = None
    lines: list[FeedReceiptLine] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def valid_lines(self) -> FeedReceiptInput:
        _validate_lines(self.status, [line.source_line_id for line in self.lines])
        if self.status == "ACTIVE" and any(line.received_date is None and self.received_date is None for line in self.lines):
            raise ValueError("every active feed line requires received_date")
        return self


class EggSaleLine(StrictModel):
    source_line_id: str = Field(min_length=1, max_length=255)
    egg_category: Literal["UTUH", "PECAH", "REJECT"]
    weight_kg: Decimal = Field(ge=0)
    quantity_pieces: int = Field(ge=0)


class EggSaleInput(SourceBase):
    transaction_date: date
    lines: list[EggSaleLine] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def valid_lines(self) -> EggSaleInput:
        _validate_lines(self.status, [line.source_line_id for line in self.lines])
        categories = [line.egg_category for line in self.lines]
        if len(categories) != len(set(categories)):
            raise ValueError("egg categories cannot be duplicated")
        return self


def _validate_lines(status: str, line_ids: list[str]) -> None:
    if status == "ACTIVE" and not line_ids:
        raise ValueError("ACTIVE sources require at least one line")
    if len(line_ids) != len(set(line_ids)):
        raise ValueError("source_line_id values must be unique")


def _payload_hash(payload: SourceBase) -> str:
    def canonical(value: object) -> object:
        if isinstance(value, Decimal):
            return decimal_string(quantity(value))
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, dict):
            return {key: canonical(item) for key, item in value.items()}
        if isinstance(value, list):
            return [canonical(item) for item in value]
        return value

    encoded = json.dumps(canonical(payload.model_dump()), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _source_snapshot(db: DbSession, source: ExternalInventorySource) -> dict[str, object]:
    lines: list[dict[str, object]] = []
    for line in sorted(source.lines, key=lambda item: item.source_line_id):
        feed = db.get(FeedType, line.feed_type_id) if line.feed_type_id else None
        lines.append(
            {
                "source_line_id": line.source_line_id,
                "feed_type_code": feed.code if feed else None,
                "egg_category": line.egg_category,
                "weight_kg" if line.item_kind == "EGG" else "quantity_kg": decimal_string(line.quantity_kg),
                "quantity_pieces": line.quantity_pieces,
                "effective_date": line.effective_date.isoformat(),
            }
        )
    return {
        "id": source.id,
        "source_service": source.source_service,
        "source_type": source.source_type,
        "source_id": source.source_id,
        "source_revision": source.source_revision,
        "status": source.status,
        "effective_date": source.effective_date.isoformat(),
        "created_at": source.created_at.isoformat(),
        "updated_at": source.updated_at.isoformat(),
        "lines": lines,
    }


def _load_source(
    db: DbSession, source_service: str, source_type: str, source_id: str
) -> ExternalInventorySource | None:
    return db.scalar(
        select(ExternalInventorySource)
        .where(
            ExternalInventorySource.source_service == source_service,
            ExternalInventorySource.source_type == source_type,
            ExternalInventorySource.source_id == source_id,
        )
        .options(selectinload(ExternalInventorySource.lines))
        .with_for_update()
    )


def _prepare_source(
    db: DbSession,
    *,
    source_id: str,
    source_type: str,
    payload: SourceBase,
    effective_date: date,
) -> tuple[ExternalInventorySource, dict[str, object] | None, bool]:
    lock_balances(db)
    digest = _payload_hash(payload)
    source = _load_source(db, payload.source_service, source_type, source_id)
    if source:
        if payload.source_revision < source.source_revision:
            raise conflict(
                "STALE_SOURCE_REVISION",
                "The source revision is older than the stored revision.",
                current_revision=source.source_revision,
            )
        if payload.source_revision == source.source_revision:
            if digest != source.payload_hash:
                raise conflict(
                    "SOURCE_REVISION_CONFLICT",
                    "The source revision already exists with different content.",
                    current_revision=source.source_revision,
                )
            return source, None, True
        before = _source_snapshot(db, source)
        source.lines.clear()
    else:
        before = None
        source = ExternalInventorySource(
            source_service=payload.source_service,
            source_type=source_type,
            source_id=source_id,
        )
        db.add(source)
    source.source_revision = payload.source_revision
    source.status = payload.status
    source.effective_date = effective_date
    source.payload_hash = digest
    db.flush()
    return source, before, False


def _finish(
    db: DbSession, source: ExternalInventorySource, before: dict[str, object] | None
) -> dict[str, object]:
    db.flush()
    db.refresh(source, attribute_names=["lines"])
    after = _source_snapshot(db, source)
    action = "external_inventory_source_created" if before is None else "external_inventory_source_updated"
    if source.status == "VOIDED":
        action = "external_inventory_source_voided"
    audit(db, source, action, None, before=before, after=after, reason=source.source_service)
    db.commit()
    return {"data": _source_snapshot(db, source)}


@router.put("/feed-receipts/{source_id}")
def upsert_feed_receipt(
    payload: FeedReceiptInput,
    db: DbSession,
    source_id: str = Path(min_length=1, max_length=255),
) -> dict[str, object]:
    dates = [item.received_date or payload.received_date for item in payload.lines]
    source_date = min((item for item in dates if item is not None), default=payload.received_date or date.today())
    source, before, replay = _prepare_source(
        db,
        source_id=source_id,
        source_type="FEED_RECEIPT",
        payload=payload,
        effective_date=source_date,
    )
    if replay:
        return {"data": _source_snapshot(db, source)}
    for index, item in enumerate(payload.lines):
        feed = db.scalar(select(FeedType).where(FeedType.code == item.feed_type_code))
        if not feed:
            raise validation(f"lines.{index}.feed_type_code", "Feed type code does not exist.")
        db.add(
            ExternalInventoryLine(
                source_id=source.id,
                source_line_id=item.source_line_id,
                item_kind="FEED",
                feed_type_id=feed.id,
                egg_category=None,
                quantity_kg=quantity(item.quantity_kg),
                quantity_pieces=None,
                effective_date=item.received_date or payload.received_date,
            )
        )
    return _finish(db, source, before)


@router.put("/egg-sales/{source_id}")
def upsert_egg_sale(
    payload: EggSaleInput,
    db: DbSession,
    source_id: str = Path(min_length=1, max_length=255),
) -> dict[str, object]:
    source, before, replay = _prepare_source(
        db,
        source_id=source_id,
        source_type="EGG_SALE",
        payload=payload,
        effective_date=payload.transaction_date,
    )
    if replay:
        return {"data": _source_snapshot(db, source)}
    for item in payload.lines:
        db.add(
            ExternalInventoryLine(
                source_id=source.id,
                source_line_id=item.source_line_id,
                item_kind="EGG",
                feed_type_id=None,
                egg_category=item.egg_category,
                quantity_kg=quantity(item.weight_kg),
                quantity_pieces=item.quantity_pieces,
                effective_date=payload.transaction_date,
            )
        )
    return _finish(db, source, before)
