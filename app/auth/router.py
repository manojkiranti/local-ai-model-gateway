"""Auth endpoints: register and login (issues a JWT)."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db.session import get_session
from ..users.models import ROLE_ADMIN, ROLE_MEMBER, User
from ..users.schemas import UserOut
from .schemas import LoginRequest, RegisterRequest, TokenResponse
from .security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])
_settings = get_settings()


async def _resolve_role(session: AsyncSession, email: str) -> str:
    """Admin if in the allowlist, or if this is the very first user."""
    if email in _settings.admin_email_set:
        return ROLE_ADMIN
    count = (await session.execute(select(func.count()).select_from(User))).scalar_one()
    return ROLE_ADMIN if count == 0 else ROLE_MEMBER


@router.post(
    "/register",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new local (email + password) user",
)
async def register(
    body: RegisterRequest, session: AsyncSession = Depends(get_session)
) -> User:
    email = body.email.lower()

    existing = (
        await session.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    user = User(
        email=email,
        auth_provider="local",
        password_hash=hash_password(body.password),
        role=await _resolve_role(session, email),
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Log in with email + password, receive a JWT",
)
async def login(
    body: LoginRequest, session: AsyncSession = Depends(get_session)
) -> TokenResponse:
    email = body.email.lower()
    user = (
        await session.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()

    # Same error whether the email is unknown or the password is wrong.
    if user is None or user.password_hash is None or not verify_password(
        body.password, user.password_hash
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User is inactive")

    token = create_access_token(subject=user.email, role=user.role)
    return TokenResponse(access_token=token, expires_in=_settings.jwt_expire_minutes * 60)
