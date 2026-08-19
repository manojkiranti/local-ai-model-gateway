"""FastAPI dependencies for authenticating requests and gating admin routes."""

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWTError
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.session import get_session
from ..users.models import ROLE_ADMIN, User
from ..users.repository import get_by_email
from .security import decode_token

# Bearer scheme -> renders an "Authorize" box in Swagger and reads the
# Authorization: Bearer <jwt> header.
_bearer = HTTPBearer(auto_error=True)
# The same scheme without the automatic rejection, for the ONE route that is
# admin-only except when the users table is empty (`POST /auth/register`).
_optional_bearer = HTTPBearer(auto_error=False)

_CREDENTIALS_EXC = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
) -> User:
    try:
        payload = decode_token(creds.credentials)
    except PyJWTError:
        raise _CREDENTIALS_EXC
    email = payload.get("sub")
    if not email:
        raise _CREDENTIALS_EXC

    user = await get_by_email(session, email)
    if user is None or not user.is_active:
        raise _CREDENTIALS_EXC
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != ROLE_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
        )
    return user


async def get_current_user_optional(
    creds: HTTPAuthorizationCredentials | None = Depends(_optional_bearer),
    session: AsyncSession = Depends(get_session),
) -> User | None:
    """The caller if they presented a valid token, else None.

    Used only by `POST /auth/register`, which is admin-only EXCEPT on an empty
    `users` table, where it is the bootstrap that creates the first admin. A
    plain `Depends(require_admin)` cannot express that, because there is nobody
    to be an admin yet.

    A token that is present but bad is still rejected — "no credentials" and
    "broken credentials" are different, and silently treating the latter as
    anonymous would let an expired admin token fall through to the bootstrap
    check.
    """
    if creds is None:
        return None
    try:
        payload = decode_token(creds.credentials)
    except PyJWTError:
        raise _CREDENTIALS_EXC
    email = payload.get("sub")
    if not email:
        raise _CREDENTIALS_EXC

    user = await get_by_email(session, email)
    if user is None or not user.is_active:
        raise _CREDENTIALS_EXC
    return user
