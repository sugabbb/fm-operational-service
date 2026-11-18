from datetime import date

from fastapi import APIRouter, Query, status
from pydantic import EmailStr, Field
from sqlalchemy import func, select

from app.core.dependencies import ActiveUser, DbSession, Superadmin
from app.core.errors import conflict, not_found
from app.core.pagination import PageDep, StrictModel
from app.core.security import hash_password, temporary_password
from app.db.models import Employee, User
from app.features.common import audit, model_snapshot

router = APIRouter(tags=["People"])


class UserCreate(StrictModel):
    name: str = Field(min_length=1, max_length=255)
    email: EmailStr
    role: str = Field(pattern="^(ADMIN|SUPERADMIN)$")


class UserUpdate(UserCreate):
    pass


class UserStatus(StrictModel):
    is_active: bool


class EmployeeInput(StrictModel):
    name: str = Field(min_length=1, max_length=255)
    job_title: str | None = Field(default=None, max_length=255)
    joined_at: date
    status: str = Field(pattern="^(ACTIVE|INACTIVE)$")
    notes: str | None = Field(default=None, max_length=3000)


def user_out(row: User) -> dict[str, object]:
    return {key: getattr(row, key) for key in ("id", "name", "email", "role", "is_active", "must_change_password")}


def employee_out(row: Employee) -> dict[str, object]:
    return {
        "id": row.id,
        "name": row.name,
        "job_title": row.job_title,
        "joined_at": row.joined_at.isoformat(),
        "status": row.status,
        "notes": row.notes,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


@router.get("/users")
def list_users(_: Superadmin, db: DbSession, page: PageDep) -> dict[str, object]:
    total = db.scalar(select(func.count()).select_from(User)) or 0
    rows = db.scalars(select(User).order_by(User.name, User.id).offset(page.offset).limit(page.per_page)).all()
    return {"data": [user_out(row) for row in rows], "meta": page.meta(total)}


@router.post("/users", status_code=status.HTTP_201_CREATED)
def create_user(payload: UserCreate, actor: Superadmin, db: DbSession) -> dict[str, object]:
    if db.scalar(select(User.id).where(func.lower(User.email) == str(payload.email).lower())):
        raise conflict("EMAIL_ALREADY_EXISTS", "A user with this email already exists.")
    password = temporary_password()
    row = User(**payload.model_dump(), password_hash=hash_password(password), is_active=True, must_change_password=True)
    db.add(row)
    db.flush()
    audit(db, row, "user_created", actor)
    db.commit()
    return {"data": user_out(row) | {"temporary_password": password}}


@router.patch("/users/{user_id}")
def update_user(user_id: int, payload: UserUpdate, actor: Superadmin, db: DbSession) -> dict[str, object]:
    row = db.get(User, user_id)
    if not row:
        raise not_found("User")
    duplicate = db.scalar(
        select(User.id).where(func.lower(User.email) == str(payload.email).lower(), User.id != row.id)
    )
    if duplicate:
        raise conflict("EMAIL_ALREADY_EXISTS", "A user with this email already exists.")
    if row.role == "SUPERADMIN" and payload.role != "SUPERADMIN" and row.is_active:
        count = (
            db.scalar(select(func.count()).select_from(User).where(User.role == "SUPERADMIN", User.is_active.is_(True)))
            or 0
        )
        if count <= 1:
            raise conflict("LAST_SUPERADMIN", "At least one active Superadmin must remain.")
    before = model_snapshot(row)
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    row.token_version += 1
    audit(db, row, "user_updated", actor, before=before)
    db.commit()
    return {"data": user_out(row)}


@router.patch("/users/{user_id}/status")
def update_user_status(user_id: int, payload: UserStatus, actor: Superadmin, db: DbSession) -> dict[str, object]:
    row = db.get(User, user_id)
    if not row:
        raise not_found("User")
    if row.id == actor.id and not payload.is_active:
        raise conflict("SELF_DEACTIVATION", "You cannot deactivate your own account.")
    if row.role == "SUPERADMIN" and row.is_active and not payload.is_active:
        count = (
            db.scalar(select(func.count()).select_from(User).where(User.role == "SUPERADMIN", User.is_active.is_(True)))
            or 0
        )
        if count <= 1:
            raise conflict("LAST_SUPERADMIN", "At least one active Superadmin must remain.")
    before = model_snapshot(row)
    row.is_active = payload.is_active
    row.token_version += 1
    audit(db, row, "active_changed", actor, before=before)
    db.commit()
    return {"data": user_out(row)}


@router.post("/users/{user_id}/reset-password")
def reset_password(user_id: int, actor: Superadmin, db: DbSession) -> dict[str, object]:
    row = db.get(User, user_id)
    if not row:
        raise not_found("User")
    password = temporary_password()
    before = model_snapshot(row)
    row.password_hash = hash_password(password)
    row.must_change_password = True
    row.token_version += 1
    audit(db, row, "password_reset", actor, before=before)
    db.commit()
    return {"data": user_out(row) | {"temporary_password": password}}


@router.get("/employees")
def list_employees(
    _: ActiveUser,
    db: DbSession,
    page: PageDep,
    q: str | None = None,
    employee_status: str | None = Query(default=None, alias="status", pattern="^(ACTIVE|INACTIVE)$"),
    sort: str = Query(default="name", pattern="^(name|job_title|joined_at|status)$"),
    direction: str = Query(default="asc", pattern="^(asc|desc)$"),
) -> dict[str, object]:
    stmt = select(Employee)
    if q:
        stmt = stmt.where(Employee.name.ilike(f"%{q}%"))
    if employee_status:
        stmt = stmt.where(Employee.status == employee_status)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    column = getattr(Employee, sort)
    stmt = (
        stmt.order_by(column.desc() if direction == "desc" else column.asc(), Employee.id)
        .offset(page.offset)
        .limit(page.per_page)
    )
    rows = db.scalars(stmt).all()
    return {"data": [employee_out(row) for row in rows], "meta": page.meta(total)}


@router.get("/employees/{employee_id}")
def get_employee(employee_id: int, _: ActiveUser, db: DbSession) -> dict[str, object]:
    row = db.get(Employee, employee_id)
    if not row:
        raise not_found("Employee")
    return {"data": employee_out(row)}


@router.post("/employees", status_code=status.HTTP_201_CREATED)
def create_employee(payload: EmployeeInput, actor: ActiveUser, db: DbSession) -> dict[str, object]:
    row = Employee(**payload.model_dump())
    db.add(row)
    db.flush()
    audit(db, row, "employee_created", actor)
    db.commit()
    return {"data": employee_out(row)}


@router.patch("/employees/{employee_id}")
def update_employee(employee_id: int, payload: EmployeeInput, actor: ActiveUser, db: DbSession) -> dict[str, object]:
    row = db.get(Employee, employee_id)
    if not row:
        raise not_found("Employee")
    before = model_snapshot(row)
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    audit(db, row, "employee_updated", actor, before=before)
    db.commit()
    return {"data": employee_out(row)}


