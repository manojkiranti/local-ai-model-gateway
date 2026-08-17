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

from sqlalchemy import extract, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..rag import documents as docs_repo
from ..rag import jobs as jobs_repo
from ..rag import storage
from ..rag.models import STATUS_ARCHIVED, Document
from . import filestore
from .catalog import bounded_keys
from .models import NRBFile, NRBSource, NRBSourceFile
from .rag import NRB_ORIGIN

logger = logging.getLogger("app.nrb.corpus")

__all__ = [
    "IngestTarget",
    "IngestOutcome",
    "create_ingest_targets",
    "select_ingest_targets",
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
