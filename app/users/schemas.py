"""Pydantic schemas for user output."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    auth_provider: str
    role: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


class UserUpdate(BaseModel):
    """What an admin may change about a user.

    `extra="forbid"` on purpose: `role` is NOT patchable here. Promotion is a
    privilege-escalation surface that needs its own guards (self-demotion, the
    last admin) and its own audit story, and ADMIN_EMAILS already designates
    admins at creation time. A request trying to set `role` is refused loudly
    rather than having the field silently ignored.
    """

    model_config = ConfigDict(extra="forbid")

    is_active: bool


class UserListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[UserOut]
