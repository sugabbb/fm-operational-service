from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, Header
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.errors import AppError, forbidden
from app.core.security import decode_access_token, valid_internal_api_key, verify_pin
from app.db.models import AbkAccount, User
from app.db.session import get_db

bearer_scheme = HTTPBearer(auto_error=False, scheme_name="BearerAuth")
abk_username_scheme = APIKeyHeader(name="X-ABK-Username", auto_error=False, scheme_name="AbkUsername")
abk_pin_scheme = APIKeyHeader(name="X-ABK-PIN", auto_error=False, scheme_name="AbkPin")
ABK_PIN_MAX_ATTEMPTS = 5
ABK_PIN_LOCK = timedelta(minutes=15)
DbSession = Annotated[Session, Depends(get_db)]


def require_internal_api_key(
    x_fm_internal_key: Annotated[str | None, Header(alias="X-FM-Internal-Key")] = None,
) -> None:
    if not valid_internal_api_key(x_fm_internal_key):
        raise AppError(401, "INVALID_INTERNAL_API_KEY", "Authentication credentials are invalid.")


def current_user(
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> User:
    if not credentials or credentials.scheme.lower() != "bearer":
        raise AppError(401, "INVALID_TOKEN", "Authentication credentials are invalid.")
    payload = decode_access_token(credentials.credentials)
    try:
        user_id = int(str(payload["sub"]))
        version = int(str(payload["token_version"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise AppError(401, "INVALID_TOKEN", "Authentication credentials are invalid.") from exc
    user = db.get(User, user_id)
    if not user or not user.is_active or user.token_version != version:
        raise AppError(401, "INVALID_TOKEN", "Authentication credentials are invalid.")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def active_user(user: CurrentUser) -> User:
    if user.must_change_password:
        raise AppError(403, "PASSWORD_CHANGE_REQUIRED", "Password must be changed before using this endpoint.")
    return user


ActiveUser = Annotated[User, Depends(active_user)]


def superadmin(user: ActiveUser) -> User:
    if user.role != "SUPERADMIN":
        raise forbidden()
    return user


Superadmin = Annotated[User, Depends(superadmin)]


def current_abk(
    db: DbSession,
    username: Annotated[str | None, Depends(abk_username_scheme)],
    pin: Annotated[str | None, Depends(abk_pin_scheme)],
) -> AbkAccount:
    """ABK Web App authentication: no login and no token, every request carries the ABK username
    and PIN. Wrong, unknown, and inactive credentials get the same answer. After
    ABK_PIN_MAX_ATTEMPTS consecutive wrong PINs the account is locked for ABK_PIN_LOCK; a correct
    PIN or an Admin PIN reset clears the counter."""
    invalid = AppError(401, "INVALID_ABK_CREDENTIALS", "Username or PIN is invalid.")
    if not username or not pin:
        raise invalid
    now = datetime.now(UTC)
    account = db.scalar(
        select(AbkAccount)
        .where(AbkAccount.username == username.strip().lower())
        .options(selectinload(AbkAccount.employee))
        .with_for_update()
    )
    if account and account.locked_until and account.locked_until > now:
        locked_until = account.locked_until.isoformat()
        db.rollback()
        raise AppError(
            429,
            "ABK_PIN_LOCKED",
            "Too many invalid PIN attempts. Try again later.",
            extra={"locked_until": locked_until},
        )
    valid = verify_pin(pin, account.pin_hash if account else None)
    if account and not valid:
        account.failed_pin_attempts += 1
        if account.failed_pin_attempts >= ABK_PIN_MAX_ATTEMPTS:
            account.failed_pin_attempts = 0
            account.locked_until = now + ABK_PIN_LOCK
        db.commit()
    if not account or not valid or not account.is_active or account.employee.status != "ACTIVE":
        db.rollback()
        raise invalid
    account.failed_pin_attempts = 0
    account.locked_until = None
    account.last_authenticated_at = now
    db.commit()
    return account


CurrentAbk = Annotated[AbkAccount, Depends(current_abk)]
