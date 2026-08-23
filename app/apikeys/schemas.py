"""Admin request/response models for /v1/api-keys.

`extra="forbid"` throughout: `is_active` or `key_hash` in a create body must be
a loud 422, not a silently ignored field. Same rule as `UserPatch` refusing
`role` and the NRB run schema refusing `all_files`.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .policy import ALL_SCOPES, SCOPE_OCR_READ


class ApiKeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1,
        max_length=255,
        description="A human-readable label for this key, e.g. the calling app's name.",
    )
    scopes: list[str] = Field(
        default_factory=lambda: [SCOPE_OCR_READ],
        description=(
            "What this key may do. Defaults to every scope that exists today "
            f"({sorted(ALL_SCOPES)}). An empty list is rejected — a key with "
            "no scopes can do nothing."
        ),
    )
    expires_at: datetime | None = Field(
        default=None,
        description=(
            "Optional expiry. A naive value (no timezone) is treated as UTC. "
            "A value in the past is rejected outright, rather than minting a "
            "key that would 401 forever with no signal anything is wrong."
        ),
    )

    @field_validator("scopes")
    @classmethod
    def _known_scopes(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - ALL_SCOPES)
        if unknown:
            raise ValueError(
                f"unknown scope(s): {', '.join(unknown)}; "
                f"known scopes are {', '.join(sorted(ALL_SCOPES))}"
            )
        if not value:
            raise ValueError("a key with no scopes can do nothing")
        return value

    @field_validator("expires_at")
    @classmethod
    def _future_utc_expiry(cls, value: datetime | None) -> datetime | None:
        """A naive datetime is interpreted in the SERVER's timezone by the
        driver on the way into Postgres — measured: a naive
        `2027-01-01T00:00:00` stores as `2026-12-31T18:15:00+00` (this
        server's +05:45 offset). Behind a server on UTC the same input would
        expire up to ~12h LATE, silently fail-open. Normalise here, at the
        boundary, rather than trusting every future caller of this schema to
        remember.

        A past `expires_at` is rejected outright: without this an admin could
        mint a 201 for a key that 401s forever and get no signal that
        anything is wrong.
        """
        if value is None:
            return value
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        if value <= datetime.now(timezone.utc):
            raise ValueError("expires_at must be in the future")
        return value


class ApiKeyCreated(BaseModel):
    """The ONLY response that ever carries the plaintext key."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "9f1c2a6e4b3d4f7c8a1e2b3c4d5e6f70",
                "name": "odin-crm-ocr",
                "prefix": "a1b2c3d4",
                "key": (
                    "lgw_live_a1b2c3d4_"
                    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
                ),
                "scopes": ["ocr:read"],
                "expires_at": None,
            }
        }
    )

    id: str = Field(description="The key's id — use this to revoke it later.")
    name: str
    prefix: str = Field(
        description="The non-secret lookup handle at the start of `key`, also shown by GET /v1/api-keys."
    )
    key: str = Field(
        description=(
            "The plaintext credential, shown here and NEVER AGAIN. Store it "
            "in the calling app's secret manager immediately; there is no "
            "recovery, only revoking this key and minting a new one."
        )
    )
    scopes: list[str]
    expires_at: datetime | None


class ApiKeyOut(BaseModel):
    """A listed key. Deliberately has no `key` and no `key_hash` field."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "9f1c2a6e4b3d4f7c8a1e2b3c4d5e6f70",
                "name": "odin-crm-ocr",
                "prefix": "a1b2c3d4",
                "scopes": ["ocr:read"],
                "is_active": True,
                "created_at": "2026-08-23T09:15:00Z",
                "last_used_at": "2026-08-23T10:02:11Z",
                "expires_at": None,
            }
        }
    )

    id: str
    name: str
    prefix: str = Field(description="The non-secret lookup handle; the full key is never listed.")
    scopes: list[str]
    is_active: bool = Field(description="False once revoked. Revoked keys are listed, never deleted.")
    created_at: datetime
    last_used_at: datetime | None = Field(
        description="Updated on every authenticated request this key makes, before the route body runs."
    )
    expires_at: datetime | None
