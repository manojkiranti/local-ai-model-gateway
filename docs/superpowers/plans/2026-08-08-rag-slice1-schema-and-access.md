# RAG Slice 1 — Schema & Access Control Implementation Plan

> **STATUS: COMPLETE AND LOCKED (2026-08-09).** 8 commits on
> `feat/rag-slice1-schema-access`, 62 RAG tests passing, migration round-trips.
> The authorization architecture is settled — see "Decided for slice 3" at the
> foot of this file. Do not reopen it, and do not add JWT department claims,
> refresh-token infrastructure, or authorization caching.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the five RAG tables, the `chat_sessions.department_id` binding, and a server-enforced department access boundary with an admin API to manage it.

**Architecture:** Five new tables in `app/rag/models.py` behind one Alembic migration that also enables `pgvector`. Access is enforced by `resolve_department()`, which validates a request-supplied department code against `user_departments` and against the session's own department before installing it as a contextvar — the same pattern `file_sink`/`file_source` already use to thread the caller into tools without the model ever seeing it. Nothing is wired into `/v1/chat` in this slice; there is no tool to consume the context yet.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 async + asyncpg, Alembic, Postgres 16 + pgvector 0.8.5, pytest.

## Global Constraints

- Use **this project's venv**: `.venv/bin/python`, `.venv/bin/pip`, `.venv/bin/alembic`, `.venv/bin/pytest`. Never a sibling's.
- Postgres reachable at `127.0.0.1:5432`; app role `gateway`, db `local_ai_gateway`. Superuser for extension creation: `postgres`/`postgres`.
- Embedding dimension is **1536** (`EMBED_DIM`). It must match `vector(1536)` exactly.
- Full-text config is **`'english'`**, not `'simple'` — measured decision, see the design doc.
- Repository functions take an `AsyncSession` and **do not commit**; the router owns transaction boundaries. (Matches `app/history/repository.py`, `app/files/repository.py`.)
- Integration tests **skip cleanly** when Postgres is unreachable, so the offline suite stays green. Copy the `_auth()` skip pattern from `tests/test_files_integration.py`.
- The `metadata` column name is reserved by SQLAlchemy declarative — the Python attribute must be `meta`.
- Reference spec: `docs/superpowers/specs/2026-08-08-department-rag-design.md`.

**Out of scope for this slice** (do not build): document upload, Docling parsing, chunking, embedding, `ingest_jobs` execution, the retrieval query, the reranker, `search_department_docs`, the `rag_queries` / `rag_feedback` audit tables, the eval harness, and the `RAG_*` config knobs. The `ingest_jobs` and `document_chunks` *tables* land here; nothing writes to them yet.

---

### Task 1: RAG ORM models

**Files:**
- Create: `app/rag/__init__.py`
- Create: `app/rag/models.py`
- Modify: `app/history/models.py` (add `department_id` to `ChatSession`)
- Modify: `requirements.txt` (add `pgvector`)
- Modify: `alembic/env.py:20` (import the new model module)
- Test: `tests/test_rag_models.py`

**Interfaces:**
- Consumes: `app.db.base.Base`
- Produces: `Department`, `UserDepartment`, `Document`, `DocumentChunk`, `IngestJob`; constants `EMBED_DIM = 1536`, `STATUS_PENDING|READY|FAILED|ARCHIVED`, `JOB_QUEUED|RUNNING|SUCCEEDED|FAILED`, `SOURCE_UPLOAD|MANUAL`; `ChatSession.department_id: int | None`

**Gotcha to know before you start:** `ChatSession.department_id` is a `ForeignKey("departments.id")` given as a *string*. SQLAlchemy resolves it lazily, so any code that configures the `ChatSession` mapper must have `app.rag.models` imported first, or you get `NoReferencedTableError`. That is why `alembic/env.py` imports it and why the test below imports both modules.

- [ ] **Step 1: Add the pgvector dependency**

Append to `requirements.txt` after `python-multipart>=0.0.9`:

```
pgvector>=0.3.6
```

Then install it:

```bash
.venv/bin/pip install 'pgvector>=0.3.6'
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_rag_models.py`:

```python
"""Schema-shape tests for the RAG models. Pure metadata assertions — no DB.

These lock the two structural decisions that are easy to break silently: the
composite FK that makes a chunk's department provably its document's, and the
partial unique indexes whose WHERE clauses are the whole point.
"""

# Import order matters: ChatSession.department_id points at departments.id by
# name, so the rag tables must be registered before the mapper is configured.
from app.rag import models as rag
from app.history.models import ChatSession


def test_embed_dim_is_1536():
    assert rag.EMBED_DIM == 1536


def test_chunk_has_composite_fk_to_document_and_department():
    """The load-bearing invariant: a chunk cannot claim a foreign department."""
    fks = list(rag.DocumentChunk.__table__.foreign_key_constraints)
    composite = [fk for fk in fks if len(fk.columns) == 2]
    assert len(composite) == 1, "expected exactly one composite FK"
    fk = composite[0]
    assert {c.name for c in fk.columns} == {"document_id", "department_id"}
    assert {e.column.name for e in fk.elements} == {"id", "department_id"}
    assert fk.elements[0].column.table.name == "documents"
    assert fk.ondelete == "CASCADE"


def test_documents_expose_the_composite_fk_target():
    """documents needs UNIQUE(id, department_id) or the composite FK cannot exist."""
    uniques = {
        tuple(sorted(c.name for c in con.columns))
        for con in rag.Document.__table__.constraints
        if con.__class__.__name__ == "UniqueConstraint"
    }
    assert ("department_id", "id") in uniques


def test_documents_dedup_index_excludes_archived():
    idx = next(i for i in rag.Document.__table__.indexes
               if i.name == "ux_documents_active_content")
    assert idx.unique is True
    assert {c.name for c in idx.columns} == {"department_id", "content_hash"}
    where = str(idx.dialect_options["postgresql"]["where"])
    assert "archived" in where


def test_ingest_jobs_allow_only_one_active_job_per_document():
    idx = next(i for i in rag.IngestJob.__table__.indexes
               if i.name == "ux_ingest_jobs_active_document")
    assert idx.unique is True
    assert {c.name for c in idx.columns} == {"document_id"}
    where = str(idx.dialect_options["postgresql"]["where"])
    assert "queued" in where and "running" in where


def test_chunk_embedding_is_not_nullable_and_1536_wide():
    col = rag.DocumentChunk.__table__.c.embedding
    assert col.nullable is False
    assert col.type.dim == rag.EMBED_DIM


def test_tsv_is_a_stored_generated_column_using_english():
    col = rag.DocumentChunk.__table__.c.tsv
    assert col.computed is not None
    assert col.computed.persisted is True
    assert "english" in str(col.computed.sqltext)


def test_metadata_column_is_named_metadata_but_attribute_is_meta():
    """SQLAlchemy reserves `metadata` on the declarative class."""
    assert rag.Document.meta.property.columns[0].name == "metadata"
    assert rag.DocumentChunk.meta.property.columns[0].name == "metadata"


def test_department_deletes_are_restricted_not_nulled():
    """Deleting a department must never silently reclassify history."""
    doc_fk = next(fk for fk in rag.Document.__table__.c.department_id.foreign_keys)
    assert doc_fk.ondelete == "RESTRICT"
    sess_fk = next(fk for fk in ChatSession.__table__.c.department_id.foreign_keys)
    assert sess_fk.ondelete == "RESTRICT"


def test_chat_session_department_is_nullable_for_general_chat():
    assert ChatSession.__table__.c.department_id.nullable is True


def _checks(table):
    return {
        c.name: str(c.sqltext)
        for c in table.constraints
        if c.__class__.__name__ == "CheckConstraint"
    }


def test_status_vocabularies_are_closed_by_check_constraints():
    """The partial indexes key off exact strings — a typo'd status would match
    no predicate and silently bypass them."""
    doc = _checks(rag.Document.__table__)
    assert "archived" in doc["ck_documents_status"]
    assert "pending" in doc["ck_documents_status"]
    assert "upload" in doc["ck_documents_source"]

    job = _checks(rag.IngestJob.__table__)
    assert "queued" in job["ck_ingest_jobs_status"]
    assert "running" in job["ck_ingest_jobs_status"]


def test_document_stores_a_relative_storage_key_not_a_path():
    cols = rag.Document.__table__.c
    assert "storage_key" in cols
    assert "path" not in cols  # host-specific absolute paths are not portable


def test_vector_and_lexical_indexes_are_declared_on_the_model():
    """Declared so autogenerate never proposes dropping them, even though the
    migration creates them by hand."""
    by_name = {i.name: i for i in rag.DocumentChunk.__table__.indexes}
    hnsw = by_name["ix_chunks_embedding"]
    assert hnsw.dialect_options["postgresql"]["using"] == "hnsw"
    assert hnsw.dialect_options["postgresql"]["ops"] == {
        "embedding": "vector_cosine_ops"
    }
    assert by_name["ix_chunks_tsv"].dialect_options["postgresql"]["using"] == "gin"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_rag_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.rag'`

- [ ] **Step 4: Create the package marker**

