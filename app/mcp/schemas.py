"""Request and response models for the MCP grant admin route."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

from .grants import ALL_GRANTS


class GrantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    grant_key: str
    granted_at: datetime
    granted_by: int | None


class GrantListResponse(BaseModel):
    user_id: int
    items: list[GrantOut]


class GrantCreate(BaseModel):
    """`extra="forbid"` matches `UserUpdate`: an unexpected field is refused
    loudly rather than silently ignored."""

    model_config = ConfigDict(extra="forbid")

    grant_key: str

    @field_validator("grant_key")
    @classmethod
    def _must_be_known(cls, value: str) -> str:
        # Validated against ALL_GRANTS rather than restated as a Literal, which
        # would be a fourth copy of the vocabulary. Pydantic turns this into a
        # 422 with the offending value named — the CHECK would give a 500.
        if value not in ALL_GRANTS:
            raise ValueError(
                f"unknown grant: {value!r}; expected one of {sorted(ALL_GRANTS)}"
            )
        return value
