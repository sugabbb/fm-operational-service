from typing import Annotated

from fastapi import Depends, Header
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.errors import AppError, forbidden
from app.core.security import decode_access_token, valid_internal_api_key
from app.db.models import User
from app.db.session import get_db

bearer_scheme = HTTPBearer(auto_error=False, scheme_name="BearerAuth")
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