Create `app/rag/__init__.py`:

```python
"""Department-scoped retrieval-augmented generation.

`models` holds the five tables; `context` the per-request department contextvar;
`access` the permission check; `repository` data access; `router` the admin API.
"""
```

- [ ] **Step 5: Write the models**

Create `app/rag/models.py`:

```python
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

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    department_id: Mapped[int] = mapped_column(
        ForeignKey("departments.id", ondelete="CASCADE"), primary_key=True
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
    # A RELATIVE key under RAG_DOCS_DIR (e.g. "hr/9f3c…​.pdf"), never a
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
```

- [ ] **Step 6: Bind the chat session to its department**

In `app/history/models.py`, add this field to `ChatSession` immediately after the `title` field (around line 47):

```python
    # Which department tab this conversation was opened in. NULL = general chat
    # (no RAG). RESTRICT rather than SET NULL: deleting a department must not
    # silently rewrite an old HR session into a general one — soft-disable
    # departments instead (departments.is_active).
    department_id: Mapped[int | None] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT"), nullable=True
    )
```

`ForeignKey` is already imported in that file — no import change needed.

- [ ] **Step 7: Let Alembic see the new tables, and stop it fighting the HNSW index**

In `alembic/env.py`, add after line 20 (`from app.files import models as _files_models  # noqa: F401`):

```python
from app.rag import models as _rag_models  # noqa: F401
```

Then add, just below `target_metadata = Base.metadata`:

```python
# Indexes whose PostgreSQL-specific options Alembic cannot round-trip: it does
# not reliably reflect an HNSW operator class or its WITH (m, ef_construction)
# build parameters, so a drift check would propose dropping and recreating them
# on every run. They ARE declared on the model (so autogenerate knows they
# exist) and created by hand in the migration; this only excludes them from
# comparison.
_AUTOGEN_SKIP_INDEXES = {"ix_chunks_embedding", "ix_chunks_tsv"}


def _include_object(obj, name, type_, reflected, compare_to) -> bool:
    if type_ == "index" and name in _AUTOGEN_SKIP_INDEXES:
        return False
    return True
```

And pass it to **both** `context.configure(...)` calls (in `run_migrations_offline` and `_do_run_migrations`), alongside the existing `compare_type=True`:

```python
        include_object=_include_object,
```

- [ ] **Step 8: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_rag_models.py -v`
Expected: PASS, 13 tests

- [ ] **Step 9: Confirm the only breakage is the expected one**

Run: `.venv/bin/pytest -q`

**Expected: the DB-backed integration tests now FAIL** with
`UndefinedColumnError: column chat_sessions.department_id does not exist`
(8 failures: `test_files_integration`, `test_history_integration`,
`test_excel_upload_integration`). This is correct and transient — the model
declares the column but Task 2's migration has not added it yet, and SQLAlchemy
emits it in every `SELECT` against `chat_sessions` regardless of nullability.

Verify the failures are *all* that error and nothing else:

```bash
.venv/bin/pytest -q 2>&1 | grep -c "department_id does not exist"
```

Anything failing for a different reason is a real regression — stop and fix it.
Task 2 clears these; do not proceed past Task 2 with them still red.

- [ ] **Step 10: Commit**

```bash
git add app/rag/__init__.py app/rag/models.py app/history/models.py \
        alembic/env.py requirements.txt tests/test_rag_models.py
git commit -m "feat(rag): department/document/chunk/ingest-job models + session department binding"
```

---

### Task 2: Alembic migration

**Files:**
- Create: `alembic/versions/<generated>_add_rag_tables.py`
- Test: `tests/test_rag_schema_integration.py`

**Interfaces:**
- Consumes: models from Task 1
- Produces: the migrated schema — `departments`, `user_departments`, `documents`, `document_chunks`, `ingest_jobs`, `chat_sessions.department_id`, the `vector` extension, and the HNSW + GIN indexes

**Why this migration is hand-written rather than autogenerated:** `CREATE EXTENSION vector` must run before any `vector(1536)` column exists, and neither the HNSW index (`USING hnsw ... WITH (m=..., ef_construction=...)`) nor its `vector_cosine_ops` operator class survives autogeneration. Generating a stamped empty revision and filling in the body is the reliable path.

- [ ] **Step 1: Ensure the extension is installable**

pgvector must be enabled by a superuser once per database:

```bash
PGPASSWORD=postgres psql -h 127.0.0.1 -U postgres -d local_ai_gateway \
  -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

Expected: `CREATE EXTENSION` (or a notice that it already exists). The migration repeats this idempotently so a fresh database also works when the app role has rights.

- [ ] **Step 2: Write the failing test**

Create `tests/test_rag_schema_integration.py`:

```python
"""Schema-invariant tests against real Postgres. Skips if the DB is unreachable.

These assert the things only the database can enforce — the composite FK, the
two partial unique indexes, and RESTRICT on department deletion. Getting any of
them wrong is silent until it matters.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import text

from app.db.session import engine

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

VEC = "[" + ",".join(["0.1"] * 1536) + "]"


def _run(coro):
    return asyncio.run(coro)


async def _fetch(sql, **params):
    async with engine.begin() as conn:
        return (await conn.execute(text(sql), params)).all()


def _skip_if_no_db():
    try:
        _run(_fetch("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - DB down -> skip
        pytest.skip(f"Postgres unreachable: {type(exc).__name__}")


@pytest.fixture()
def dept_pair():
    """Two departments plus one document in the first. Cleaned up after."""
    _skip_if_no_db()
    suffix = uuid.uuid4().hex[:8]
    doc_id = uuid.uuid4().hex
    state = {}

    async def setup():
        async with engine.begin() as conn:
            a = (await conn.execute(text(
                "INSERT INTO departments (code, name) VALUES (:c, 'A') RETURNING id"),
                {"c": f"a-{suffix}"})).scalar_one()
            b = (await conn.execute(text(
                "INSERT INTO departments (code, name) VALUES (:c, 'B') RETURNING id"),
                {"c": f"b-{suffix}"})).scalar_one()
            await conn.execute(text(
                "INSERT INTO documents (id, department_id, title, source, file_type,"
                " content_hash, status) VALUES (:i, :d, 'T', 'upload', 'pdf', :h, 'ready')"),
                {"i": doc_id, "d": a, "h": "h" * 64})
        return a, b

    state["a"], state["b"] = _run(setup())
    state["doc"] = doc_id
    yield state

    async def teardown():
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM documents WHERE department_id IN (:a,:b)"),
                               {"a": state["a"], "b": state["b"]})
            await conn.execute(text("DELETE FROM departments WHERE id IN (:a,:b)"),
                               {"a": state["a"], "b": state["b"]})
    _run(teardown())


def test_vector_extension_is_installed():
    _skip_if_no_db()
    rows = _run(_fetch("SELECT extname FROM pg_extension WHERE extname = 'vector'"))
    assert rows, "pgvector extension is not installed in this database"


def test_chunk_cannot_claim_a_foreign_department(dept_pair):
    """The composite FK: a chunk of an A-document may not be labelled B."""
    async def forge():
        async with engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO document_chunks (document_id, department_id, chunk_index,"
                " content, embedding) VALUES (:d, :dept, 0, 'forged', CAST(:v AS vector))"),
                {"d": dept_pair["doc"], "dept": dept_pair["b"], "v": VEC})

    with pytest.raises(Exception) as exc:
        _run(forge())
    assert "foreign key" in str(exc.value).lower()


def test_chunk_with_its_own_department_is_accepted(dept_pair):
    async def insert_ok():
        async with engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO document_chunks (document_id, department_id, chunk_index,"
                " content, embedding) VALUES (:d, :dept, 0, 'fine', CAST(:v AS vector))"),
                {"d": dept_pair["doc"], "dept": dept_pair["a"], "v": VEC})
            return (await conn.execute(text(
                "SELECT count(*) FROM document_chunks WHERE document_id = :d"),
                {"d": dept_pair["doc"]})).scalar_one()

    assert _run(insert_ok()) == 1


def test_same_content_hash_twice_in_one_department_is_rejected(dept_pair):
    async def dupe():
        async with engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO documents (id, department_id, title, source, file_type,"
                " content_hash, status) VALUES (:i, :d, 'T2', 'upload', 'pdf', :h, 'ready')"),
                {"i": uuid.uuid4().hex, "d": dept_pair["a"], "h": "h" * 64})

    with pytest.raises(Exception) as exc:
        _run(dupe())
    assert "ux_documents_active_content" in str(exc.value)


def test_archived_document_frees_its_content_hash_for_re_upload(dept_pair):
    """The reason the dedup index is partial rather than a plain UNIQUE."""
    new_id = uuid.uuid4().hex

    async def archive_then_readd():
        async with engine.begin() as conn:
            await conn.execute(text(
                "UPDATE documents SET status='archived' WHERE id = :i"),
                {"i": dept_pair["doc"]})
            await conn.execute(text(
                "INSERT INTO documents (id, department_id, title, source, file_type,"
                " content_hash, status) VALUES (:i, :d, 'again', 'upload', 'pdf', :h, 'ready')"),
                {"i": new_id, "d": dept_pair["a"], "h": "h" * 64})
            return (await conn.execute(text(
                "SELECT count(*) FROM documents WHERE department_id = :d"),
                {"d": dept_pair["a"]})).scalar_one()

    assert _run(archive_then_readd()) == 2  # archived original + fresh copy


def test_only_one_active_ingest_job_per_document(dept_pair):
    async def two_jobs():
        async with engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO ingest_jobs (id, document_id, status)"
                " VALUES (:i, :d, 'queued')"),
                {"i": uuid.uuid4().hex, "d": dept_pair["doc"]})
            await conn.execute(text(
                "INSERT INTO ingest_jobs (id, document_id, status)"
                " VALUES (:i, :d, 'running')"),
                {"i": uuid.uuid4().hex, "d": dept_pair["doc"]})

    with pytest.raises(Exception) as exc:
        _run(two_jobs())
    assert "ux_ingest_jobs_active_document" in str(exc.value)


def test_finished_jobs_do_not_block_a_new_one(dept_pair):
    async def finished_then_new():
        async with engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO ingest_jobs (id, document_id, status)"
                " VALUES (:i, :d, 'succeeded')"),
                {"i": uuid.uuid4().hex, "d": dept_pair["doc"]})
            await conn.execute(text(
                "INSERT INTO ingest_jobs (id, document_id, status)"
                " VALUES (:i, :d, 'queued')"),
                {"i": uuid.uuid4().hex, "d": dept_pair["doc"]})
            return (await conn.execute(text(
                "SELECT count(*) FROM ingest_jobs WHERE document_id = :d"),
                {"d": dept_pair["doc"]})).scalar_one()

    assert _run(finished_then_new()) == 2


def test_department_with_documents_cannot_be_deleted(dept_pair):
    async def drop():
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM departments WHERE id = :a"),
                               {"a": dept_pair["a"]})

    with pytest.raises(Exception) as exc:
        _run(drop())
    assert "foreign key" in str(exc.value).lower()


def test_chunk_tsv_is_populated_and_stems_english(dept_pair):
    async def insert_and_read():
        async with engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO document_chunks (document_id, department_id, chunk_index,"
                " content, embedding) VALUES (:d, :dept, 1, 'annual leave loans',"
                " CAST(:v AS vector))"),
                {"d": dept_pair["doc"], "dept": dept_pair["a"], "v": VEC})
            return (await conn.execute(text(
                "SELECT tsv::text FROM document_chunks WHERE document_id = :d"
                " AND chunk_index = 1"), {"d": dept_pair["doc"]})).scalar_one()

    tsv = _run(insert_and_read())
    assert "loan" in tsv and "loans" not in tsv  # 'english' stemmed it


def test_bad_document_status_is_rejected(dept_pair):
    """Without this CHECK, a typo'd status matches no partial-index predicate
    and silently escapes the dedup guarantee."""
    async def bad():
        async with engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO documents (id, department_id, title, source, file_type,"
                " content_hash, status) VALUES (:i, :d, 'T', 'upload', 'pdf', :h, 'archived_')"),
                {"i": uuid.uuid4().hex, "d": dept_pair["a"], "h": "z" * 64})

    with pytest.raises(Exception) as exc:
        _run(bad())
    assert "ck_documents_status" in str(exc.value)


def test_bad_ingest_status_cannot_bypass_the_active_job_index(dept_pair):
    """'runnning' would match neither the predicate nor the claim query."""
    async def bad():
        async with engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO ingest_jobs (id, document_id, status)"
                " VALUES (:i, :d, 'runnning')"),
                {"i": uuid.uuid4().hex, "d": dept_pair["doc"]})

    with pytest.raises(Exception) as exc:
        _run(bad())
    assert "ck_ingest_jobs_status" in str(exc.value)


def test_bad_document_source_is_rejected(dept_pair):
    async def bad():
        async with engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO documents (id, department_id, title, source, file_type,"
                " content_hash, status) VALUES (:i, :d, 'T', 'ftp', 'pdf', :h, 'ready')"),
                {"i": uuid.uuid4().hex, "d": dept_pair["a"], "h": "y" * 64})

    with pytest.raises(Exception) as exc:
        _run(bad())
    assert "ck_documents_source" in str(exc.value)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_rag_schema_integration.py -v`
