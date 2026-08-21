"""Request/response models for the department admin API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

# The level vocabulary as the API accepts it. `Literal` rejects a typo with 422
# before the request reaches ck_user_departments_role, so a client gets a field
# error rather than a 500 out of an IntegrityError.
DepartmentRole = Literal["viewer", "editor", "owner"]


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
    # The CALLER's effective level here ('owner' for a global admin with no grant
    # row). Server-side so the frontend has ONE rule — role is editor or owner
    # means show the upload button — instead of reimplementing the policy against
    # /users/me. Not on the ORM row, so `_department_out` builds it.
    #
    # REQUIRED, not `str | None`. A fail-closed client hides every control when
    # this is null, with no error anywhere to explain why, so a construction site
    # that forgets it must fail loudly here instead.
    role: DepartmentRole


class GrantCreate(BaseModel):
    """Who to grant, by id or by email.

    `email` exists because an admin knows a colleague's address, not their
    numeric id, and resolving one to the other used to mean paging the whole
    user list. Exactly one must be supplied: accepting both would raise the
    question of which wins when they disagree, and silently preferring one is
    how the wrong person gets access to a department.
    """

    model_config = ConfigDict(extra="forbid")

    user_id: int | None = None
    email: EmailStr | None = None
    # ABSENT means "do not change the level": viewer for a new grant, unchanged
    # for an existing one. It cannot default to "viewer", because this endpoint
    # upserts — "field omitted" would then be indistinguishable from "set to
    # viewer" and re-adding an existing owner would silently strip them, with a
    # 204 and no audit trail. Demotion must be something you asked for.
    role: DepartmentRole | None = None

    @model_validator(mode="after")
    def _exactly_one_identifier(self) -> "GrantCreate":
        if (self.user_id is None) == (self.email is None):
            raise ValueError("supply exactly one of user_id or email")
        return self


class MemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: int
    department_id: int
    role: DepartmentRole
    # `GET /users` is global-admin-only, so without the email a department owner
    # managing members sees bare integers they have no way to resolve.
    email: str
    granted_by: int | None
    granted_at: datetime


class TextDocumentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=512)
    content: str = Field(min_length=1)


class DocumentOut(BaseModel):
    """Member-facing. Deliberately omits `embed_model` — which model produced
    the vectors is an operations detail with no UI use, and leaking the model
    inventory to every reader buys nothing."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    department_id: int
    title: str
    source: str
    file_type: str
    file_name: str | None
    status: str
    chunk_count: int
    created_at: datetime


class DocumentAdminOut(DocumentOut):
    """Admin-facing: adds the operational fields used to manage the corpus."""

    embed_model: str | None
    embed_dim: int | None
    updated_at: datetime


class IngestAccepted(BaseModel):
    document_id: str
    job_id: str
    status: str


class IngestJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    document_id: str
    status: str
    chunks_total: int | None
    chunks_done: int
    attempts: int
    error: str | None
    created_at: datetime
    finished_at: datetime | None
