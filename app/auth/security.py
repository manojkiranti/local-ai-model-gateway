"""Password hashing (bcrypt) and JWT issue/verify (PyJWT).

Swapping bcrypt for argon2 later means changing only this module.
"""

from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from ..config import get_settings

_settings = get_settings()

# bcrypt operates on at most 72 bytes; longer inputs are truncated (its
# documented behaviour). Registration also caps password length.
_BCRYPT_MAX_BYTES = 72


def hash_password(password: str) -> str:
    pw = password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    return bcrypt.hashpw(pw, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        pw = password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
        return bcrypt.checkpw(pw, password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_access_token(*, subject: str, role: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=_settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, _settings.jwt_secret, algorithm=_settings.jwt_algorithm)


def decode_token(token: str) -> dict:
    """Decode + validate a JWT. Raises jwt.PyJWTError on any problem."""
    return jwt.decode(
        token, _settings.jwt_secret, algorithms=[_settings.jwt_algorithm]
    )
