"""Re-queue already-ingested documents so they pick up a new chunker.

Chunking changes only affect documents parsed AFTER the change — a document
that was ingested before does not retroactively benefit just because the
chunker code changed. This replays existing ones through the SAME worker path
a fresh upload uses: it enqueues an `ingest_jobs` row per document and stops.
It does no parsing or embedding itself, so **the separate worker process
(`python -m app.rag.worker`) must already be running** for anything to
actually happen — this command only ever inserts rows.

That is deliberately safe to run against a live corpus: `replace_chunks` is
atomic and re-checks the document's status under a row lock, and a failed
re-ingest of a `ready` document leaves it `ready` with its previous chunks
intact (see `app/rag/ingest.py`). A document that already has an active
(queued|running) job is left alone and counted as skipped, not retried or
raised as an error — `ux_ingest_jobs_active_document` already guarantees at
most one active job per document, and that existing job will pick up the new
chunker anyway since it hasn't run yet. Archived documents are never
requeued: their chunks were deliberately removed and the row kept only for
audit (same reasoning as departments never being deleted).

    .venv/bin/python -m app.rag.reingest [--department CODE] [--dry-run]

`--dry-run` reports what would be queued without enqueuing anything at all.
`--ids-file FILE` limits the run to the listed document ids (native-3 re-ingests only the detected documents).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ..config import get_settings
from . import jobs as jobs_repo
from .models import STATUS_ARCHIVED, Department, Document

log = logging.getLogger("rag.reingest")


async def reingest(
    session: AsyncSession,
    *,
    department_code: str | None,
    dry_run: bool,
    document_ids: Sequence[str] | None = None,
) -> dict[str, int]:
    """Queue an ingest job for every non-archived document. Returns a summary.

    `dry_run=True` enqueues nothing at all — the loop below never calls
    `jobs_repo.enqueue` in that branch, so there is nothing to roll back.

    The scan selects plain (id, title) TUPLES, never ORM `Document` instances,
    and that is load-bearing rather than a micro-optimisation. `jobs_repo.enqueue`
    calls `session.rollback()` on an IntegrityError, and `rollback()` expires every
    persistent object in the session — independent of `expire_on_commit`, which
    governs `commit()` only. Holding ORM rows across that rollback means the NEXT
    iteration's `doc.id` triggers an implicit synchronous refresh, which raises
    `MissingGreenlet` under an `AsyncSession` and kills the whole run partway
    through with no summary. Tuples cannot expire.

    `ORDER BY id` is likewise deliberate: without it the scan order is whatever
    Postgres happens to return, so the failure above appeared only when a
    conflicted document sorted before a clean one — an intermittent crash in a
    backfill command, which is the worst way to find out.
    """
    stmt = select(Document.id, Document.title).where(
        Document.status != STATUS_ARCHIVED
    )
    if department_code:
        stmt = stmt.join(Department, Department.id == Document.department_id).where(
            Department.code == department_code
        )
    if document_ids is not None:
        stmt = stmt.where(Document.id.in_(list(document_ids)))
    documents = list((await session.execute(stmt.order_by(Document.id))).all())

    queued = skipped = 0
    for doc_id, title in documents:
        if dry_run:
            log.info("would queue %s (%s)", doc_id, title)
            continue
        try:
            await jobs_repo.enqueue(session, document_id=doc_id)
            await session.commit()
            queued += 1
        except jobs_repo.JobConflict:
            # Expected traffic, not an error: an ingest is already queued or
            # running for this document. Skipping is correct — that job will
            # use the new chunker since it hasn't run yet.
            skipped += 1

    return {"queued": queued, "skipped": skipped, "total": len(documents)}


def read_ids_file(path) -> list[str]:
    """One document id per line; blank lines ignored."""
    from pathlib import Path

    return [line.strip() for line in Path(path).read_text().splitlines() if line.strip()]


async def _main() -> None:  # pragma: no cover - process entrypoint
    parser = argparse.ArgumentParser(description="Re-queue documents for ingestion.")
    parser.add_argument("--department", default=None, help="Department code to limit to.")
    parser.add_argument("--dry-run", action="store_true", help="Show, don't queue.")
    parser.add_argument("--ids-file", default=None, help="Only these document ids, one per line.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            stats = await reingest(
                session,
                department_code=args.department,
                dry_run=args.dry_run,
                document_ids=read_ids_file(args.ids_file) if args.ids_file else None,
            )
    finally:
        await engine.dispose()

    verb = "would queue" if args.dry_run else "queued"
    log.info(
        "%s %d of %d document(s); %d skipped (already active)",
        verb,
        stats["queued"] if not args.dry_run else stats["total"],
        stats["total"],
        stats["skipped"],
    )
    if not args.dry_run:
        log.info("the ingest worker must be running for these to be processed")


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(_main())
