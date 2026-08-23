"""ORM models for API keys and their usage log.

Two constraints do real work and must survive any rewrite:

  * `ck_api_keys_scopes` closes the scope vocabulary, the same rule as
    `ck_documents_status` and `ck_user_departments_role`. A typo'd scope must
    fail at INSERT rather than sit in a key someone believes works. Adding a
    scope means editing this CHECK.
  * `ck_api_keys_revoked` makes the half-revoked state unrepresentable:
    `is_active=false` with no `revoked_at` (inactive since when?) and
    `is_active=true` with a `revoked_at` (revoked but still serving) are both
    illegal. Same shape as `ck_nrb_files_blocked_reason`.

Both FKs are ON DELETE RESTRICT and keys are never hard-deleted, exactly like
departments and `nrb_files`: usage rows are the only evidence of a leaked key's
activity, and a cascade would destroy it. Revocation is `is_active=false`.

`ApiKeyUsage.id` is a bigserial because the table grows once per request and is
only ever read by time range; `ApiKey.id` is uuid-hex like every other id here.
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from ..db.base import Base
from .policy import ALL_SCOPES

_SCOPE_LIST = ", ".join(f"'{s}'" for s in sorted(ALL_SCOPES))


def _uuid_hex() -> str:
    return uuid4().hex


class ApiKey(Base):
    __tablename__ = "api_keys"
    __table_args__ = (
        CheckConstraint(
            "(revoked_at IS NULL) = is_active",
            name="ck_api_keys_revoked",
        ),
        CheckConstraint(
            f"scopes <@ ARRAY[{_SCOPE_LIST}]::text[]",
            name="ck_api_keys_scopes",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_hex)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # NON-secret lookup handle. UNIQUE and indexed: verification is one B-tree
    # hit, and a collision would make verification ambiguous.
    key_prefix: Mapped[str] = mapped_column(
        String(16), nullable=False, unique=True, index=True
    )
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Text, not String: the CHECK below casts the vocabulary literal to
    # `::text[]`, and Postgres has no `<@` operator between `character
    # varying[]` and `text[]` — CREATE TABLE fails outright with
    # "operator does not exist" if this column is VARCHAR[] instead.
    scopes: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default="{}"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ApiKeyUsage(Base):
    __tablename__ = "api_key_usage"
    __table_args__ = (
        Index("ix_api_key_usage_key_time", "api_key_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    api_key_id: Mapped[str] = mapped_column(
        ForeignKey("api_keys.id", ondelete="RESTRICT"), nullable=False
    )
    route: Mapped[str] = mapped_column(String(128), nullable=False)
    status_code: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    bytes_in: Mapped[int] = mapped_column(Integer, nullable=False)
    # NULL when the image was never decoded (a 401/413/429 path).
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lines_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
