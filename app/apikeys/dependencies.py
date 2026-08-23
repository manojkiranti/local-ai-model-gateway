"""The `X-API-Key` boundary. The ONLY place the pieces are combined.

`require_api_client` returns an `ApiClient` and never a `User`. That is the
whole reason this package exists: `app/auth/dependencies.py` resolves humans
and never sees a key, so no route written for a JWT user can be reached with
one, and no key can inherit admin or a department grant.

It is a dependency FACTORY (`require_api_client("ocr:read")`) rather than a
plain dependency, because the required scope belongs to the ROUTE. A single
dependency reading a scope out of the request would let the caller choose which
scope they are checked against.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.session import get_session
from . import keygen, policy, repository
from .throttle import get_auth_throttle

logger = logging.getLogger("app.apikeys.auth")


@dataclass(frozen=True)
class ApiClient:
    """An authenticated non-human caller. NOT a User, and never convertible."""

    key_id: str
    name: str
    scopes: tuple[str, ...]


def _invalid() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=policy.INVALID_KEY,
    )


def require_api_client(scope: str) -> Callable[..., Awaitable[ApiClient]]:
    if scope not in policy.ALL_SCOPES:
        # A programming error, caught at import time rather than at request
        # time: a route asking for a scope no key can hold would 403 forever.
        raise ValueError(f"unknown scope {scope!r}")

    async def dependency(
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        session: AsyncSession = Depends(get_session),
    ) -> ApiClient:
        parsed = keygen.parse(x_api_key or "")
        if parsed is None:
            # No prefix to throttle on, and nothing to look up.
            logger.info("api key rejected: malformed or absent header")
            raise _invalid()
        prefix, _secret = parsed

        throttle = get_auth_throttle()
        retry_after = throttle.retry_after(prefix)
        if retry_after is not None:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed attempts for this key",
                headers={"Retry-After": str(retry_after)},
            )

        key = await repository.find_by_prefix(session, prefix)

        # The log distinguishes the six causes; the RESPONSE never does.
        if key is None:
            throttle.record_failure(prefix)
            logger.info("api key rejected: no key with prefix %s", prefix)
            raise _invalid()
        if not keygen.verify(x_api_key or "", key.key_hash):
            throttle.record_failure(prefix)
            logger.warning(
                "api key rejected: secret mismatch for key %s (%s)", key.id, key.name
            )
            raise _invalid()
        if not policy.is_usable(
            repository.facts_of(key), now=datetime.now(timezone.utc)
        ):
            # Not a guess, so it does not consume a throttle attempt: a revoked
            # or expired key presented by an honest caller would otherwise lock
            # out the prefix on top of already being refused.
            logger.info(
                "api key rejected: not usable (key=%s active=%s expires=%s)",
                key.id, key.is_active, key.expires_at,
            )
            raise _invalid()

        refusal = policy.scope_refusal(repository.facts_of(key), required=scope)
        if refusal is not None:
            # 403, not 401: the credential is GENUINE. Telling the caller their
            # key is fine and their permissions are not stops them rotating a
            # working key chasing the wrong bug.
            logger.info("api key %s lacks scope %s", key.id, scope)
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=refusal)

        throttle.reset(prefix)
        await repository.touch_last_used(session, key.id)
        await session.commit()
        return ApiClient(key_id=key.id, name=key.name, scopes=tuple(key.scopes or ()))

    return dependency
