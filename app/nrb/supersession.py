"""Which version of an NRB document is the current, searchable one.

NRB republishes. The same circular gets a corrected annex, the same statistical
release gets restated figures, and the file at that URL becomes different bytes.
Phase 5 gives those bytes a new `content_sha256`, Phase 7 step 1 mints a second
`documents` row for them, and step 2 recovers them without touching the old
recovery. What was missing is the last question: **which of the two rows should
retrieval return, and when does the other stop being searchable.**

THE RULE, AND IT IS ONE-DIRECTIONAL
-----------------------------------
A is the version currently serving. B is the candidate.

    B fails at any stage  →  A is still searchable. Nothing was archived.
    B succeeds            →  B is current, A is archived.

There must be no window in which a failed replacement has removed the last good
version. That is not achieved by ordering two commits carefully — it is achieved
by doing the archive and the activation in **one transaction**, the same one
`ingest.replace_chunks` already owns. If anything in it fails, A was never
archived, because the rollback took the archive with it.

So promotion runs *inside* the replacement transaction and *before* the chunks
are written, which sounds backwards and is not: the expensive, failure-prone
work (recover, chunk, embed) has already completed by then and is sitting in
memory. Reaching this transaction at all means B succeeded.

THE LOGICAL SOURCE IDENTITY IS `comparison_key`
-----------------------------------------------
`nrb_files.comparison_key` — the percent-decoded attachment URL, unique in the
catalog by `ux_nrb_files_comparison_key`, and already written onto every NRB
document's `metadata` by both ingest drivers. It identifies the FILE, across
versions of its bytes.

Why not the alternatives:

  * **`content_sha256` / `content_hash`** identifies the VERSION, not the
    source. Two versions of one circular have two hashes; that is the whole
    reason this module exists.
  * **`page_url` / `nrb_sources.url_key`** is post-level, and a post really can
    carry two attachments — a circular plus its annex (§3 measured 0.7% of
    posts). Both would share one page_url, so promoting the circular would
    archive the annex. That is a concrete collision, not a hypothetical, which
    is what rules it out.
  * **titles, filenames, dates, text similarity** — never. NRB publishes
    near-identical Devanagari titles across years and 3 documents have no title
    at all (`models.NRBSource`). A fuzzy match here would silently un-publish a
    document nobody asked to retire.

TWO CATALOG KEYS THAT SHARE BYTES
    Deduplication is by BYTES: `select_ingest_targets` does
    `DISTINCT ON (content_sha256)` and keeps the lowest `nrb_files.id` as the
    representative, so N aliases of one blob produce ONE document carrying the
    representative's `comparison_key`. They therefore share one logical
    identity, chosen deterministically rather than by whichever pass ran first.
    If those aliases later diverge — different bytes behind each URL — each
    becomes its own logical source from that point, and neither supersedes the
    other. That is the honest outcome: they are no longer the same file.

ORDERING, AND WHAT THE CATALOG DOES *NOT* GIVE US
-------------------------------------------------
`nrb_files` holds one `content_sha256` per key and **overwrites it in place**.
It keeps no history of prior versions, so there is no catalog-side version
number, sequence or timestamp to order B against C. What exists is the order in
which our own driver OBSERVED each version — `documents.created_at`, tie-broken
by `id` — and because the driver is the only thing that mints these rows, and
the catalog only ever offers the current version, that order faithfully records
catalog succession. It is our record, not NRB's; see `docs/nrb-integration.md`
§22 for what a stronger guarantee would need.

Job COMPLETION order is explicitly not used. C can finish before B, and does not
thereby become older. So:

    a document promotes itself over strictly OLDER siblings only;
    if a strictly NEWER sibling is already `ready`, the document archives
    ITSELF instead of demoting it.

Both halves are needed. Without the first, a late-finishing B would archive C.
Without the second, B would go live after C and stay there.

CONCURRENCY
    Two guards, and they do different jobs. `SELECT … FOR UPDATE` over the
    sibling set in `id` order serialises two promoting workers and gives the
    second one the first one's committed state. The partial unique index
    `ux_documents_nrb_current_source` makes "at most one `ready` document per
    (department, logical source)" a database invariant rather than a property of
    this file being correct — the same posture as `ux_documents_active_content`
    and the composite chunk FK. No advisory lock, no distributed lock: row
    locking plus a constraint is enough.

WHAT THIS MODULE NEVER TOUCHES
    `nrb_files`, blobs, `nrb_recoveries`/`nrb_recovery_units`, and
    `nrb_extractions`. Supersession decides which RAG document is searchable and
    nothing else. Archiving a version must not cost us the evidence of it, and
    an archived version's recovery stays cached — re-running OCR on a document
    precisely because it stopped being current would be the worst possible time
    to do it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

import json

from sqlalchemy import String, cast, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from ..rag import documents as docs_repo
from ..rag.models import STATUS_ARCHIVED, STATUS_READY, Document
from .rag import NRB_ORIGIN

logger = logging.getLogger("app.nrb.supersession")

__all__ = [
    "LOGICAL_KEY_FIELD",
    "Promotion",
    "Sibling",
    "archive_self",
    "logical_key",
    "plan_promotion",
    "promote",
]

# The metadata field that carries the logical source identity. Both ingest
# drivers already write it; a document without it (an ordinary upload, or an
# NRB document minted before this existed) simply has no logical source and is
# never superseded and never supersedes.
LOGICAL_KEY_FIELD = "comparison_key"


def logical_key(meta: dict[str, Any] | None) -> str | None:
    """The logical source of one document, or None if it has none.

    Requires `origin == "nrb"` as well as the key: `comparison_key` is an NRB
    word, and an ordinary upload that happened to carry one must not be drawn
    into NRB's lifecycle.
    """
    meta = meta or {}
    if meta.get("origin") != NRB_ORIGIN:
        return None
    value = meta.get(LOGICAL_KEY_FIELD)
    if not isinstance(value, str):
        return None
    return value.strip() or None


@dataclass(frozen=True)
class Sibling:
    """Another version of the same logical source, in the same department."""

    id: str
    status: str
    content_hash: str
    created_at: datetime

    @property
    def order_key(self) -> tuple[datetime, str]:
        """Observation order. `created_at` is the driver's transaction clock, so
        two versions minted in separate transactions never tie; `id` is the
        tie-break, arbitrary but total and stable."""
        return (self.created_at, self.id)


@dataclass(frozen=True)
class Promotion:
    """What promoting this document means. Computed under the sibling lock."""

    document_id: str
    key: str | None
    # Non-archived siblings strictly OLDER than this document. Archived when the
    # promotion is applied.
    supersedes: tuple[str, ...] = ()
    # Set when a strictly NEWER sibling is already `ready`. This document lost;
    # it must archive itself rather than replace the newer one.
    superseded_by: str | None = None

    @property
    def applies(self) -> bool:
        """Is there any lifecycle work to do at all?

        False for a non-NRB document and for the ordinary first-version case,
        which keeps the generic ingest path exactly as it was.
        """
        return bool(self.key) and (
            bool(self.supersedes) or self.superseded_by is not None
        )


async def _lock_siblings(
    session: AsyncSession, *, department_id: int, key: str
) -> list[Sibling]:
    """Every non-archived version of this logical source, locked in `id` order.

    `FOR UPDATE` is what serialises two workers promoting two versions of the
    same source; the `ORDER BY id` is what stops them deadlocking, since both
    take the same rows in the same order. Archived rows are excluded — they are
    already out of the running and locking them would widen the lock for nothing.

    The expression `metadata ->> 'comparison_key'` is matched rather than a
    column, which is exactly what `ux_documents_nrb_current_source` indexes.
    """
    rows = (
        await session.execute(
            select(
                Document.id,
                Document.status,
                Document.content_hash,
                Document.created_at,
            )
            .where(
                Document.department_id == department_id,
                Document.status != STATUS_ARCHIVED,
                Document.meta["origin"].astext == NRB_ORIGIN,
                Document.meta[LOGICAL_KEY_FIELD].astext == key,
            )
            .order_by(Document.id)
            .with_for_update()
        )
    ).all()
    return [Sibling(*row) for row in rows]


async def plan_promotion(
    session: AsyncSession, *, document_id: str
) -> Promotion:
    """Decide what promoting `document_id` means, holding the sibling lock.

    Pure decision, no writes — but it is NOT side-effect free: it takes row
    locks, and those must be held until the caller's transaction commits or the
    decision it returns is worthless. Call it inside the transaction that will
    act on it, never in a scratch session.
    """
    doc = await docs_repo.get_document(session, document_id)
    if doc is None:
        return Promotion(document_id, None)
    key = logical_key(doc.meta)
    if key is None:
        return Promotion(document_id, None)

    siblings = await _lock_siblings(
        session, department_id=doc.department_id, key=key
    )
    me = next((s for s in siblings if s.id == document_id), None)
    if me is None:
        # Archived (or deleted) between the job finishing and this lock. Not our
        # call to reverse — an archive that landed mid-ingest wins, exactly as
        # `replace_chunks` already decides for the plain re-ingest case.
        return Promotion(document_id, key)

    older = tuple(
        s.id for s in siblings
        if s.id != document_id and s.order_key < me.order_key
    )
    newer_ready = next(
        (
            s for s in sorted(siblings, key=lambda s: s.order_key, reverse=True)
            if s.order_key > me.order_key and s.status == STATUS_READY
        ),
        None,
    )
    return Promotion(
        document_id=document_id,
        key=key,
        supersedes=older,
        superseded_by=newer_ready.id if newer_ready else None,
    )


def _stamp(**fields: Any):
    """A jsonb merge for `documents.metadata`, as a Core expression.

    Core rather than mutating `doc.meta` in the ORM: a JSONB dict mutated in
    place is not seen as dirty without `flag_modified`, which is the kind of
    thing that works in a test and silently does nothing in the worker.

    The bind is typed `String` before the cast, deliberately. `cast(<py str>,
    JSONB)` lets SQLAlchemy bind the parameter AS jsonb, which serialises the
    string into a JSON *string scalar* — and `jsonb_object || jsonb_string` is
    a legal operation producing an ARRAY, so the metadata silently turns into
    `[{...}, "{...}"]` with no error anywhere. Measured.
    """
    payload = {k: v for k, v in fields.items() if v is not None}
    return Document.meta.op("||")(
        cast(literal(json.dumps(payload), String), JSONB)
    )


async def _mark(
    session: AsyncSession, ids: Sequence[str], **fields: Any
) -> None:
    if not ids:
        return
    await session.execute(
        update(Document).where(Document.id.in_(list(ids))).values(meta=_stamp(**fields))
    )


async def promote(session: AsyncSession, *, document_id: str) -> Promotion:
    """Archive every older version of this document's logical source.

    Returns the plan that was applied, so the caller can log it and can see the
    `superseded_by` case — which it must handle differently, because a document
    that lost to a newer sibling must NOT go on to write its chunks.

    Does not commit. The caller owns the transaction, and that is the whole
    safety property: this archive and the activation that follows it either both
    land or neither does.
    """
    plan = await plan_promotion(session, document_id=document_id)
    if not plan.supersedes:
        return plan

    now = datetime.now(timezone.utc).isoformat()
    for old in plan.supersedes:
        # `archive_document` deletes the chunks and keeps the row: the version is
        # no longer searchable, and the audit trail of what it held survives.
        # Blobs, recovery rows and catalog history are untouched.
        await docs_repo.archive_document(session, old)
    await _mark(
        session, plan.supersedes, superseded_by=document_id, superseded_at=now
    )
    logger.info(
        "NRB supersession: %s supersedes %d older version(s) of %s",
        document_id, len(plan.supersedes), plan.key,
    )
    return plan


async def archive_self(
    session: AsyncSession, *, document_id: str, superseded_by: str
) -> None:
    """Retire a candidate that lost to a newer version while it was ingesting.

    Its chunks are never written — it goes straight to `archived` — because
    publishing them even briefly would put two `ready` versions of one source in
    the index, which `ux_documents_nrb_current_source` would refuse anyway. The
    job is still a success: it did its work, and the outcome is that a newer
    version had already won.
    """
    await docs_repo.archive_document(session, document_id)
    await _mark(
        session,
        [document_id],
        superseded_by=superseded_by,
        superseded_at=datetime.now(timezone.utc).isoformat(),
    )
    logger.info(
        "NRB supersession: %s archived on arrival — %s is newer and already ready",
        document_id, superseded_by,
    )