Expected: FAIL — `relation "departments" does not exist` (or skips if Postgres is down; bring it up, this task needs it).

- [ ] **Step 4: Generate an empty stamped revision**

```bash
.venv/bin/alembic revision -m "add rag tables"
```

Note the generated filename and the `revision` / `down_revision` values it wrote. Keep them.

- [ ] **Step 5: Fill in the migration body**

Replace the `upgrade()` and `downgrade()` functions in the generated file with the following. **Keep the generated `revision` / `down_revision` lines exactly as Alembic wrote them.** Add the imports shown at the top.

```python
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

# revision identifiers -- LEAVE AS GENERATED
# revision: str = '...'
# down_revision: Union[str, None] = '...'


def upgrade() -> None:
    # Must precede any vector(1536) column.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "departments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )

    op.create_table(
        "user_departments",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("department_id", sa.Integer(), nullable=False),
        sa.Column("granted_by", sa.Integer(), nullable=True),
        sa.Column("granted_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["department_id"], ["departments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["granted_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("user_id", "department_id"),
    )

    op.create_table(
        "documents",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("department_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("file_type", sa.String(length=16), nullable=False),
        sa.Column("file_name", sa.String(length=255), nullable=True),
        # Relative key under RAG_DOCS_DIR, never an absolute host path.
        sa.Column("storage_key", sa.String(length=1024), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("embed_model", sa.String(length=128), nullable=True),
        sa.Column("embed_dim", sa.Integer(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("uploaded_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["department_id"], ["departments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["uploaded_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        # FK target for the chunk composite FK below.
        sa.UniqueConstraint("id", "department_id", name="uq_documents_id_department"),
        # Closed vocabularies: ux_documents_active_content's predicate keys off
        # the exact string 'archived'.
        sa.CheckConstraint(
            "status IN ('pending', 'ready', 'failed', 'archived')",
            name="ck_documents_status",
        ),
        sa.CheckConstraint(
            "source IN ('upload', 'manual')", name="ck_documents_source"
        ),
    )
    op.create_index("ix_documents_department", "documents", ["department_id"])
    op.create_index("ix_documents_status", "documents", ["status"])
    # Partial: archived rows are retained for audit but must not block re-upload.
    op.create_index(
        "ux_documents_active_content", "documents", ["department_id", "content_hash"],
        unique=True, postgresql_where=sa.text("status <> 'archived'"),
    )

    op.create_table(
        "document_chunks",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("document_id", sa.String(length=32), nullable=False),
        sa.Column("department_id", sa.Integer(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(1536), nullable=False),
        sa.Column("tsv", postgresql.TSVECTOR(),
                  sa.Computed("to_tsvector('english', content)", persisted=True), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=True),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("section", sa.String(length=512), nullable=True),
        sa.Column("element_type", sa.String(length=32), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "chunk_index", name="uq_document_chunks_doc_index"),
        # The invariant: a chunk's department is provably its document's.
        sa.ForeignKeyConstraint(
            ["document_id", "department_id"],
            ["documents.id", "documents.department_id"],
            ondelete="CASCADE",
            name="fk_document_chunks_document_department",
        ),
    )
    op.create_index("ix_chunks_department", "document_chunks", ["department_id"])
    # Raw SQL: the operator class and the WITH options do not survive autogenerate.
    op.execute(
        "CREATE INDEX ix_chunks_embedding ON document_chunks "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )
    op.execute("CREATE INDEX ix_chunks_tsv ON document_chunks USING gin (tsv)")

    op.create_table(
        "ingest_jobs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("document_id", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="queued", nullable=False),
        sa.Column("chunks_total", sa.Integer(), nullable=True),
        sa.Column("chunks_done", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # Load-bearing: a typo'd status would match neither the partial index
        # predicate nor the worker's claim query, bypassing both.
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_ingest_jobs_status",
        ),
    )
    op.create_index("ix_ingest_jobs_status", "ingest_jobs", ["status", "created_at"])
    # One ACTIVE job per document (SKIP LOCKED only guards a single row).
    op.create_index(
        "ux_ingest_jobs_active_document", "ingest_jobs", ["document_id"],
        unique=True, postgresql_where=sa.text("status IN ('queued', 'running')"),
    )

    # Bind a conversation to the tab it was opened in. NULL = general chat.
    op.add_column("chat_sessions", sa.Column("department_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_chat_sessions_department", "chat_sessions", "departments",
        ["department_id"], ["id"], ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint("fk_chat_sessions_department", "chat_sessions", type_="foreignkey")
    op.drop_column("chat_sessions", "department_id")
    op.drop_index("ux_ingest_jobs_active_document", table_name="ingest_jobs")
    op.drop_index("ix_ingest_jobs_status", table_name="ingest_jobs")
    op.drop_table("ingest_jobs")
    op.execute("DROP INDEX IF EXISTS ix_chunks_tsv")
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding")
    op.drop_index("ix_chunks_department", table_name="document_chunks")
    op.drop_table("document_chunks")
    op.drop_index("ux_documents_active_content", table_name="documents")
    op.drop_index("ix_documents_status", table_name="documents")
    op.drop_index("ix_documents_department", table_name="documents")
    op.drop_table("documents")
    op.drop_table("user_departments")
    op.drop_table("departments")
    # The extension is left installed: other databases/objects may rely on it.
```

