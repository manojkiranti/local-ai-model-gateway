"""User ORM model.

Provider-agnostic by design so SSO/OIDC can be added later WITHOUT a schema
rewrite:
  - `email` is the stable identity.
  - `auth_provider` records how the user authenticates ("local" now; "google",
    "microsoft", ... later).
  - `password_hash` is nullable — SSO users won't have one.
  - `role` gates admin-only routes.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from ..db.base import Base

ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"


class User(Base):
    __tablename__ = "users"

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
