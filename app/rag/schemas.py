"""Request/response models for the department admin API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class DepartmentCreate(BaseModel):
    # Lowercase slug: this is what the frontend tab sends on every chat turn.
    code: str = Field(min_length=1, max_length=32, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    name: str = Field(min_length=1, max_length=128)


class DepartmentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    is_active: bool | None = None


class DepartmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    is_active: bool
    created_at: datetime


class GrantCreate(BaseModel):
    user_id: int


class MemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: int
    department_id: int
    granted_by: int | None
    granted_at: datetime
