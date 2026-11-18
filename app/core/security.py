import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import cast

import jwt
from jwt import InvalidTokenError
from pwdlib import PasswordHash

from app.core.config import settings
from app.core.errors import AppError

password_hash = PasswordHash.recommended()
DUMMY_HASH = password_hash.hash("not-a-real-password")


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, encoded: str | None) -> bool:
    return password_hash.verify(password, encoded or DUMMY_HASH)


def validate_password(password: str) -> None:
    if len(password) < 12 or not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        raise AppError(
            422,
            "VALIDATION_ERROR",
            "The request is invalid.",
            errors={"password": ["Password must be at least 12 characters and contain letters and numbers."]},
        )


def create_access_token(user_id: int, token_version: int) -> tuple[str, datetime]:
    now = datetime.now(UTC)
    expires = now + timedelta(hours=settings.jwt_ttl_hours)
    payload = {
        "sub": str(user_id),
        "jti": secrets.token_urlsafe(24),
        "iat": now,
        "exp": expires,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "token_version": token_version,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256"), expires


def decode_access_token(token: str) -> dict[str, object]:
    try:
        return cast(
            dict[str, object],
            jwt.decode(
                token,
                settings.jwt_secret,
                algorithms=["HS256"],
                issuer=settings.jwt_issuer,
                audience=settings.jwt_audience,
                options={"require": ["sub", "jti", "iat", "exp", "iss", "aud", "token_version"]},
            ),
        )
    except InvalidTokenError as exc:
        raise AppError(401, "INVALID_TOKEN", "Authentication credentials are invalid.") from exc


def valid_internal_api_key(provided: str | None) -> bool:
    if not provided:
        return False
    keys = [key for key in (settings.internal_api_key, settings.internal_api_previous_key) if key]
    return any(hmac.compare_digest(provided, key) for key in keys)


def login_attempt_key(email: str, ip_address: str) -> str:
    return hashlib.sha256(f"{email.lower()}|{ip_address}".encode()).hexdigest()


def temporary_password(length: int = 18) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%"
    while True:
        value = "".join(secrets.choice(alphabet) for _ in range(length))
        if any(c.isalpha() for c in value) and any(c.isdigit() for c in value):
            return value


ABK_PIN_LENGTH = 6


def generate_pin() -> str:
    """Random numeric PIN for an ABK account, issued by an Admin and shown once."""
    return "".join(secrets.choice("0123456789") for _ in range(ABK_PIN_LENGTH))


def hash_pin(pin: str) -> str:
    return password_hash.hash(pin)


def verify_pin(pin: str, encoded: str | None) -> bool:
    """Constant work whether or not the account exists (unknown usernames verify a dummy hash)."""
    return password_hash.verify(pin, encoded or DUMMY_HASH)