- [ ] **Step 6: Apply the migration**

```bash
.venv/bin/alembic upgrade head
```

Expected: `Running upgrade ... -> <rev>, add rag tables` with no error.

- [ ] **Step 7: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_rag_schema_integration.py -v`
Expected: PASS, 12 tests

- [ ] **Step 8: Verify the migration is reversible**

```bash
.venv/bin/alembic downgrade -1 && .venv/bin/alembic upgrade head
```

Expected: both succeed. A migration that cannot round-trip is a migration you cannot back out under pressure.

- [ ] **Step 9: Confirm autogenerate sees no drift**

```bash
.venv/bin/alembic revision --autogenerate -m "drift check"
```

Open the generated file: `upgrade()` must be **empty** (just the autogenerate comments). Two things make that achievable, both done in Task 1 Step 7 — the HNSW and GIN indexes are declared on `DocumentChunk.__table_args__` so autogenerate knows they exist, and `_include_object` in `alembic/env.py` excludes those two names from comparison because Alembic cannot reflect an HNSW opclass or its build options.

If `upgrade()` is **not** empty, read what it proposes before changing anything:

- `drop_index`/`create_index` on `ix_chunks_embedding` or `ix_chunks_tsv` → `_include_object` is not wired into both `context.configure(...)` calls. Fix env.py, don't touch the migration.
- Anything else (a column, a constraint, another index) → the models and the migration genuinely disagree. Fix whichever is wrong.

Then delete the drift-check file:

```bash
rm alembic/versions/*drift_check.py
```

- [ ] **Step 10: Commit**

```bash
git add alembic/versions/ tests/test_rag_schema_integration.py
git commit -m "feat(rag): migration for rag tables, pgvector extension, hnsw+gin indexes"
```

---

### Task 3: Department contextvar

**Files:**
- Create: `app/rag/context.py`
- Test: `tests/test_rag_context.py`

**Interfaces:**
- Consumes: nothing
- Produces: `DepartmentContext` (frozen dataclass: `id: int`, `code: str`), `rag_context(ctx: DepartmentContext) -> ContextManager[None]`, `current_department() -> DepartmentContext | None`

This mirrors `app/files/store.py`'s `file_sink`/`file_source` exactly, for the same reason: the value must reach the tool without being a tool parameter the model can set.

- [ ] **Step 1: Write the failing test**

Create `tests/test_rag_context.py`:

```python
"""The department contextvar. Pure — no DB, no app.

This is the mechanism that keeps `department` out of the tool schema: the tool
reads it from the context, so a prompt injection has nothing to target.
"""

import asyncio
import dataclasses

import pytest

from app.rag.context import DepartmentContext, current_department, rag_context

HR = DepartmentContext(id=1, code="hr")
FIN = DepartmentContext(id=2, code="finance")


def test_no_context_by_default():
    assert current_department() is None


def test_context_is_visible_inside_the_block():
    with rag_context(HR):
        assert current_department() == HR


def test_context_is_cleared_on_exit():
    with rag_context(HR):
        pass
    assert current_department() is None


def test_context_is_cleared_even_when_the_block_raises():
    try:
        with rag_context(HR):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert current_department() is None


def test_nested_contexts_restore_the_outer_value():
    with rag_context(HR):
        with rag_context(FIN):
            assert current_department() == FIN
        assert current_department() == HR


def test_context_does_not_leak_between_concurrent_tasks():
    """Two turns in one process must not see each other's department."""
    seen = {}

    async def turn(ctx, key):
        with rag_context(ctx):
            await asyncio.sleep(0)  # force interleaving
            seen[key] = current_department()

    async def main():
        await asyncio.gather(turn(HR, "a"), turn(FIN, "b"))

    asyncio.run(main())
    assert seen == {"a": HR, "b": FIN}


def test_department_context_is_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        HR.id = 99
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_rag_context.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.rag.context'`

- [ ] **Step 3: Write the implementation**

Create `app/rag/context.py`:

```python
"""The department in effect for the current request.

Same mechanism as `app/files/store.py`'s file sink/source, and for the same
reason: retrieval must be scoped to a department that the *model* cannot choose.
The tool reads it from here, so `search_department_docs` has no department
parameter and a prompt injection has nothing to target.

Streaming gotcha, inherited from the file sink: for a streamed turn this MUST be
installed INSIDE the async generator Starlette iterates, not merely in the router
before returning the StreamingResponse — otherwise it is invisible while the
agent loop runs.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class DepartmentContext:
    """The authorized department for this turn. Frozen so nothing downstream can
    quietly repoint it after `resolve_department` has vetted it."""

    id: int
    code: str


# None -> no department (general chat); retrieval tools refuse to run.
_current: ContextVar[DepartmentContext | None] = ContextVar(
    "current_department", default=None
)


@contextmanager
def rag_context(ctx: DepartmentContext) -> Iterator[None]:
    """Install `ctx` as the active department for the enclosed block."""
    token = _current.set(ctx)
    try:
        yield
    finally:
        _current.reset(token)


def current_department() -> DepartmentContext | None:
    """The active department, or None outside a department-scoped turn."""
    return _current.get()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_rag_context.py -v`
Expected: PASS, 7 tests

- [ ] **Step 5: Commit**

```bash
git add app/rag/context.py tests/test_rag_context.py
git commit -m "feat(rag): department contextvar, mirroring the file sink pattern"
```

---

### Task 4: Repository

**Files:**
- Create: `app/rag/repository.py`
- Test: `tests/test_rag_repository_integration.py`

**Interfaces:**
- Consumes: `Department`, `UserDepartment` from `app.rag.models`
- Produces, all `async` and all taking `session: AsyncSession` first, none committing:
  - `create_department(session, *, code: str, name: str) -> Department`
  - `get_department_by_code(session, code: str) -> Department | None`
  - `list_departments(session) -> list[Department]`
  - `list_departments_for_user(session, user_id: int) -> list[Department]`
  - `set_department_active(session, *, code: str, is_active: bool) -> Department | None`
  - `grant_department(session, *, user_id: int, department_id: int, granted_by: int | None) -> None`
  - `revoke_department(session, *, user_id: int, department_id: int) -> bool`
  - `has_department_access(session, *, user_id: int, department_id: int) -> bool`
  - `list_department_members(session, department_id: int) -> list[UserDepartment]`

- [ ] **Step 1: Write the failing test**

Create `tests/test_rag_repository_integration.py`:

```python
"""Repository tests against real Postgres. Skips if the DB is unreachable."""

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.session import engine
from app.rag import repository as repo

Session = async_sessionmaker(engine, expire_on_commit=False)


def _run(coro):
    return asyncio.run(coro)


def _skip_if_no_db():
    async def ping():
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
    try:
        _run(ping())
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres unreachable: {type(exc).__name__}")


@pytest.fixture()
def user_id():
    """A throwaway user row; removed afterwards."""
    _skip_if_no_db()
    email = f"rag-repo-{uuid.uuid4().hex[:8]}@example.com"

    async def make():
        async with engine.begin() as conn:
            return (await conn.execute(text(
                "INSERT INTO users (email, auth_provider, role, is_active)"
                " VALUES (:e, 'local', 'member', true) RETURNING id"),
                {"e": email})).scalar_one()

    uid = _run(make())
    yield uid

    async def drop():
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM users WHERE id = :i"), {"i": uid})
    _run(drop())


@pytest.fixture()
def codes():
    """Unique department codes, with cleanup."""
    _skip_if_no_db()
    made = [f"d{uuid.uuid4().hex[:8]}", f"d{uuid.uuid4().hex[:8]}"]
    yield made

    async def drop():
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM departments WHERE code = ANY(:c)"),
                               {"c": made})
    _run(drop())


def test_create_and_fetch_by_code(codes):
    async def go():
        async with Session() as s:
            await repo.create_department(s, code=codes[0], name="HR")
            await s.commit()
        async with Session() as s:
            return await repo.get_department_by_code(s, codes[0])

    dept = _run(go())
    assert dept is not None and dept.name == "HR" and dept.is_active is True


def test_unknown_code_is_none(codes):
    async def go():
        async with Session() as s:
            return await repo.get_department_by_code(s, "no-such-code-xyz")
    assert _run(go()) is None


def test_access_is_denied_until_granted(codes, user_id):
    async def go():
        async with Session() as s:
            d = await repo.create_department(s, code=codes[0], name="HR")
            await s.commit()
            before = await repo.has_department_access(
                s, user_id=user_id, department_id=d.id)
            await repo.grant_department(
                s, user_id=user_id, department_id=d.id, granted_by=None)
            await s.commit()
            after = await repo.has_department_access(
                s, user_id=user_id, department_id=d.id)
            return before, after

    before, after = _run(go())
    assert before is False
    assert after is True


def test_grant_is_idempotent(codes, user_id):
    """Re-granting must not raise on the composite PK."""
    async def go():
        async with Session() as s:
            d = await repo.create_department(s, code=codes[0], name="HR")
            await s.commit()
            await repo.grant_department(
                s, user_id=user_id, department_id=d.id, granted_by=None)
            await s.commit()
            await repo.grant_department(
                s, user_id=user_id, department_id=d.id, granted_by=None)
            await s.commit()
            return len(await repo.list_department_members(s, d.id))

    assert _run(go()) == 1


def test_revoke_removes_access_and_reports_whether_it_did(codes, user_id):
    async def go():
        async with Session() as s:
            d = await repo.create_department(s, code=codes[0], name="HR")
            await s.commit()
            await repo.grant_department(
                s, user_id=user_id, department_id=d.id, granted_by=None)
            await s.commit()
            first = await repo.revoke_department(s, user_id=user_id, department_id=d.id)
            await s.commit()
            second = await repo.revoke_department(s, user_id=user_id, department_id=d.id)
            await s.commit()
            access = await repo.has_department_access(
                s, user_id=user_id, department_id=d.id)
            return first, second, access

    first, second, access = _run(go())
    assert first is True
    assert second is False   # nothing left to revoke
    assert access is False


def test_list_for_user_returns_only_granted_and_active(codes, user_id):
    async def go():
        async with Session() as s:
            granted = await repo.create_department(s, code=codes[0], name="HR")
            await repo.create_department(s, code=codes[1], name="Finance")
            await s.commit()
            await repo.grant_department(
                s, user_id=user_id, department_id=granted.id, granted_by=None)
            await s.commit()
            visible = await repo.list_departments_for_user(s, user_id)
            names = [d.code for d in visible]
            # Now soft-disable the granted one: it must disappear.
            await repo.set_department_active(s, code=codes[0], is_active=False)
            await s.commit()
            after = [d.code for d in await repo.list_departments_for_user(s, user_id)]
            return names, after

    names, after = _run(go())
    assert names == [codes[0]]          # granted only, not codes[1]
    assert after == []                  # inactive departments are hidden


def test_set_active_on_unknown_code_returns_none(codes):
    async def go():
        async with Session() as s:
            return await repo.set_department_active(
                s, code="no-such-code-xyz", is_active=False)
    assert _run(go()) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_rag_repository_integration.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.rag.repository'`

- [ ] **Step 3: Write the implementation**

Create `app/rag/repository.py`:

```python
"""Data-access for departments and department grants.

Same convention as `history/repository.py` and `files/repository.py`: every
function takes an AsyncSession and DOES NOT commit — the router owns the
transaction boundary.
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Department, UserDepartment


async def create_department(
    session: AsyncSession, *, code: str, name: str
) -> Department:
    """Insert a department. `code` is unique; a duplicate raises IntegrityError
    and the router turns that into 409."""
    dept = Department(code=code, name=name)
    session.add(dept)
    await session.flush()
    return dept


async def get_department_by_code(
    session: AsyncSession, code: str
) -> Department | None:
    return (
        await session.execute(select(Department).where(Department.code == code))
    ).scalar_one_or_none()


async def list_departments(session: AsyncSession) -> list[Department]:
    """Every department, active or not — the admin view."""
    result = await session.execute(select(Department).order_by(Department.code))
    return list(result.scalars())


async def list_departments_for_user(
    session: AsyncSession, user_id: int
) -> list[Department]:
    """The departments this user may query: granted AND active.

    This is what the frontend renders as tabs. Inactive departments disappear
    from the UI without any grant being revoked, which is the point of
    soft-disable — departments can never be deleted (ON DELETE RESTRICT).
    """
    result = await session.execute(
        select(Department)
        .join(UserDepartment, UserDepartment.department_id == Department.id)
        .where(UserDepartment.user_id == user_id, Department.is_active.is_(True))
        .order_by(Department.code)
    )
    return list(result.scalars())


async def set_department_active(
    session: AsyncSession, *, code: str, is_active: bool
) -> Department | None:
    """Soft-enable/disable. Returns None if the code is unknown."""
    dept = await get_department_by_code(session, code)
    if dept is None:
        return None
    dept.is_active = is_active
    await session.flush()
    return dept


async def grant_department(
    session: AsyncSession, *, user_id: int, department_id: int,
    granted_by: int | None,
) -> None:
    """Grant access. Idempotent: re-granting is a no-op rather than a PK error,
    so an admin clicking twice does not produce a 500."""
    stmt = (
        pg_insert(UserDepartment)
        .values(
            user_id=user_id, department_id=department_id, granted_by=granted_by
        )
        .on_conflict_do_nothing(index_elements=["user_id", "department_id"])
    )
    await session.execute(stmt)


async def revoke_department(
    session: AsyncSession, *, user_id: int, department_id: int
) -> bool:
    """Remove access. Returns True if a grant was actually removed."""
    result = await session.execute(
        delete(UserDepartment).where(
            UserDepartment.user_id == user_id,
            UserDepartment.department_id == department_id,
        )
    )
    return result.rowcount > 0


async def has_department_access(
    session: AsyncSession, *, user_id: int, department_id: int
) -> bool:
    """The permission check. Absence of a row = no access."""
    found = (
        await session.execute(
            select(UserDepartment.user_id).where(
                UserDepartment.user_id == user_id,
                UserDepartment.department_id == department_id,
            )
        )
    ).first()
    return found is not None


async def list_department_members(
    session: AsyncSession, department_id: int
) -> list[UserDepartment]:
    result = await session.execute(
        select(UserDepartment)
        .where(UserDepartment.department_id == department_id)
        .order_by(UserDepartment.user_id)
    )
    return list(result.scalars())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_rag_repository_integration.py -v`
Expected: PASS, 7 tests

- [ ] **Step 5: Commit**

```bash
git add app/rag/repository.py tests/test_rag_repository_integration.py
git commit -m "feat(rag): department + grant repository"
```

---

### Task 5: `resolve_department` — the access boundary

**Files:**
- Create: `app/rag/access.py`
- Test: `tests/test_rag_access_integration.py`

**Interfaces:**
- Consumes: `app.rag.repository`, `app.rag.context.DepartmentContext`, `app.users.models.User`, `app.history.models.ChatSession`
- Produces: `async resolve_department(session: AsyncSession, user: User, code: str | None, chat_session: ChatSession | None) -> DepartmentContext | None`, raising `fastapi.HTTPException`

**Contract — every row is a security assertion:**

| `chat_session` | `code` | Result |
|---|---|---|
| `None` (new session) | `None` | `None` — general chat |
| `None` (new session) | given | `DepartmentContext` (subject to 404/403 below) |
| existing, `department_id is None` | `None` | `None` — stays general |
| existing, `department_id is None` | given | **409** — a general transcript must not become a department transcript |
| existing, `department_id = X` | `None` | **400** — a bound session cannot be continued without its department |
| existing, `department_id = X` | `≠ X` | **409** |
| existing, `department_id = X` | `= X` | `DepartmentContext` |
| any | unknown or inactive | **404** |
| any | no grant, non-admin | **403** |
| `chat_session.user_id != user.id` | any | **404** — not yours, and its existence is not confirmed |

**The distinction between "no session" and "session whose department is NULL" is the whole point of rows 2 and 4** — both give `department_id is None`, and collapsing them lets an existing general conversation be silently reclassified as HR on turn five.

Admins bypass the grant check **only**. They do not bypass 404 (inactive is gone for everyone), the session-binding rules, or the ownership check.

**Ownership is verified here rather than assumed.** Callers are expected to load the session through an owner-scoped history lookup, but `resolve_department` re-checks `chat_session.user_id == user.id` anyway: this is a security boundary, and a boundary that depends on every caller having done the right thing upstream is one refactor away from a hole.

- [ ] **Step 1: Write the failing test**

Create `tests/test_rag_access_integration.py`:

```python
"""resolve_department — the department permission boundary.

Every branch here is a security assertion. Skips if Postgres is unreachable.
"""

import asyncio
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.session import engine
from app.history.models import ChatSession
from app.rag import repository as repo
from app.rag.access import resolve_department
from app.users.models import ROLE_ADMIN, ROLE_MEMBER, User

Session = async_sessionmaker(engine, expire_on_commit=False)


def _run(coro):
    return asyncio.run(coro)


def _skip_if_no_db():
    async def ping():
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
    try:
        _run(ping())
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres unreachable: {type(exc).__name__}")


@pytest.fixture()
def env():
    """Two departments (one inactive), a member with a grant to the first,
    and an admin with no grants at all."""
    _skip_if_no_db()
    tag = uuid.uuid4().hex[:8]
    state = {"hr_code": f"hr{tag}", "fin_code": f"fin{tag}", "off_code": f"off{tag}"}

    async def setup():
        async with Session() as s:
            hr = await repo.create_department(s, code=state["hr_code"], name="HR")
            fin = await repo.create_department(s, code=state["fin_code"], name="Fin")
            off = await repo.create_department(s, code=state["off_code"], name="Off")
            off.is_active = False
            await s.flush()
            member = User(email=f"m{tag}@example.com", auth_provider="local",
                          role=ROLE_MEMBER, is_active=True)
            admin = User(email=f"a{tag}@example.com", auth_provider="local",
                         role=ROLE_ADMIN, is_active=True)
            s.add_all([member, admin])
            await s.flush()
            await repo.grant_department(
                s, user_id=member.id, department_id=hr.id, granted_by=None)
            await s.commit()
            return {"hr": hr.id, "fin": fin.id, "off": off.id,
                    "member": member, "admin": admin}

    state.update(_run(setup()))
    yield state

    async def teardown():
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM users WHERE id IN (:m,:a)"),
                               {"m": state["member"].id, "a": state["admin"].id})
            await conn.execute(text("DELETE FROM departments WHERE id IN (:h,:f,:o)"),
                               {"h": state["hr"], "f": state["fin"], "o": state["off"]})
    _run(teardown())


def _resolve(user, code, chat_session=None):
    async def go():
        async with Session() as s:
            return await resolve_department(s, user, code, chat_session)
    return _run(go())


def test_no_code_and_no_session_department_is_general_chat(env):
    assert _resolve(env["member"], None, None) is None


def test_granted_department_resolves(env):
    ctx = _resolve(env["member"], env["hr_code"], None)
    assert ctx is not None
    assert ctx.id == env["hr"] and ctx.code == env["hr_code"]


def test_ungranted_department_is_403(env):
    with pytest.raises(HTTPException) as exc:
        _resolve(env["member"], env["fin_code"], None)
    assert exc.value.status_code == 403


def test_unknown_department_is_404(env):
    with pytest.raises(HTTPException) as exc:
        _resolve(env["member"], "no-such-department", None)
    assert exc.value.status_code == 404


def test_inactive_department_is_404_even_for_admin(env):
    """Soft-disabled means gone from the product, for everyone."""
    with pytest.raises(HTTPException) as exc:
        _resolve(env["admin"], env["off_code"], None)
    assert exc.value.status_code == 404


def test_admin_bypasses_the_grant_check(env):
    """The admin holds no grant to Finance and still resolves it."""
    ctx = _resolve(env["admin"], env["fin_code"], None)
    assert ctx is not None and ctx.id == env["fin"]


def test_session_bound_to_another_department_is_409(env):
    """An HR session must not be continued as Finance on a later turn."""
    bound = ChatSession(id=uuid.uuid4().hex, user_id=env["member"].id,
                        department_id=env["fin"])
    with pytest.raises(HTTPException) as exc:
        _resolve(env["member"], env["hr_code"], bound)
    assert exc.value.status_code == 409


def test_matching_session_department_resolves(env):
    bound = ChatSession(id=uuid.uuid4().hex, user_id=env["member"].id,
                        department_id=env["hr"])
    ctx = _resolve(env["member"], env["hr_code"], bound)
    assert ctx is not None and ctx.id == env["hr"]


def test_bound_session_cannot_be_continued_without_a_code(env):
    """Omitting `department` must not silently downgrade an HR chat to general."""
    bound = ChatSession(id=uuid.uuid4().hex, user_id=env["member"].id,
                        department_id=env["hr"])
    with pytest.raises(HTTPException) as exc:
        _resolve(env["member"], None, bound)
    assert exc.value.status_code == 400


def test_general_session_stays_general(env):
    bound = ChatSession(id=uuid.uuid4().hex, user_id=env["member"].id,
                        department_id=None)
    assert _resolve(env["member"], None, bound) is None


def test_existing_general_session_cannot_be_adopted_into_a_department(env):
    """The hole this closes: every prior turn in a general chat was answered
    without departmental grounding, so relabelling the thread HR would
    misrepresent all of them. A new chat is the only way in."""
    general = ChatSession(id=uuid.uuid4().hex, user_id=env["member"].id,
                          department_id=None)
    with pytest.raises(HTTPException) as exc:
        _resolve(env["member"], env["hr_code"], general)
    assert exc.value.status_code == 409


def test_a_brand_new_session_may_be_given_a_department(env):
    """The counterpart: chat_session=None is a NEW session, not an existing
    general one, and must still be allowed to open in a department."""
    ctx = _resolve(env["member"], env["hr_code"], None)
    assert ctx is not None and ctx.id == env["hr"]


def test_session_belonging_to_another_user_is_404(env):
    """Ownership is re-checked here rather than assumed of the caller. 404, not
    403 — a foreign session id must not be confirmed to exist."""
    foreign = ChatSession(id=uuid.uuid4().hex, user_id=env["admin"].id,
                          department_id=env["hr"])
    with pytest.raises(HTTPException) as exc:
        _resolve(env["member"], env["hr_code"], foreign)
    assert exc.value.status_code == 404


def test_ownership_is_checked_before_the_department_is_even_looked_up(env):
    """A foreign session with a nonsense code still 404s on ownership — the
    check must not be reachable-around by varying the code."""
    foreign = ChatSession(id=uuid.uuid4().hex, user_id=env["admin"].id,
                          department_id=None)
    with pytest.raises(HTTPException) as exc:
        _resolve(env["member"], "no-such-department", foreign)
    assert exc.value.status_code == 404
    assert "Session" in exc.value.detail
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_rag_access_integration.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.rag.access'`

- [ ] **Step 3: Write the implementation**

Create `app/rag/access.py`:

```python
"""The department permission boundary.

The security invariant, stated precisely: **`department_id` is derived from the
authorized department context — it is not trusted directly from the request
body.** The tab code DOES originate in the request; it becomes trusted only
after this function validates it against `user_departments` and against the
session's own department.

Everything downstream (the contextvar, the retrieval SQL) may assume the
department it receives has already passed through here.
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..history.models import ChatSession
from ..users.models import ROLE_ADMIN, User
from . import repository as repo
from .context import DepartmentContext


async def resolve_department(
    session: AsyncSession,
    user: User,
    code: str | None,
    chat_session: ChatSession | None,
) -> DepartmentContext | None:
    """Validate a request-supplied department code for this user and session.

    Returns None for general chat (no department, no RAG). Raises HTTPException
    on every rejection so callers never have to interpret a falsy result.
    """
    # Callers are expected to have loaded the session through an owner-scoped
    # lookup. Re-check anyway — a boundary that assumes every caller did the
    # right thing upstream is one refactor away from a hole. 404 rather than
    # 403, matching GET /v1/files/{id}: don't confirm that a foreign id exists.
    if chat_session is not None and chat_session.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Session not found"
        )

    # A brand-new session (None) is NOT the same as an existing session whose
    # department is NULL. Both have no department id, but only the first may be
    # given one — see `is_existing` below.
    is_existing = chat_session is not None
    bound_id = chat_session.department_id if is_existing else None

    if code is None:
        # A session opened in a department tab cannot be continued without it —
        # otherwise omitting the field silently downgrades an HR conversation to
        # general chat, and the transcript stops meaning what it says.
        if bound_id is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This conversation belongs to a department; "
                       "'department' is required to continue it.",
            )
        return None

    dept = await repo.get_department_by_code(session, code)
    # Inactive is 404, not 403, and for admins too: soft-disable means the
    # department is gone from the product, and 403 would confirm it still exists.
    if dept is None or not dept.is_active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Unknown department",
        )

    # Admins bypass the grant check ONLY. They do not bypass 404, the ownership
    # check above, or the session-binding checks below.
    if user.role != ROLE_ADMIN:
        allowed = await repo.has_department_access(
            session, user_id=user.id, department_id=dept.id
        )
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have access to this department",
            )

    if is_existing and bound_id is None:
        # An existing GENERAL conversation cannot be adopted into a department:
        # every prior turn was answered without departmental grounding, and
        # relabelling the thread would misrepresent all of them.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This conversation is not a department conversation; "
                   "start a new chat in the department tab.",
        )

    if bound_id is not None and bound_id != dept.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This conversation belongs to a different department",
        )

    return DepartmentContext(id=dept.id, code=dept.code)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_rag_access_integration.py -v`
Expected: PASS, 14 tests

- [ ] **Step 5: Commit**

```bash
git add app/rag/access.py tests/test_rag_access_integration.py
git commit -m "feat(rag): resolve_department access boundary (403/404/409/400 contract)"
```

---

### Task 6: Admin API

**Files:**
- Create: `app/rag/schemas.py`
- Create: `app/rag/router.py`
- Modify: `app/main.py` (import + `include_router`)
- Test: `tests/test_rag_departments_api.py`

**Interfaces:**
- Consumes: `app.rag.repository`, `app.auth.dependencies.get_current_user` / `require_admin`, `app.db.session.get_session`
- Produces these routes:

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/v1/departments` | admin | create (409 on duplicate code) |
| `GET` | `/v1/departments` | any | admin → all; member → granted **and** active (the frontend's tabs) |
| `PATCH` | `/v1/departments/{code}` | admin | rename / soft-disable |
| `GET` | `/v1/departments/{code}/members` | admin | list grants |
| `POST` | `/v1/departments/{code}/members` | admin | grant (idempotent, 204) |
| `DELETE` | `/v1/departments/{code}/members/{user_id}` | admin | revoke (204; 404 if no grant) |

- [ ] **Step 1: Write the failing test**

Create `tests/test_rag_departments_api.py`:

```python
"""Department admin API. Real Postgres + TestClient; skips if the DB is down.

Follows the auth/skip pattern in tests/test_files_integration.py.
"""

import uuid

import pytest
from starlette.testclient import TestClient

from app.main import app

PASSWORD = "supersecret123"


def _auth(client, email):
    err = resp = None
    try:
        client.post("/auth/register", json={"email": email, "password": PASSWORD})
        resp = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    except Exception as exc:  # noqa: BLE001 - DB down -> skip
        err = exc
    if err is not None:
        pytest.skip(f"Postgres unreachable: {type(err).__name__}")
    if resp.status_code != 200:
        pytest.skip(f"auth failed (login -> {resp.status_code})")
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _me(client, headers):
    return client.get("/users/me", headers=headers).json()


@pytest.fixture()
def clients():
    """An admin (the project's seeded admin) and a fresh member."""
    with TestClient(app) as client:
        admin = _auth(client, "admin@example.com")
        if _me(client, admin).get("role") != "admin":
            pytest.skip("admin@example.com is not an admin in this database")
        member_email = f"rag-member-{uuid.uuid4().hex[:8]}@example.com"
        member = _auth(client, member_email)
        yield client, admin, member, _me(client, member)["id"]


def test_member_cannot_create_a_department(clients):
    client, _admin, member, _uid = clients
    resp = client.post("/v1/departments",
                       json={"code": f"x{uuid.uuid4().hex[:6]}", "name": "X"},
                       headers=member)
    assert resp.status_code == 403


def test_admin_creates_and_duplicate_code_is_409(clients):
    client, admin, _member, _uid = clients
    code = f"hr{uuid.uuid4().hex[:6]}"
    first = client.post("/v1/departments", json={"code": code, "name": "HR"},
                        headers=admin)
    assert first.status_code == 201
    assert first.json()["code"] == code and first.json()["is_active"] is True

    dupe = client.post("/v1/departments", json={"code": code, "name": "HR again"},
                       headers=admin)
    assert dupe.status_code == 409


def test_member_sees_only_granted_active_departments(clients):
    client, admin, member, uid = clients
    granted = f"g{uuid.uuid4().hex[:6]}"
    ungranted = f"u{uuid.uuid4().hex[:6]}"
    client.post("/v1/departments", json={"code": granted, "name": "G"}, headers=admin)
    client.post("/v1/departments", json={"code": ungranted, "name": "U"}, headers=admin)

    # Before any grant: neither is visible.
    assert granted not in [d["code"] for d in
                           client.get("/v1/departments", headers=member).json()]

    assert client.post(f"/v1/departments/{granted}/members",
                       json={"user_id": uid}, headers=admin).status_code == 204

    visible = [d["code"] for d in client.get("/v1/departments", headers=member).json()]
    assert granted in visible
    assert ungranted not in visible


def test_soft_disable_hides_a_department_without_revoking_the_grant(clients):
    client, admin, member, uid = clients
    code = f"s{uuid.uuid4().hex[:6]}"
    client.post("/v1/departments", json={"code": code, "name": "S"}, headers=admin)
    client.post(f"/v1/departments/{code}/members", json={"user_id": uid}, headers=admin)
    assert code in [d["code"] for d in
                    client.get("/v1/departments", headers=member).json()]

    patched = client.patch(f"/v1/departments/{code}", json={"is_active": False},
                           headers=admin)
    assert patched.status_code == 200 and patched.json()["is_active"] is False
    assert code not in [d["code"] for d in
                        client.get("/v1/departments", headers=member).json()]

    # The grant survives — the admin listing still shows the member.
    members = client.get(f"/v1/departments/{code}/members", headers=admin).json()
    assert uid in [m["user_id"] for m in members]


def test_granting_twice_is_idempotent(clients):
    client, admin, _member, uid = clients
    code = f"i{uuid.uuid4().hex[:6]}"
    client.post("/v1/departments", json={"code": code, "name": "I"}, headers=admin)
    for _ in range(2):
        assert client.post(f"/v1/departments/{code}/members",
                           json={"user_id": uid}, headers=admin).status_code == 204
    members = client.get(f"/v1/departments/{code}/members", headers=admin).json()
    assert len([m for m in members if m["user_id"] == uid]) == 1


def test_revoke_removes_then_404s(clients):
    client, admin, _member, uid = clients
    code = f"r{uuid.uuid4().hex[:6]}"
    client.post("/v1/departments", json={"code": code, "name": "R"}, headers=admin)
    client.post(f"/v1/departments/{code}/members", json={"user_id": uid}, headers=admin)

    assert client.delete(f"/v1/departments/{code}/members/{uid}",
                         headers=admin).status_code == 204
    assert client.delete(f"/v1/departments/{code}/members/{uid}",
                         headers=admin).status_code == 404


def test_unknown_department_is_404_on_grant_and_patch(clients):
    client, admin, _member, uid = clients
    assert client.post("/v1/departments/nope-xyz/members",
                       json={"user_id": uid}, headers=admin).status_code == 404
    assert client.patch("/v1/departments/nope-xyz", json={"is_active": False},
                        headers=admin).status_code == 404


def test_member_cannot_list_members(clients):
    client, admin, member, _uid = clients
    code = f"p{uuid.uuid4().hex[:6]}"
    client.post("/v1/departments", json={"code": code, "name": "P"}, headers=admin)
    assert client.get(f"/v1/departments/{code}/members",
                      headers=member).status_code == 403


def test_departments_require_authentication(clients):
    client, _admin, _member, _uid = clients
    assert client.get("/v1/departments").status_code == 403
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_rag_departments_api.py -v`
Expected: FAIL — every request 404s, the routes do not exist.

- [ ] **Step 3: Write the schemas**

Create `app/rag/schemas.py`:

```python
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
```

- [ ] **Step 4: Write the router**

Create `app/rag/router.py`:

```python
"""Department administration.

Creating departments and granting access are admin-only. The one route open to
every authenticated caller is `GET /v1/departments`, which returns *that
caller's* departments — granted and active — because it is what the frontend
renders as tabs. A member must not be able to enumerate departments they cannot
use.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import get_current_user, require_admin
from ..db.session import get_session
from ..users.models import ROLE_ADMIN, User
from . import repository as repo
from .schemas import (
    DepartmentCreate,
    DepartmentOut,
    DepartmentUpdate,
    GrantCreate,
    MemberOut,
)

router = APIRouter(prefix="/v1/departments", tags=["departments"])


async def _require_department(session: AsyncSession, code: str):
    dept = await repo.get_department_by_code(session, code)
    if dept is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown department"
        )
    return dept


@router.post("", response_model=DepartmentOut, status_code=status.HTTP_201_CREATED)
async def create_department(
    body: DepartmentCreate,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> DepartmentOut:
    try:
        dept = await repo.create_department(session, code=body.code, name=body.name)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Department '{body.code}' already exists",
        )
    return DepartmentOut.model_validate(dept)


@router.get("", response_model=list[DepartmentOut])
async def list_departments(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[DepartmentOut]:
    """Admins see every department; everyone else sees only their own tabs."""
    if user.role == ROLE_ADMIN:
        rows = await repo.list_departments(session)
    else:
        rows = await repo.list_departments_for_user(session, user.id)
    return [DepartmentOut.model_validate(d) for d in rows]


@router.patch("/{code}", response_model=DepartmentOut)
async def update_department(
    code: str,
    body: DepartmentUpdate,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> DepartmentOut:
    dept = await _require_department(session, code)
    if body.name is not None:
        dept.name = body.name
    if body.is_active is not None:
        # Soft-disable is the only retirement path: documents and chat_sessions
        # reference departments with ON DELETE RESTRICT.
        dept.is_active = body.is_active
    await session.commit()
    await session.refresh(dept)
    return DepartmentOut.model_validate(dept)


@router.get("/{code}/members", response_model=list[MemberOut])
async def list_members(
    code: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[MemberOut]:
    dept = await _require_department(session, code)
    rows = await repo.list_department_members(session, dept.id)
    return [MemberOut.model_validate(m) for m in rows]


@router.post("/{code}/members", status_code=status.HTTP_204_NO_CONTENT)
async def grant_member(
    code: str,
    body: GrantCreate,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    dept = await _require_department(session, code)
    try:
        await repo.grant_department(
            session, user_id=body.user_id, department_id=dept.id,
            granted_by=admin.id,
        )
        await session.commit()
    except IntegrityError:
        # Unknown user_id -> FK violation.
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown user"
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{code}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_member(
    code: str,
    user_id: int,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    dept = await _require_department(session, code)
    removed = await repo.revoke_department(
        session, user_id=user_id, department_id=dept.id
    )
    await session.commit()
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No such grant"
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
```

- [ ] **Step 5: Wire it into the app**

In `app/main.py`, add the import next to the other router imports (after the `from .ollama.client import ...` line, keeping alphabetical-ish grouping):

```python
from .rag.router import router as departments_router
```

And register it with the other authenticated routers, after `app.include_router(sessions_router)`:

```python
app.include_router(departments_router)
```

- [ ] **Step 6: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_rag_departments_api.py -v`
Expected: PASS, 9 tests

- [ ] **Step 7: Run the whole suite**

Run: `.venv/bin/pytest -q`
Expected: all pass or skip; no new failures. Remember `include_router` mounts lazily in Starlette 1.x — verify routes via TestClient or `/openapi.json`, never `isinstance` checks on `app.routes`.

- [ ] **Step 8: Commit**

```bash
git add app/rag/schemas.py app/rag/router.py app/main.py tests/test_rag_departments_api.py
git commit -m "feat(rag): department admin API (create/list/patch, grant/revoke)"
```

---

### Task 7: Document the slice

**Files:**
- Modify: `CLAUDE.md` (Layout, Endpoints, Conventions sections)

- [ ] **Step 1: Add `rag/` to the Layout section**

In `CLAUDE.md`, in the `## Layout` paragraph, after the `history/` entry, add:

```
`rag/` (department-scoped RAG: `models` = `departments` + `user_departments` +
`documents` + `document_chunks` (pgvector `vector(1536)` + generated `tsv`) +
`ingest_jobs`, `context` = `rag_context`/`current_department` contextvar,
`access.resolve_department` = the permission boundary, `repository` = data
access, `router` = `/v1/departments`),
```

- [ ] **Step 2: Add the endpoints**

In the `## Endpoints` section, in the authed list, add:

```
`POST /v1/departments` (admin), `GET /v1/departments` (admin → all; member →
granted+active, i.e. the frontend's tabs), `PATCH /v1/departments/{code}`
(admin), `GET|POST /v1/departments/{code}/members` (admin),
`DELETE /v1/departments/{code}/members/{user_id}` (admin).
```

- [ ] **Step 3: Add the conventions/gotchas**

Append to the `## Conventions / gotchas` list:

```
- **Department access is a database invariant, not a convention.** A chunk's
  `department_id` is held to its document's by the composite FK
  `(document_id, department_id) → documents(id, department_id)`, so
  `WHERE department_id = ?` is enforced by Postgres rather than by application
  code behaving correctly. `documents` carries the otherwise-redundant
  `UNIQUE (id, department_id)` purely as that FK's target — don't "clean it up".
- **The department is NEVER a tool argument.** `resolve_department` validates the
  request's tab code against `user_departments` and installs it via
  `rag_context`, exactly like `file_sink`/`file_source`. Same streaming rule: set
  it INSIDE the async generator Starlette iterates. Retrieval tools take no
  `department` parameter, so a prompt injection has nothing to target. Contract:
  404 unknown/inactive, 404 foreign session (ownership is re-checked, not assumed
  of the caller), 403 ungranted, 409 department mismatch, 409 **existing general
  session given a department**, 400 bound session with no code. Admins bypass the
  grant check ONLY.
- **`chat_session is None` (new) ≠ `chat_session.department_id is None`
  (existing general chat).** Both look like "no department". Collapsing them lets
  an existing general conversation be relabelled HR on turn five, misrepresenting
  every prior turn as departmentally grounded. New sessions may open in a
  department; existing general ones get a 409.
- **Departments are never deleted.** `documents.department_id` and
  `chat_sessions.department_id` are both `ON DELETE RESTRICT` — deleting a
  department must not silently rewrite an old HR session into a general one.
  `departments.is_active = false` is the only retirement path.
- **Both RAG unique indexes are PARTIAL, deliberately.**
  `ux_documents_active_content` excludes `archived` rows, or archiving a document
  (which deletes its chunks but keeps the row for audit) would permanently block
  re-uploading that file. `ux_ingest_jobs_active_document` covers only
  `queued|running`, because `FOR UPDATE SKIP LOCKED` guards a single row and does
  nothing about two active jobs for one document. Both surface as 409, not 500.
- **The status CHECK constraints are load-bearing, not hygiene.** Both partial
  indexes key off exact strings, so a typo'd status (`'runnning'`) would match no
  predicate and silently escape `ux_ingest_jobs_active_document` entirely.
  `ck_documents_status`, `ck_documents_source` and `ck_ingest_jobs_status` close
  the vocabularies. Adding a status value means editing the CHECK too.
- **`documents.storage_key` is a RELATIVE key under `RAG_DOCS_DIR`**, not an
  absolute path (unlike `generated_files.path`). Rows stay portable across hosts
  and the same value becomes the object-storage key later.
- **`metadata` is reserved by SQLAlchemy declarative** — the attribute is `meta`,
  the column keeps the name (`mapped_column("metadata", JSONB, ...)`).
- **The HNSW/GIN indexes are declared on the model AND hand-written in the
  migration**, and excluded from autogenerate comparison via `_include_object` in
  `alembic/env.py` — Alembic cannot reflect an HNSW opclass or its
  `WITH (m, ef_construction)` options, so without the exclusion every drift check
  proposes dropping and recreating them.
- **`tsv` uses `'english'`, not `'simple'`** — measured: English stems
  (`loans`→`loan`) while Devanagari passes through untouched, so a mixed
  Nepali/English corpus gains recall and loses nothing. Changing it rewrites the
  table (it's a STORED generated column).
```

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: RAG slice 1 — layout, endpoints, department invariants"
```

---

## Verification

Run the whole suite and confirm the RAG tests are present and passing:

```bash
.venv/bin/pytest -q
.venv/bin/pytest tests/test_rag_models.py tests/test_rag_context.py \
                 tests/test_rag_schema_integration.py \
                 tests/test_rag_repository_integration.py \
                 tests/test_rag_access_integration.py \
                 tests/test_rag_departments_api.py -v
```

Expected: 62 RAG tests pass (13 models + 7 context + 12 schema + 7 repository + 14 access + 9 API), and the pre-existing suite is unchanged. If the integration files skip, Postgres is down — this slice is not verified until they actually run.

Then confirm the routes are really mounted (Starlette 1.x lazy-mount caveat):

```bash
.venv/bin/python -c "
from starlette.testclient import TestClient
from app.main import app
with TestClient(app) as c:
    paths = [p for p in c.get('/openapi.json').json()['paths'] if 'department' in p]
    print('\n'.join(sorted(paths)))
"
```

Expected: four paths — `/v1/departments`, `/v1/departments/{code}`, `/v1/departments/{code}/members`, `/v1/departments/{code}/members/{user_id}`.

## What this slice deliberately does not do

`/v1/chat` still has no `department` field, and nothing calls `resolve_department`
or `rag_context` in production code yet — there is no retrieval tool to consume
them. That wiring belongs to slice 3, alongside `search_department_docs`. The
functions are fully built and tested here because slice 2 (ingestion) needs the
schema and slice 3 needs the boundary, and building a security boundary under
time pressure next to the feature that uses it is how boundaries get holes.

## Decided for slice 3: how the department reaches a turn

**A chat session is bound to exactly one department, and retrieval always uses
the server-side `chat_sessions.department_id`.** The request's `department` opens
a session in a tab and is cross-checked against the bound one; it is never the
source of truth for a turn that already has a session.

**Fold the grant check into `open_turn`'s existing session query** — one query
returning session + department + grant, so department authorization costs **zero
additional round trips**. Postgres stays the live source of truth and revocation
takes effect on the next turn.

**Explicitly out of scope, now and for the demo: JWT department claims, refresh
tokens, and authorization caching.** Measured, `resolve_department` costs
0.518 ms against a turn dominated by seconds of inference, and the request stays
DB-bound regardless because `get_current_user` selects the user row on every
authenticated request (0.244 ms). Claims would buy that half millisecond with a
revocation propagation window — up to 24h under the current token lifetime, since
this project has no refresh flow (`/auth/register` and `/auth/login` are the only
auth routes). Not a trade worth making in a bank. Do not reintroduce these
without a decision to build refresh-token infrastructure first.

### The final slice-3 contract (decided — no longer open)

| Session state | Request `department` | Slice 3 behaviour |
|---|---|---|
| new (no row) | given | validate grant, **bind** the new session to it |
| new (no row) | absent | general chat |
| bound to X | absent | **use X** — not an error |
| bound to X | `= X` | use X (consistency check passed) |
| bound to X | `≠ X` | **409** |
| `department_id IS NULL` | given | **409** — a general chat stays general; start a new session |
| `department_id IS NULL` | absent | general chat |

- A session is bound to exactly one department **for its lifetime**.
- `chat_sessions.department_id` is the server-side source of truth for retrieval.
- `department` in the body is **required only to open a new department chat**. On
  an existing session it is an optional consistency check, never the source.
- Revoked access takes effect on the **next turn** — Postgres stays the live
  authorization source.
- Admins retain the grant-bypass.

**This changes one slice-1 behaviour, in slice 3 only.** Slice 1 returns **400**
for "bound session + no `department` in the body"
(`test_bound_session_cannot_be_continued_without_a_code`). Slice 3 replaces that
with "use the bound department" and updates that test. **Do not retroactively
change slice 1** — it is locked and its tests pass as written.
