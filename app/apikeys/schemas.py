"""Admin request/response models for /v1/api-keys.

`extra="forbid"` throughout: `is_active` or `key_hash` in a create body must be
a loud 422, not a silently ignored field. Same rule as `UserPatch` refusing
`role` and the NRB run schema refusing `all_files`.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .policy import ALL_SCOPES, SCOPE_OCR_READ


class ApiKeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    scopes: list[str] = Field(default_factory=lambda: [SCOPE_OCR_READ])
    expires_at: datetime | None = None

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


class ApiKeyCreated(BaseModel):
    """The ONLY response that ever carries the plaintext key."""

    id: str
    name: str
    prefix: str
    key: str
    scopes: list[str]
    expires_at: datetime | None


class ApiKeyOut(BaseModel):
    """A listed key. Deliberately has no `key` and no `key_hash` field."""

    id: str
    name: str
    prefix: str
    scopes: list[str]
    is_active: bool
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
