"""Unit tests for the auth primitives (no DB, no network).

The full register->login->me flow is proven end-to-end via curl in the README;
these cover the security-critical pure functions.
"""

from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.auth import security
from app.config import get_settings


def test_password_hash_roundtrip():
    h = security.hash_password("correct horse battery staple")
    assert h != "correct horse battery staple"  # not stored in plaintext
    assert security.verify_password("correct horse battery staple", h) is True
    assert security.verify_password("wrong password", h) is False


def test_verify_password_handles_garbage_hash():
    assert security.verify_password("anything", "not-a-bcrypt-hash") is False


def test_access_token_roundtrip():
    token = security.create_access_token(subject="a@b.com", role="admin")
    payload = security.decode_token(token)
    assert payload["sub"] == "a@b.com"
    assert payload["role"] == "admin"
    assert "exp" in payload and "iat" in payload


def test_expired_token_is_rejected():
    s = get_settings()
    expired = jwt.encode(
        {
            "sub": "a@b.com",
            "role": "member",
            "iat": datetime.now(timezone.utc) - timedelta(hours=2),
            "exp": datetime.now(timezone.utc) - timedelta(hours=1),
        },
        s.jwt_secret,
        algorithm=s.jwt_algorithm,
    )
    with pytest.raises(jwt.ExpiredSignatureError):
        security.decode_token(expired)


def test_token_signed_with_other_secret_is_rejected():
    s = get_settings()
    forged = jwt.encode({"sub": "a@b.com"}, "not-the-secret", algorithm=s.jwt_algorithm)
    with pytest.raises(jwt.InvalidTokenError):
        security.decode_token(forged)
