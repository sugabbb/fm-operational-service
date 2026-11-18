from datetime import UTC, datetime

import pytest

from app.core.errors import AppError
from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    valid_internal_api_key,
    verify_password,
)


def test_password_hash_and_jwt_contract() -> None:
    encoded = hash_password("StrongPassword123")
    assert verify_password("StrongPassword123", encoded)
    assert not verify_password("wrong", encoded)
    token, expires = create_access_token(42, 7)
    payload = decode_access_token(token)
    assert payload["sub"] == "42"
    assert payload["token_version"] == 7
    assert expires > datetime.now(UTC)


def test_internal_api_key_and_invalid_token() -> None:
    assert valid_internal_api_key("test-internal-key-with-at-least-24-characters")
    assert not valid_internal_api_key("bad")
    with pytest.raises(AppError) as caught:
        decode_access_token("not-a-jwt")
    assert caught.value.code == "INVALID_TOKEN"
