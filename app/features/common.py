from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import AuditEvent, User


def now_utc() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)


def today_local() -> date:
    return datetime.now(settings.timezone).date()


def json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    return value


def model_snapshot(model: Any) -> dict[str, Any]:
    return {column.name: json_value(getattr(model, column.name)) for column in model.__table__.columns}


def audit(
    db: Session,
    entity: Any,
    action: str,
    actor: User | None,
    *,
    before: dict[str, Any] | None = None,
    reason: str | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    db.flush()
    db.add(
        AuditEvent(
            actor_id=actor.id if actor else None,
            entity_type=entity.__tablename__,
            entity_id=entity.id,
            action=action,
            happened_at=now_utc(),
            reason=reason,
            before=json_value(before),
            after=json_value(after or model_snapshot(entity)),
        )
    )


def actor_name(db: Session, user_id: int | None) -> str | None:
    return db.scalar(select(User.name).where(User.id == user_id)) if user_id else None
