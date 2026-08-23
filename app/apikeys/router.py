"""/v1/api-keys — admin-only key management (JWT, not API key).

Managing keys is a human, privileged act, so it sits behind the EXISTING
`require_admin`. A key can never manage keys: `require_api_client` is not used
here, and `ocr:read` is the only scope that exists.

No PATCH: rotation is "mint a new one, revoke the old", which needs no overlap
state machine. No GET /{id} and no usage listing — nothing consumes them.
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import require_admin
from ..config import get_settings
from ..db.session import get_session
from ..users.models import User
from . import keygen, repository
from .schemas import ApiKeyCreate, ApiKeyCreated, ApiKeyOut

router = APIRouter(prefix="/v1", tags=["api-keys"])


def _out(key) -> ApiKeyOut:
    return ApiKeyOut(
        id=key.id,
        name=key.name,
        prefix=key.key_prefix,
        scopes=list(key.scopes or []),
        is_active=key.is_active,
        created_at=key.created_at,
        last_used_at=key.last_used_at,
        expires_at=key.expires_at,
    )


@router.post(
    "/api-keys",
    response_model=ApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Mint an API key for an external caller (admin)",
    responses={
        401: {"description": "Missing/invalid JWT."},
        403: {"description": "Not an admin."},
        422: {"description": "Unknown scope, or an unexpected field."},
    },
)
async def create_api_key(
    body: ApiKeyCreate,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Returns the plaintext key ONCE. It is never recoverable afterwards."""
    minted = keygen.mint(get_settings().api_key_prefix)
    key = await repository.create_key(
        session,
        key_id=uuid4().hex,
        name=body.name,
        key_prefix=minted.prefix,
        key_hash=minted.key_hash,
        scopes=list(body.scopes),
        expires_at=body.expires_at,
        created_by_user_id=admin.id,
    )
    await session.commit()
    return ApiKeyCreated(
        id=key.id,
        name=key.name,
        prefix=minted.prefix,
        key=minted.token,
        scopes=list(key.scopes or []),
        expires_at=key.expires_at,
    )


@router.get(
    "/api-keys",
    response_model=list[ApiKeyOut],
    summary="List API keys, newest first (admin)",
)
async def list_api_keys(
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Revoked keys are listed too — `is_active` says which. They are never
    deleted, so a leaked key's history stays attributable."""
    return [_out(k) for k in await repository.list_keys(session)]


@router.delete(
    "/api-keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an API key (admin)",
    responses={404: {"description": "No such active key."}},
)
async def revoke_api_key(
    key_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Revocation is `is_active=false` + `revoked_at`, never a DELETE: the
    usage rows are the only evidence of what this key did."""
    if not await repository.revoke(session, key_id):
        raise HTTPException(status_code=404, detail="No such active API key")
    await session.commit()
