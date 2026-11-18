from datetime import timedelta

from fastapi import APIRouter, Request, status
from pydantic import EmailStr, Field
from sqlalchemy import delete, func, select

from app.core.dependencies import CurrentUser, DbSession
from app.core.errors import AppError
from app.core.pagination import StrictModel
from app.core.security import (
    create_access_token,
    hash_password,
    login_attempt_key,
    validate_password,
    verify_password,
)
from app.db.models import AuthLoginAttempt, User
from app.features.common import audit, now_utc

router = APIRouter(prefix="/auth", tags=["Authentication"])


class LoginRequest(StrictModel):
    email: EmailStr
    password: str


class PasswordChangeRequest(StrictModel):
    current_password: str
    password: str = Field(min_length=12)
    password_confirmation: str


def user_data(user: User) -> dict[str, object]:
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "role": user.role,
        "is_active": user.is_active,
        "must_change_password": user.must_change_password,
    }


@router.post("/login")
def login(payload: LoginRequest, request: Request, db: DbSession) -> dict[str, object]:
    now = now_utc()
    key = login_attempt_key(str(payload.email), request.client.host if request.client else "unknown")
    attempt = db.get(AuthLoginAttempt, key)
    if attempt and attempt.locked_until and attempt.locked_until > now:
        raise AppError(429, "LOGIN_THROTTLED", "Too many login attempts. Try again later.")
    user = db.scalar(select(User).where(func.lower(User.email) == str(payload.email).lower()))
    valid = verify_password(payload.password, user.password_hash if user else None)
    if not user or not user.is_active or not valid:
        if not attempt:
            attempt = AuthLoginAttempt(key_hash=key, attempts=0, last_attempt_at=now)
            db.add(attempt)
        inside_window = attempt.last_attempt_at >= now - timedelta(minutes=1)
        attempt.attempts = (attempt.attempts if inside_window else 0) + 1
        attempt.last_attempt_at = now
        attempt.locked_until = now + timedelta(minutes=1) if attempt.attempts >= 5 else None
        db.commit()
        raise AppError(401, "INVALID_CREDENTIALS", "Authentication credentials are invalid.")
    db.execute(delete(AuthLoginAttempt).where(AuthLoginAttempt.key_hash == key))
    audit(db, user, "login", user)
    db.commit()
    token, expires = create_access_token(user.id, user.token_version)
    return {
        "data": {
            "access_token": token,
            "token_type": "bearer",
            "expires_at": expires.isoformat(),
            "user": user_data(user),
        }
    }


@router.get("/me")
def me(user: CurrentUser) -> dict[str, object]:
    return {"data": user_data(user)}


@router.post("/change-password")
def change_password(payload: PasswordChangeRequest, user: CurrentUser, db: DbSession) -> dict[str, object]:
    if not verify_password(payload.current_password, user.password_hash):
        raise AppError(
            422,
            "VALIDATION_ERROR",
            "The request is invalid.",
            errors={"current_password": ["Current password is incorrect."]},
        )
    if payload.password != payload.password_confirmation:
        raise AppError(
            422,
            "VALIDATION_ERROR",
            "The request is invalid.",
            errors={"password_confirmation": ["Passwords do not match."]},
        )
    validate_password(payload.password)
    before = {"must_change_password": user.must_change_password, "token_version": user.token_version}
    user.password_hash = hash_password(payload.password)
    user.must_change_password = False
    user.token_version += 1
    audit(db, user, "password_changed", user, before=before)
    db.commit()
    token, expires = create_access_token(user.id, user.token_version)
    return {
        "data": {
            "access_token": token,
            "token_type": "bearer",
            "expires_at": expires.isoformat(),
            "user": user_data(user),
        }
    }


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(user: CurrentUser, db: DbSession) -> None:
    before = {"token_version": user.token_version}
    user.token_version += 1
    audit(db, user, "logout", user, before=before)
    db.commit()
