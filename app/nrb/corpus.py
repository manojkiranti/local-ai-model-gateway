"""Phase 7: NRB catalog blobs → queued department-RAG ingest jobs.

This is the ENQUEUE seam, and it is deliberately separate from `app/nrb/rag.py`
(the PARSE seam). This module decides *which* blobs become `documents` rows and
creates them; `rag.py` decides what a blob's bytes become once the worker picks
the job up. Neither knows about the other.

WHAT IT SELECTS FROM, AND WHAT IT REFUSES TO LOOK AT
    The catalog only — `nrb_files` joined to `nrb_sources` for a title. It does
    **not** read `nrb_extractions`, by decision (`docs/nrb-integration.md` §19,
    2026-08-17): that table is Phase 6 evidence, it is not on the ingestion path,
    and a driver that filtered on it would make every future ingest depend on
    whether a measurement pass had been run first. The consequence is that this
    module cannot know a blob's route before enqueuing it and does not try; a
    blob that turns out to be unparseable fails its own job, which is the
    behaviour the Phase 7 cohort's one OLE2 file exists to prove.

    Recovery reuse, when it lands, comes from a new versioned recovery cache —
    never from `nrb_extractions`, and never from a pre-filter here.

TWO WAYS A DOCUMENT IS ALREADY PRESENT, AND THEY MEAN DIFFERENT THINGS
    `select_ingest_targets` anti-joins the scope against `documents` in the
    target department, so the ordinary "I ran this yesterday" case selects
    nothing and costs one query. `create_ingest_targets` ALSO catches
    `DocumentConflict`, but that path only fires when a row appeared between the
    select and the insert — a second driver, or a manual upload of the same
    bytes. The two are counted separately because a nonzero conflict count means
    concurrency, not idempotence.

    The anti-join repeats `ux_documents_active_content`'s own predicate
    (`status <> 'archived'`), because an archived document is deliberately
    re-ingestable and skipping it here would make archiving permanent.

A THIRD WAY, AND IT IS OPT-IN — `--retry-failed`
    The anti-join's side effect is that a document whose ingest FAILED is never
    selected again: `failed` is not `archived`, so it stays excluded forever.
    That is right by default — a permanently unparseable blob (the cohort's OLE2
    file) must not be retried on every pass — and wrong for a transient failure,
    which is what `select_retry_targets` + `requeue_failed` exist for.

    They are a separate pair of functions, not a flag threaded through the
    normal path, because they do a different thing: the normal path CREATES
    documents, the retry path creates none and only enqueues a job against a
    document that already exists. No second `documents` row is minted, no file
    is re-copied, and `ready`, `pending` and `archived` documents are
    unreachable from it. There is deliberately no transient-vs-permanent
    classifier: "which failures are worth retrying" is an operator's judgement
    for now, and the retry is explicit precisely so it can be.

IDENTITY
    `documents.content_hash` is `sha256(bytes)` (`rag.documents.content_hash_of`)
    and so is `nrb_files.content_sha256` (`nrb.filestore`). They are the same
    value for the same bytes, which is what makes the anti-join a plain equality
    and makes NRB dedup fall out of the existing unique index rather than
    needing one of its own. `create_ingest_targets` asserts it per file rather
    than trusting it — the bytes are read anyway to be copied.

NOTHING IS DRAINED HERE
    Jobs are queued and that is all. A driver that drained in-process would race
    the deployed worker, and `FOR UPDATE SKIP LOCKED` means the two would split
    the scope rather than collide — quietly measuring neither.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

from sqlalchemy import extract, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..rag import documents as docs_repo
from ..rag import jobs as jobs_repo
from ..rag import storage
from ..rag.models import (
    JOB_QUEUED,
    JOB_RUNNING,
    STATUS_ARCHIVED,
    STATUS_FAILED,
    STATUS_PENDING,
    Document,
    IngestJob,
)
from . import filestore
from .catalog import bounded_keys
from .models import NRBFile, NRBSource, NRBSourceFile
from .rag import NRB_ORIGIN

logger = logging.getLogger("app.nrb.corpus")

__all__ = [
    "IngestTarget",
    "IngestOutcome",
    "RetryOutcome",
    "RetryTarget",
    "create_ingest_targets",
    "requeue_failed",
    "select_ingest_targets",
    "select_retry_targets",
]

# Commit cadence. Matches `fetch.py`'s: an interrupt keeps its progress, and the
# next pass takes the NEXT documents because the anti-join no longer selects the
# committed ones.
BATCH = 25


@dataclass(frozen=True)
class IngestTarget:
    """One blob, with everything needed to mint a `documents` row.

    `title` is NRB's own, from the source that references the file; the filename
    is only a fallback. A retrieval hit shows this string, so a uuid storage key
    or a slugified URL would make every citation unreadable.
    """

    file_id: int
    content_sha256: str
    storage_key: str
    extension: str | None
    filename: str | None
    comparison_key: str
    title: str
    catalog: dict[str, Any] = field(default_factory=dict)


@dataclass
class IngestOutcome:
    """What one pass did. Every field is a count of documents, not of catalog
    keys — two keys sharing bytes are one document, and the caller reports both
    numbers so the gap does not read as a failure."""

    scope_keys: int = 0
    selected: int = 0
    created: int = 0
    skipped_existing: int = 0
    conflict_document: int = 0
    conflict_job: int = 0
    missing_blob: int = 0
    errors: list[str] = field(default_factory=list)
    documents: list[tuple[str, str]] = field(default_factory=list)  # (doc_id, sha)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope_keys": self.scope_keys,
            "selected": self.selected,
            "created": self.created,
            "skipped_existing": self.skipped_existing,
            "conflict_document": self.conflict_document,
            "conflict_job": self.conflict_job,
            "missing_blob": self.missing_blob,
            "errors": list(self.errors),
        }


def _scoped_files(stmt, *, keys, sections, owners, years, resource_types, extensions):
    """Narrow a `nrb_files` select to the operator's scope. Shared, on purpose.

    Both the create path and the retry path have to mean the SAME thing by
    `--section circulars`; two copies of these predicates would eventually
    disagree and a retry would quietly cover a different slice than the run it
    is retrying.
    """
    if keys:
        stmt = stmt.where(NRBFile.comparison_key.in_(bounded_keys(keys)))
    if resource_types:
        stmt = stmt.where(NRBFile.resource_type.in_(list(resource_types)))
    if extensions:
        stmt = stmt.where(NRBFile.extension.in_([e.lower() for e in extensions]))

    if sections or owners or years:
        link = (
            select(NRBSourceFile.file_id)
            .join(NRBSource, NRBSource.id == NRBSourceFile.source_id)
            .where(NRBSourceFile.file_id == NRBFile.id, NRBSource.is_active.is_(True))
        )
        if sections:
            link = link.where(NRBSource.document_type.in_(list(sections)))
        if owners:
            link = link.where(NRBSource.owner.in_(list(owners)))
        if years:
            link = link.where(
                extract("year", NRBSource.published_at).in_([int(y) for y in years])
            )
        stmt = stmt.where(link.exists())
    return stmt


async def select_ingest_targets(
    session: AsyncSession,
    *,
    department_id: int,
    keys: Sequence[str] | None = None,
    sections: Sequence[str] | None = None,
    owners: Sequence[str] | None = None,
    years: Sequence[int] | None = None,
    resource_types: Sequence[str] | None = None,
    extensions: Sequence[str] | None = None,
    limit: int | None = None,
) -> list[IngestTarget]:
    """Fetched blobs not yet in this department, in a deterministic order.

    Only `fetch_status = 'fetched'` rows can be selected, which by construction
    excludes `pending`, `failed` and the three `blocked_host` UAT links — the
    same "excluded by the status column, not by a WHERE clause someone could
    forget" rule Phases 5 and 6 use.

    `DISTINCT ON (content_sha256)` because two catalog entries sharing bytes are
    ONE document: the unique index would reject the second anyway, and selecting
    it would inflate the conflict count with something that is not a conflict.
    The representative is the lowest `id`, and the ordering is stable, so a
    resumed pass continues rather than re-rolling which blobs got done.
    """
    stmt = (
        select(
            NRBFile.id,
            NRBFile.content_sha256,
            NRBFile.storage_key,
            NRBFile.extension,
            NRBFile.filename,
            NRBFile.comparison_key,
        )
        .where(
            NRBFile.fetch_status == "fetched",
            NRBFile.content_sha256.isnot(None),
            NRBFile.storage_key.isnot(None),
        )
        .distinct(NRBFile.content_sha256)
        .order_by(NRBFile.content_sha256, NRBFile.id)
    )
    stmt = _scoped_files(
        stmt, keys=keys, sections=sections, owners=owners, years=years,
        resource_types=resource_types, extensions=extensions,
    )

    # Already ingested here? Skip. Correlates on `content_hash == content_sha256`
    # — the same number (see the module docstring) — and repeats the partial
    # unique index's own predicate, because an ARCHIVED document must stay
    # re-ingestable.
    already = select(Document.id).where(
        Document.department_id == department_id,
        Document.content_hash == NRBFile.content_sha256,
        Document.status != STATUS_ARCHIVED,
    )
    stmt = stmt.where(~already.exists())

    if limit is not None:
        stmt = stmt.limit(limit)

    rows = (await session.execute(stmt)).all()
    if not rows:
        return []

    titles = await _titles_for(session, [r[0] for r in rows])
    targets: list[IngestTarget] = []
    for file_id, sha, key, ext, filename, comparison_key in rows:
        meta = titles.get(file_id, {})
        title = (meta.get("title") or filename or sha)[:512]
        targets.append(
            IngestTarget(
                file_id=file_id,
                content_sha256=sha,
                storage_key=key,
                extension=ext,
                filename=filename,
                comparison_key=comparison_key,
                title=title.strip(),
                catalog={k: v for k, v in meta.items() if k != "title" and v},
            )
        )
    return targets


async def _titles_for(
    session: AsyncSession, file_ids: Sequence[int]
) -> dict[int, dict[str, Any]]:
    """NRB's own title and provenance per file, from its first source.

    `ordinal NULLS LAST` picks the source's primary attachment when a post has
    several; a file referenced by three sources still yields one title. Left
    join, because a file whose sources were all deactivated still has bytes and
    is still ingestable — it just falls back to its filename.
    """
    if not file_ids:
        return {}
    rows = (
        await session.execute(
            text(
                """
                SELECT DISTINCT ON (sf.file_id)
                       sf.file_id, s.title, s.page_url,
                       s.document_type, s.owner, s.published_at
                  FROM nrb_source_files sf
                  JOIN nrb_sources s ON s.id = sf.source_id
                 WHERE sf.file_id = ANY(:ids)
                 ORDER BY sf.file_id, sf.ordinal NULLS LAST, s.id
                """
            ),
            {"ids": list(file_ids)},
        )
    ).mappings().all()
    return {
        r["file_id"]: {
            "title": (r["title"] or "").strip(),
            "page_url": r["page_url"],
            "document_type": r["document_type"],
            "owner": r["owner"],
            "published_at": str(r["published_at"]) if r["published_at"] else None,
        }
        for r in rows
    }


async def create_ingest_targets(
    Session,
    *,
    department_id: int,
    department_code: str,
    targets: Sequence[IngestTarget],
    rag_docs_dir: str,
    batch: int = BATCH,
    cohort: str | None = None,
) -> IngestOutcome:
    """Mint a `documents` row + a queued job per target. Commits every `batch`.

    Each document is written in its OWN session-scoped unit so one bad blob
    cannot roll back the rest of its batch — `DocumentConflict` rolls the
    session back internally, which would otherwise discard the work already
    staged beside it.
    """
    outcome = IngestOutcome(selected=len(targets))
    pending = 0
    for target in targets:
        blob = filestore.resolve_path(target.storage_key)
        if not blob.exists():
            outcome.missing_blob += 1
            outcome.errors.append(f"{target.content_sha256[:12]}: blob missing on disk")
            continue
        data = blob.read_bytes()

        # Assert, don't assume: the anti-join and the unique index both rely on
        # these two hashes being the same number.
        content_hash = docs_repo.content_hash_of(data)
        if content_hash != target.content_sha256:
            outcome.errors.append(
                f"{target.content_sha256[:12]}: bytes hash to {content_hash[:12]}"
            )
            continue

        key = storage.mint_storage_key(department_code, target.filename or "blob.bin")
        storage.write_document(data, key, rag_docs_dir)
        try:
            async with Session() as session:
                doc = await docs_repo.create_document(
                    session,
                    department_id=department_id,
                    title=target.title,
                    source="upload",
                    file_type=(target.extension or "").lower() or "bin",
                    content_hash=content_hash,
                    storage_key=key,
                    file_name=target.filename,
                )
                doc.meta = {
                    "origin": NRB_ORIGIN,
                    "blob_sha256": target.content_sha256,
                    "comparison_key": target.comparison_key,
                    **({"cohort": cohort} if cohort else {}),
                    **target.catalog,
                }
                job = await jobs_repo.enqueue(session, document_id=doc.id)
                await session.commit()
                outcome.created += 1
                outcome.documents.append((doc.id, target.content_sha256))
                pending += 1
                _ = job
        except docs_repo.DocumentConflict:
            # Raced, not idempotent — see the module docstring. The file we just
            # wrote is now an orphan, so compensate exactly as the upload route
            # does.
            outcome.conflict_document += 1
            storage.delete_document(key, rag_docs_dir)
        except jobs_repo.JobConflict:
            outcome.conflict_job += 1
            storage.delete_document(key, rag_docs_dir)
        if pending >= batch:
            logger.info("corpus ingest: %d documents queued so far", outcome.created)
            pending = 0
    return outcome


# --------------------------------------------------------------------------- #
# The explicit retry path (`--retry-failed`). Enqueue-only, like everything
# above it: it creates no document, copies no file and drains no job.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RetryTarget:
    """A document that already exists, already failed, and is in scope."""

    document_id: str
    content_sha256: str
    title: str


@dataclass
class RetryOutcome:
    selected: int = 0
    requeued: int = 0
    conflict_job: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "selected": self.selected,
            "requeued": self.requeued,
            "conflict_job": self.conflict_job,
            "errors": list(self.errors),
        }


async def select_retry_targets(
    session: AsyncSession,
    *,
    department_id: int,
    keys: Sequence[str] | None = None,
    sections: Sequence[str] | None = None,
    owners: Sequence[str] | None = None,
    years: Sequence[int] | None = None,
    resource_types: Sequence[str] | None = None,
    extensions: Sequence[str] | None = None,
    limit: int | None = None,
) -> list[RetryTarget]:
    """`failed` documents in this department whose blob is in the NRB scope.

    Three exclusions, each doing work the others do not:

    - `status = 'failed'` is the whole selection. `ready` documents are serving
      and must never be requeued; `pending` ones are already on their way (a
      second `--retry-failed` before the worker drains the first is a no-op for
      this reason, not merely because of the job conflict below); `archived`
      ones are re-ingestable through the ordinary create path, which is where
      that decision already lives.
    - **No active job.** A `failed` document with a `queued` or `running` job is
      a state the sweep can produce, and enqueuing beside it would either hit
      `ux_ingest_jobs_active_document` or — worse, if the index were ever
      relaxed — put two workers on one document.
    - The join to `nrb_files` is what makes this an *NRB* retry. A failed
      ordinary upload sitting in the same department is not ours to requeue,
      and without the join `--retry-failed` would silently adopt it.

    `DISTINCT ON (documents.id)` because two catalog keys can share bytes and
    would otherwise nominate one document twice.
    """
    active_job = select(IngestJob.id).where(
        IngestJob.document_id == Document.id,
        IngestJob.status.in_((JOB_QUEUED, JOB_RUNNING)),
    )
    stmt = (
        select(Document.id, NRBFile.content_sha256, Document.title)
        .join(NRBFile, NRBFile.content_sha256 == Document.content_hash)
        .where(
            Document.department_id == department_id,
            Document.status == STATUS_FAILED,
            NRBFile.fetch_status == "fetched",
            NRBFile.content_sha256.isnot(None),
            ~active_job.exists(),
        )
        .distinct(Document.id)
        .order_by(Document.id)
    )
    stmt = _scoped_files(
        stmt, keys=keys, sections=sections, owners=owners, years=years,
        resource_types=resource_types, extensions=extensions,
    )
    if limit is not None:
        stmt = stmt.limit(limit)

    rows = (await session.execute(stmt)).all()
    return [RetryTarget(doc_id, sha, title or sha) for doc_id, sha, title in rows]


async def requeue_failed(
    Session, *, targets: Sequence[RetryTarget], batch: int = BATCH
) -> RetryOutcome:
    """Queue a fresh job against each already-existing document. Returns counts.

    The document row is REUSED — same id, same `content_hash`, same
    `storage_key`, same `metadata`. Minting a second document would be rejected
    by `ux_documents_active_content` anyway, but the reason not to try is that
    the retry is a new attempt at the SAME document. The previous failure is not
    overwritten either: it stays on its own `ingest_jobs` row, error and all, so
    `--report` can still show what went wrong the first time.

    Status goes back to `pending`, so a document that is queued does not also
    claim to have failed. If the retry fails again the worker writes `failed`
    back (`_record_failure` demotes anything that is not `ready`/`archived`),
    and if it succeeds `replace_chunks` writes `ready`.

    One session per document, exactly as `create_ingest_targets` does, so a
    target that raises cannot roll back the ones already requeued beside it —
    the OLE2 file failing again must not stop the rest of the batch.
    """
    outcome = RetryOutcome(selected=len(targets))
    pending = 0
    for target in targets:
        try:
            async with Session() as session:
                await jobs_repo.enqueue(session, document_id=target.document_id)
                await session.execute(
                    update(Document)
                    .where(Document.id == target.document_id)
                    .values(status=STATUS_PENDING)
                )
                await session.commit()
                outcome.requeued += 1
                pending += 1
        except jobs_repo.JobConflict:
            # A job appeared between the select and the insert. Concurrency, not
            # idempotence — the same distinction `create_ingest_targets` draws.
            outcome.conflict_job += 1
        except Exception as exc:  # noqa: BLE001 - one bad target, recorded
            outcome.errors.append(
                f"{target.content_sha256[:12]}: {type(exc).__name__}: {exc}"
            )
        if pending >= batch:
            logger.info("corpus retry: %d documents requeued so far", outcome.requeued)
            pending = 0
    return outcome
