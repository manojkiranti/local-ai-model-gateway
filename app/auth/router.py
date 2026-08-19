"""Auth endpoints: register and login (issues a JWT).

**Login dispatches on the user's `auth_provider`; it is never a fallback chain.**
A `local` user is checked against their bcrypt hash and Active Directory is not
consulted. An `ad` user is checked against the directory and their (always NULL)
hash is not consulted. That asymmetry is the security property:

  - "try AD, then fall back to local" would let an offboarded employee keep
    signing in on a stale `password_hash` after their AD account was disabled;
  - "try local first" would let a locally-set password shadow an AD identity.

`ck_users_credential` in `app/users/models.py` enforces the same rule one layer
down, so no future code path can give one identity two ways in.

An identifier with **no** user row is the one case that consults the directory
without knowing the provider in advance, and a `Success` there provisions the row
(`users_repo.create_directory_user`). That is the intended onboarding path: no
admin action to sign in, and no department visible until an admin grants one.

Three failure modes that are easy to collapse and must not be:

  - **401 vs 503.** A rejected credential is 401; a directory that cannot be
    reached is 503. Rendering an outage as "invalid email or password" sends a
    whole office to reset passwords that were never wrong.
  - **401 vs 429.** Because login now forwards credentials to AD, an unthrottled
    endpoint is a way to trip the DOMAIN lockout counter on every account in the
    company. See `app/auth/throttle.py`. An UNAVAILABLE outcome deliberately does
    NOT count against the limit — an outage is not the user's fault, and counting
    it would lock everybody out for the duration.
  - **Register is admin-only, EXCEPT on an empty `users` table.** With public
    registration, anyone could pre-register a colleague's address as a *local*
    account and permanently shadow their AD identity — and on a fresh database
    `_resolve_role` would hand them admin as the first user.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings, get_settings
from ..db.session import get_session
from ..users import repository as users_repo
from ..users.models import PROVIDER_AD, PROVIDER_LOCAL, ROLE_ADMIN, ROLE_MEMBER, User
from ..users.schemas import UserOut
from . import directory
from .dependencies import get_current_user_optional
from .directory import DirectoryOutcome
from .schemas import LoginRequest, RegisterRequest, TokenResponse
from .security import create_access_token, hash_password, verify_password
from .throttle import LoginThrottle, get_throttle

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger("app.auth")

# Rendered to the client whenever a directory user cannot be served. Worded so a
# frontend can show it verbatim without implying the password was wrong.
DIRECTORY_UNAVAILABLE_DETAIL = (
    "Directory sign-in is temporarily unavailable. This is not a password "
    "problem — please try again shortly."
)


def _invalid_credentials() -> HTTPException:
    """One message for every rejection.

    Identical whether the identifier is unknown, the local password is wrong, or
    the directory said "Failed" — the response must not reveal which, nor whether
    a given address has an account at all.
    """
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid email or password",
    )


def _directory_unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=DIRECTORY_UNAVAILABLE_DETAIL,
    )


async def _resolve_role(session: AsyncSession, email: str) -> str:
    """Admin if in the allowlist, or if this is the very first user.

    The empty-table clause is a REGISTRATION bootstrap and must stay reachable
    only from `register`. `users_repo.resolve_directory_role` is the directory
    equivalent and deliberately omits it.
    """
    if email in get_settings().admin_email_set:
        return ROLE_ADMIN
    count = (await session.execute(select(func.count()).select_from(User))).scalar_one()
    return ROLE_ADMIN if count == 0 else ROLE_MEMBER


@router.post(
    "/register",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new local (email + password) user — admin only",
)
async def register(
    body: RegisterRequest,
    caller: User | None = Depends(get_current_user_optional),
    session: AsyncSession = Depends(get_session),
) -> User:
    email = body.email.lower()

    user_count = (
        await session.execute(select(func.count()).select_from(User))
    ).scalar_one()
    if user_count > 0:
        # Not the bootstrap: this is an admin creating a local service or
        # break-glass account.
        if caller is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required to register a user",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if caller.role != ROLE_ADMIN:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admin privileges required",
            )

    existing = await users_repo.get_by_email(session, email)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email already registered"
        )

    user = User(
        email=email,
        auth_provider=PROVIDER_LOCAL,
        password_hash=hash_password(body.password),
        role=await _resolve_role(session, email),
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        # The check above is a read-then-write; under concurrency the unique
        # index is the real arbiter. Report the conflict, not a 500.
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email already registered"
        )
    await session.refresh(user)
    return user


def _verify_local(
    throttle: LoginThrottle, identifier: str, user: User, password: str
) -> None:
    """Check a local password. The directory is not consulted on this path."""
    if user.password_hash is None or not verify_password(password, user.password_hash):
        throttle.record_failure(identifier)
        raise _invalid_credentials()


async def _verify_directory(
    settings: Settings, throttle: LoginThrottle, identifier: str, password: str
) -> None:
    """Check a credential against AD. The local hash is not consulted."""
    if not settings.ad_auth_enabled:
        # Fail CLOSED. A directory-backed user must never fall through to a
        # local password check just because the integration is switched off.
        logger.warning(
            "directory sign-in attempted for %s while AD_AUTH_ENABLED is false",
            identifier,
        )
        raise _directory_unavailable()

    outcome = await directory.verify_credentials(identifier, password)
    if outcome is DirectoryOutcome.UNAVAILABLE:
        # Not counted against the throttle: an outage is not a failed attempt,
        # and counting it would lock every user out for the whole outage.
        raise _directory_unavailable()
    if outcome is not DirectoryOutcome.AUTHENTICATED:
        throttle.record_failure(identifier)
        raise _invalid_credentials()


async def _login_unknown_identifier(
    session: AsyncSession,
    settings: Settings,
    throttle: LoginThrottle,
    identifier: str,
    password: str,
) -> User:
    """No local row: ask the directory, and provision on success."""
    if not settings.ad_auth_enabled:
        # Exactly the pre-AD behaviour: an unknown address is a 401.
        throttle.record_failure(identifier)
        raise _invalid_credentials()

    await _verify_directory(settings, throttle, identifier, password)

    role = users_repo.resolve_directory_role(identifier, settings.admin_email_set)
    user = await users_repo.create_directory_user(
        session, email=identifier, role=role
    )
    logger.info("provisioned directory user %s with role %s", identifier, role)
    return user


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Log in with email + password (local or Active Directory), receive a JWT",
)
async def login(
    body: LoginRequest, session: AsyncSession = Depends(get_session)
) -> TokenResponse:
    settings = get_settings()
    throttle = get_throttle()
    identifier = body.email.lower()

    retry_after = throttle.retry_after(identifier)
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                "Too many failed sign-in attempts. Try again in "
                f"{retry_after} seconds."
            ),
            headers={"Retry-After": str(retry_after)},
        )

    user = await users_repo.get_by_email(session, identifier)

    if user is None:
        user = await _login_unknown_identifier(
            session, settings, throttle, identifier, body.password
        )
    elif user.auth_provider == PROVIDER_LOCAL:
        _verify_local(throttle, identifier, user, body.password)
    elif user.auth_provider == PROVIDER_AD:
        await _verify_directory(settings, throttle, identifier, body.password)
    else:
        # `ck_users_auth_provider` should make this unreachable. If it is ever
        # reached, refuse rather than guess which credential store to trust —
        # falling through to the directory branch would send, say, a Google
        # user's password to AD.
        logger.error(
            "user %s has unrecognised auth_provider %r; refusing sign-in",
            identifier,
            user.auth_provider,
        )
        raise _invalid_credentials()

    # Checked AFTER the credential, as it always was: an inactive account that
    # answered 403 to any password would confirm the account exists.
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="User is inactive"
        )

    throttle.reset(identifier)
    token = create_access_token(subject=user.email, role=user.role)
    return TokenResponse(
        access_token=token, expires_in=settings.jwt_expire_minutes * 60
    )
