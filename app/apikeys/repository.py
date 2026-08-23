"""Data access for API keys. No decisions live here — `policy.py` owns those.

`facts_of` is the seam: it lifts an ORM row into the pure `KeyFacts` the policy
reasons about, so the policy never imports SQLAlchemy and its tests never need
a row.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from . import policy
from .models import ApiKey, ApiKeyUsage


def facts_of(key: ApiKey) -> policy.KeyFacts:
    return policy.KeyFacts(
        is_active=key.is_active,
        expires_at=key.expires_at,
        scopes=tuple(key.scopes or ()),
    )


async def create_key(
    session: AsyncSession,
    *,
    key_id: str,
    name: str,
    key_prefix: str,
    key_hash: str,
    scopes: list[str],
    expires_at: datetime | None,
    created_by_user_id: int,
) -> ApiKey:
    key = ApiKey(
        id=key_id,
        name=name,
        key_prefix=key_prefix,
        key_hash=key_hash,
        scopes=scopes,
        expires_at=expires_at,
        created_by_user_id=created_by_user_id,
    )
    session.add(key)
    await session.flush()
    return key


async def find_by_prefix(session: AsyncSession, prefix: str) -> ApiKey | None:
    """One indexed lookup. The prefix is non-secret; the hash is the credential."""
    return await session.scalar(select(ApiKey).where(ApiKey.key_prefix == prefix))


async def list_keys(session: AsyncSession) -> list[ApiKey]:
    rows = await session.scalars(select(ApiKey).order_by(ApiKey.created_at.desc()))
    return list(rows)


async def revoke(session: AsyncSession, key_id: str) -> bool:
    """Revoke, never delete. False if there was no such active key.

    `is_active` and `revoked_at` move TOGETHER because `ck_api_keys_revoked`
    forbids the half state — writing one without the other raises.
    """
    result = await session.execute(
        update(ApiKey)
        .where(ApiKey.id == key_id, ApiKey.is_active.is_(True))
        .values(is_active=False, revoked_at=datetime.now(timezone.utc))
    )
    return result.rowcount > 0


async def touch_last_used(session: AsyncSession, key_id: str) -> None:
    await session.execute(
        update(ApiKey)
        .where(ApiKey.id == key_id)
        .values(last_used_at=datetime.now(timezone.utc))
    )


async def record_usage(
    session: AsyncSession,
    *,
    api_key_id: str,
    route: str,
    status_code: int,
    bytes_in: int,
    duration_ms: int,
    width: int | None = None,
    height: int | None = None,
    lines_out: int | None = None,
) -> None:
    """One row per call. Deliberately holds NO image bytes and NO OCR text.

    The text is the caller's own content; retaining it would recreate exactly
    the confidentiality problem the 'usage record only' decision avoided. What
    is kept is enough to answer 'who called, how often, how big, how slow, and
    did it work' — and `request_id` on a support ticket joins to it.
    """
    session.add(
        ApiKeyUsage(
            api_key_id=api_key_id,
            route=route,
            status_code=status_code,
            bytes_in=bytes_in,
            duration_ms=duration_ms,
            width=width,
            height=height,
            lines_out=lines_out,
        )
    )
