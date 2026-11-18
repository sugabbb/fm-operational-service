from fastapi import APIRouter, Depends
from pydantic import Field
from sqlalchemy import select

from app.core.dependencies import DbSession, require_internal_api_key
from app.core.errors import AppError, not_found
from app.core.pagination import StrictModel
from app.db.models import Employee, FeedType, User

router = APIRouter(
    prefix="/internal",
    tags=["Internal References"],
    dependencies=[Depends(require_internal_api_key)],
)


class AuthorizeInput(StrictModel):
    user_id: int
    token_version: int


class FeedResolveInput(StrictModel):
    ids: list[int] = Field(min_length=1, max_length=100)


@router.post("/auth/authorize")
def authorize(payload: AuthorizeInput, db: DbSession) -> dict[str, object]:
    user = db.get(User, payload.user_id)
    if not user or not user.is_active or user.token_version != payload.token_version:
        raise AppError(401, "INVALID_TOKEN", "Authentication credentials are invalid.")
    if user.must_change_password:
        raise AppError(403, "PASSWORD_CHANGE_REQUIRED", "Password must be changed before using this endpoint.")
    return {
        "data": {
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "role": user.role,
            "is_active": user.is_active,
            "must_change_password": user.must_change_password,
            "token_version": user.token_version,
            "updated_at": user.updated_at.isoformat(),
        }
    }


@router.post("/references/feed-types/resolve")
def resolve_feed_types(payload: FeedResolveInput, db: DbSession) -> dict[str, object]:
    ids = list(dict.fromkeys(payload.ids))
    rows = db.scalars(select(FeedType).where(FeedType.id.in_(ids)).order_by(FeedType.id)).all()
    found = {row.id for row in rows}
    return {
        "data": [
            {
                "id": row.id,
                "code": row.code,
                "species": row.species,
                "name": row.name,
                "is_active": row.is_active,
                "updated_at": row.updated_at.isoformat(),
            }
            for row in rows
        ],
        "missing_ids": [item for item in ids if item not in found],
    }


@router.get("/references/employees/{employee_id}")
def employee_reference(employee_id: int, db: DbSession) -> dict[str, object]:
    row = db.get(Employee, employee_id)
    if not row:
        raise not_found("Employee")
    return {
        "data": {
            "id": row.id,
            "name": row.name,
            "job_title": row.job_title,
            "status": row.status,
            "updated_at": row.updated_at.isoformat(),
        }
    }
