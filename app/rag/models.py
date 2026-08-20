"""ORM models for department-scoped RAG.

One invariant is worth stating up front: a chunk's `department_id` is not merely
denormalized from its document. The composite FK
`(document_id, department_id) -> documents(id, department_id)` makes it
*provably* the document's, which is what lets retrieval filter on
`WHERE department_id = ?` without a join and still be a security boundary rather
than a convention.

IDs follow the project convention: UUID-hex for rows the frontend renders
(documents, ingest jobs), integer PKs for the small admin tables.
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from ..db.base import Base

# Registration import, not a usage one: `user_departments`, `documents.uploaded_by`
# and `user_departments.granted_by` all reference `users.id` BY NAME, and
# SQLAlchemy resolves that lazily at mapper-configuration time. Without the
# `users` table in Base.metadata first, any consumer that imports only this
# module gets NoReferencedTableError. Importing it here makes this module
# self-sufficient instead of making every caller remember.
from ..users import models as _users_models  # noqa: F401

# Must equal the vector(N) width in the migration. Qwen3-Embedding is 2560
# native, MRL-truncated to 1536 — pgvector's HNSW index caps at 2000 dims.
EMBED_DIM = 1536

# documents.status
STATUS_PENDING = "pending"
STATUS_READY = "ready"
STATUS_FAILED = "failed"
STATUS_ARCHIVED = "archived"

# ingest_jobs.status
JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_SUCCEEDED = "succeeded"
JOB_FAILED = "failed"

# documents.source
SOURCE_UPLOAD = "upload"
SOURCE_MANUAL = "manual"


def _uuid_hex() -> str:
    return uuid4().hex


class Department(Base):
    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(primary_key=True)
    # What the frontend tab sends. Lowercase by convention ('hr', 'finance').
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # Soft-disable is the ONLY retirement path: documents and chat_sessions both
    # reference departments with ON DELETE RESTRICT, so a department that has
    # ever been used cannot be deleted. That is deliberate (audit retention).
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class UserDepartment(Base):
    """The permission boundary for ordinary users.

    Absence of a row means no access **for a non-admin**. Admins deliberately
    bypass this table (see `access.resolve_department`) — they still cannot
    reach an inactive department, and they are still bound by a session's own
    department. Grants are the mechanism for members, not a global gate.
    """

    __tablename__ = "user_departments"
    __table_args__ = (
        # Closed vocabulary, same rule as ck_documents_status: every gate compares
        # this exact string, so an unrecognised value is not cosmetic — it is a
        # level that allows nothing (`permissions.allows` fails closed). Adding a
        # level means editing this CHECK.
        CheckConstraint(
            "role IN ('viewer', 'editor', 'owner')",
            name="ck_user_departments_role",
        ),
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    department_id: Mapped[int] = mapped_column(
        ForeignKey("departments.id", ondelete="CASCADE"), primary_key=True
    )
    # What the holder may DO here: viewer < editor < owner (app/rag/permissions.py).
    # Defaulting to the weakest level is least privilege on omission, and it is what
    # let the migration backfill every pre-existing grant without a data step.
    role: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'viewer'")
    )
    granted_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        # FK target for DocumentChunk's composite FK — without this the
        # invariant below cannot be expressed at all.
        UniqueConstraint("id", "department_id", name="uq_documents_id_department"),
        # The partial index below keys off the EXACT string 'archived', so the
        # status vocabulary has to be closed. A typo would silently produce a
        # row that no predicate matches.
        CheckConstraint(
            "status IN ('pending', 'ready', 'failed', 'archived')",
            name="ck_documents_status",
        ),
        CheckConstraint(
            "source IN ('upload', 'manual')", name="ck_documents_source"
        ),
        # Dedup among NON-archived rows only. Archiving deletes the chunks but
        # keeps the row for audit; a plain UNIQUE(department_id, content_hash)
        # would then permanently block re-adding a file that was once archived.
        Index(
            "ux_documents_active_content",
            "department_id",
            "content_hash",
            unique=True,
            postgresql_where=text("status <> 'archived'"),
        ),
        # AT MOST ONE CURRENT VERSION per logical NRB source (Phase 7 step 3).
        # NRB republishes the same file at the same URL with new bytes, which is
        # a new `content_hash` and therefore a second row that
        # `ux_documents_active_content` is perfectly happy with. This is the
        # index that says only one of them may be SEARCHABLE.
        #
        # `metadata->>'comparison_key'` is the catalog's own logical file
        # identity (`app/nrb/supersession.py` explains why not page_url and why
        # never a title). Rows without it index as NULL and never conflict, so
        # ordinary uploads and pre-Phase-7 documents are untouched.
        #
        # Declared here AND hand-written in migration 8f2d1c05a7b4, then
        # excluded from autogenerate comparison in `alembic/env.py` — Alembic
        # cannot reflect a JSONB expression index, so without the exclusion
        # every drift check proposes dropping and recreating it. Same treatment
        # as the HNSW/GIN indexes, for the same reason.
        Index(
            "ux_documents_nrb_current_source",
            "department_id",
            text("(metadata ->> 'comparison_key')"),
            unique=True,
            postgresql_where=text(
                "status = 'ready' AND metadata ->> 'origin' = 'nrb'"
                " AND metadata ->> 'comparison_key' IS NOT NULL"
            ),
        ),
        Index("ix_documents_department", "department_id"),
        Index("ix_documents_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_hex)
    department_id: Mapped[int] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)  # upload|manual
    file_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # Both NULL for typed-in text (source='manual').
    file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # A RELATIVE key under RAG_DOCS_DIR (e.g. "hr/9f3c....pdf"), never a
    # host-specific absolute path. Keeps the rows portable across machines and
    # leaves a clean migration path to object storage, where the same value
    # becomes the bucket key.
    storage_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # sha256 of the bytes, or of the typed text. Drives the dedup index above.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=STATUS_PENDING
    )
    # Audit: which model produced this document's vectors. Identifies documents
    # holding stale embeddings after a model swap.
    embed_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    embed_dim: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    # `metadata` is reserved by SQLAlchemy declarative (Base.metadata), so the
    # attribute is `meta` while the column keeps the intended name.
    meta: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    uploaded_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint(
            "document_id", "chunk_index", name="uq_document_chunks_doc_index"
        ),
        # THE invariant. Postgres rejects a chunk whose department_id is not its
        # document's, so `WHERE department_id = ?` is backed by the database
        # rather than by application code behaving correctly.
        ForeignKeyConstraint(
            ["document_id", "department_id"],
            ["documents.id", "documents.department_id"],
            ondelete="CASCADE",
            name="fk_document_chunks_document_department",
        ),
        Index("ix_chunks_department", "department_id"),
        # Declared here so autogenerate knows they exist and never proposes
        # dropping them. They are still created by hand in the migration —
        # Alembic cannot round-trip the HNSW opclass and build options, so
        # `alembic/env.py` also excludes these two names from comparison.
        Index(
            "ix_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_with={"m": 16, "ef_construction": 64},
        ),
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(String(32), nullable=False)
    department_id: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # NOT NULL: a chunk can never exist without a searchable vector. Document
    # completeness is a separate guarantee, provided by the ingest transaction.
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBED_DIM), nullable=False)
    # Lexical channel. 'english' rather than 'simple' is deliberate and measured:
    # English stems ('loans'->'loan') while Devanagari passes through untouched,
    # so a mixed Nepali/English corpus gains recall and loses nothing.
    tsv: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', content)", persisted=True),
        nullable=True,
    )
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)  # PDF only
    section: Mapped[str | None] = mapped_column(String(512), nullable=True)
    element_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    meta: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class IngestJob(Base):
    __tablename__ = "ingest_jobs"
    __table_args__ = (
        # Load-bearing: ux_ingest_jobs_active_document's predicate is the exact
        # pair ('queued','running'). A typo'd status would match no predicate and
        # so bypass the one-active-job-per-document guarantee entirely.
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_ingest_jobs_status",
        ),
        Index("ix_ingest_jobs_status", "status", "created_at"),
        # SELECT ... FOR UPDATE SKIP LOCKED stops two workers claiming the same
        # ROW; it does nothing about two distinct active JOBS for one document,
        # which would both run the replacement transaction. Enqueue must catch
        # this violation and return 409.
        Index(
            "ux_ingest_jobs_active_document",
            "document_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_hex)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=JOB_QUEUED
    )
    chunks_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Embedding progress. Inserts all land at COMMIT, so this is not row count.
    chunks_done: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Drives the stale-job sweep: running + stale heartbeat -> failed, retryable.
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
