"""User ORM model.

Provider-agnostic by design so SSO/OIDC can be added later WITHOUT a schema
rewrite:
  - `email` is the stable identity.
  - `auth_provider` records how the user authenticates: "local" (bcrypt) or
    "ad" (the Active Directory shim, see `app/auth/directory.py`). The
    vocabulary is CLOSED by a CHECK constraint because the login route
    dispatches on it.
  - `password_hash` is nullable — directory users have none, and
    `ck_users_credential` forbids them ever acquiring one.
  - `role` gates admin-only routes.
"""

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from ..db.base import Base

ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"

# How a user authenticates. A CLOSED vocabulary, pinned by
# `ck_users_auth_provider` below: `app/auth/router.py` dispatches the login on
# this value, so an unrecognised provider would decide which credential store is
# consulted. Adding one means editing the CHECK too — the same rule as
# `ck_documents_status`.
PROVIDER_LOCAL = "local"
PROVIDER_AD = "ad"
AUTH_PROVIDERS = (PROVIDER_LOCAL, PROVIDER_AD)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "auth_provider IN ('local', 'ad')",
            name="ck_users_auth_provider",
        ),
        # ONE identity, ONE credential store. This makes the rule that a
        # directory-backed user can never also hold a local password
        # unrepresentable in Postgres rather than merely observed by the login
        # route: without it, an AD user who somehow acquired a `password_hash`
        # would have a second way in that survives being disabled in AD — the
        # classic directory-integration hole.
        CheckConstraint(
            "(auth_provider = 'local' AND password_hash IS NOT NULL)"
            " OR (auth_provider <> 'local' AND password_hash IS NULL)",
            name="ck_users_credential",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(
        String(320), unique=True, index=True, nullable=False
    )
    auth_provider: Mapped[str] = mapped_column(
        String(32), default="local", nullable=False
    )
    # Nullable: SSO users authenticate elsewhere and have no local password.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(String(16), default=ROLE_MEMBER, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
