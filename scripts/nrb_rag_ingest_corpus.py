#!/usr/bin/env python
"""Queue NRB catalog blobs into a department for RAG ingestion. ENQUEUE ONLY.

    DATABASE_URL=postgresql+asyncpg://gateway:***@127.0.0.1:5432/local_ai_gateway_p4 \
        .venv/bin/python scripts/nrb_rag_ingest_corpus.py \
            --department nrb-p7 --cohort docs/nrb/phase7-validation-cohort.json

    ... scripts/nrb_rag_ingest_corpus.py --department nrb-p7 --report

Distinct from `scripts/nrb_rag_ingest.py`, which stays exactly as it is: that
one is the 8-blob smoke test behind §17/§18.7 and it drains in-process. This one
takes a scope and queues it. **It never drains** — a deployed worker does that,
and a driver that drained would race it (`FOR UPDATE SKIP LOCKED` means the two
would split the scope rather than collide, quietly measuring neither).

    .venv/bin/python -m app.rag.worker        # the thing that does the work

IT REFUSES TO RUN WITHOUT A SCOPE
    Same rule as `nrb_fetch.py`: the corpus is 18,266 files and a slice must be
    named. `--cohort` / `--key` / `--section` / `--owner` / `--year` /
    `--extension` / `--limit` compose; `--all` is the explicit way to say the
    whole thing, and it is not what Phase 7 step 1 is for.

RUNNING IT TWICE IS THE POINT
    A second pass over the same scope selects nothing, creates nothing and
    raises nothing, because `select_ingest_targets` anti-joins against the
    documents already in the department. That is the property
    `tests/test_nrb_corpus_ingest.py` locks, and it is what makes an interrupted
    pass resumable rather than restartable.

    `--retry-failed` is the one exception, and it is opt-in for a reason: the
    anti-join excludes `failed` documents along with everything else that is not
    archived, so a transient failure would otherwise never be picked up again.
    With the flag, failed documents IN SCOPE get a fresh job against the
    EXISTING document row — no second `documents` row, no re-copied file, and
    `ready`/`pending`/`running` documents are untouched. A deterministically
    unparseable blob (the cohort's OLE2 file) will simply fail again; that is
    the operator's call to make, and there is no transient-vs-permanent
    classifier here yet.

SCRATCH DATABASE ONLY
    Refuses unless `DATABASE_URL` names `local_ai_gateway_p4`, and prints the
    resolved database name before touching anything. The two URLs differ by one
    suffix and one of them is the real corpus.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path  # noqa: E402

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.nrb import corpus  # noqa: E402
from app.rag import repository as dept_repo  # noqa: E402

SCRATCH_DB = "local_ai_gateway_p4"


def _guard() -> str:
    url = os.environ.get("DATABASE_URL", "")
    name = url.rsplit("/", 1)[-1].split("?")[0]
    if name != SCRATCH_DB:
        print(
            f"refusing to run: DATABASE_URL resolves to database {name!r}, "
            f"but NRB work runs only against {SCRATCH_DB!r}.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    print(f"database: {name}")
    return url


def _cohort_keys(path: str) -> tuple[list[str], str, dict]:
    """Keys from a frozen cohort file, plus its fingerprint for the record."""
    payload = json.loads(Path(path).read_text())
    entries = payload.get("entries") or []
    keys = [e["comparison_key"] for e in entries]
    return keys, payload.get("cohort_sha256", ""), payload


async def _department(session, code: str):
    dept = await dept_repo.get_department_by_code(session, code)
    if dept is None:
        dept = await dept_repo.create_department(
            session, code=code, name=f"NRB corpus ({code})"
        )
        await session.flush()
        print(f"created department {code}")
    return dept


async def do_report(Session, dept_id: int) -> None:
    async with Session() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT d.status, count(*) AS docs, sum(d.chunk_count) AS chunks
                      FROM documents d WHERE d.department_id = :dept
                     GROUP BY 1 ORDER BY 2 DESC
                    """
                ),
                {"dept": dept_id},
            )
        ).mappings().all()
        jobs = (
            await session.execute(
                text(
                    """
                    SELECT j.status, count(*) AS n
                      FROM ingest_jobs j
                      JOIN documents d ON d.id = j.document_id
                     WHERE d.department_id = :dept GROUP BY 1 ORDER BY 2 DESC
                    """
                ),
                {"dept": dept_id},
            )
        ).mappings().all()
        routes = (
            await session.execute(
                text(
                    """
                    SELECT c.metadata->>'route' AS route, count(*) AS chunks,
                           count(DISTINCT c.document_id) AS docs
                      FROM document_chunks c WHERE c.department_id = :dept
                     GROUP BY 1 ORDER BY 2 DESC
                    """
                ),
                {"dept": dept_id},
            )
        ).mappings().all()
        superseded = (
            await session.execute(
                text(
                    """
                    SELECT count(*) FILTER (WHERE d.status = 'archived'
                                        AND d.metadata ? 'superseded_by') AS superseded,
                           count(*) FILTER (WHERE d.status = 'ready') AS current
                      FROM documents d
                     WHERE d.department_id = :dept
                       AND d.metadata->>'origin' = 'nrb'
                    """
                ),
                {"dept": dept_id},
            )
        ).mappings().first()
        failed = (
            await session.execute(
                text(
                    """
                    SELECT substr(d.metadata->>'blob_sha256', 1, 12) AS sha,
                           d.title, j.error
                      FROM ingest_jobs j
                      JOIN documents d ON d.id = j.document_id
                     WHERE d.department_id = :dept AND j.status = 'failed'
                     ORDER BY d.created_at
                    """
                ),
                {"dept": dept_id},
            )
        ).mappings().all()
        await session.rollback()

    print("\n--- documents ---")
    for r in rows:
        print(f"  {r['status']:<10} {r['docs']:>4} docs  {r['chunks'] or 0:>6} chunks")
    print("--- jobs ---")
    for r in jobs:
        print(f"  {r['status']:<10} {r['n']:>4}")
    if superseded:
        print("--- nrb versions ---")
        print(f"  current (ready)    {superseded['current']}")
        print(f"  superseded         {superseded['superseded']}")
    print("--- chunks by route ---")
    for r in routes:
        print(f"  {str(r['route']):<22} {r['chunks']:>6} chunks over {r['docs']} documents")
    if failed:
        print("--- failed jobs ---")
        for r in failed:
            print(f"  {r['sha']}  {(r['title'] or '')[:44]:<44} {(r['error'] or '')[:90]}")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--department", required=True, help="department code to ingest into")
    ap.add_argument("--cohort", help="frozen cohort JSON; its entries are the scope")
    ap.add_argument("--key", action="append", default=[], help="nrb_files.comparison_key")
    ap.add_argument("--section", action="append", default=[])
    ap.add_argument("--owner", action="append", default=[])
    ap.add_argument("--year", action="append", default=[], type=int)
    ap.add_argument("--extension", action="append", default=[],
                    help="catalog extension, e.g. pdf (NOT a route filter)")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--all", action="store_true",
                    help="the whole fetched catalog — say it explicitly")
    ap.add_argument("--retry-failed", action="store_true",
                    help="also requeue FAILED documents in scope, reusing the "
                         "existing document row (they may fail again)")
    ap.add_argument("--dry-run", action="store_true",
                    help="select and print, create nothing")
    ap.add_argument("--report", action="store_true", help="report status and exit")
    ap.add_argument("--json", help="write the run report here")
    args = ap.parse_args()

    url = _guard()
    settings = get_settings()
    engine = create_async_engine(url)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with Session() as session:
            dept = await _department(session, args.department)
            await session.commit()
            dept_id, dept_code = dept.id, dept.code

        if args.report:
            await do_report(Session, dept_id)
            return 0

        keys = list(args.key)
        cohort_sha = ""
        if args.cohort:
            cohort_keys, cohort_sha, payload = _cohort_keys(args.cohort)
            keys.extend(cohort_keys)
            counts = payload.get("counts", {})
            print(f"cohort: {args.cohort}\n  sha256 {cohort_sha}\n  "
                  f"{counts.get('anchor', 0)} anchors + {counts.get('unknown', 0)} "
                  f"unknown + {counts.get('unsupported', 0)} unsupported "
                  f"= {counts.get('total', len(cohort_keys))} keys")

        scoped = bool(keys or args.section or args.owner or args.year
                      or args.extension or args.limit)
        if not scoped and not args.all:
            print(
                "refusing to run without a scope. The fetched catalog is not a "
                "default. Use --cohort/--key/--section/--owner/--year/"
                "--extension/--limit, or --all to mean the whole thing.",
                file=sys.stderr,
            )
            return 2

        started = time.perf_counter()
        scope = dict(
            keys=keys or None,
            sections=args.section or None,
            owners=args.owner or None,
            years=args.year or None,
            extensions=args.extension or None,
            limit=args.limit,
        )
        async with Session() as session:
            summary = await corpus.summarise_scope(
                session, department_id=dept_id, **scope
            )
            targets = await corpus.select_ingest_targets(
                session, department_id=dept_id, **scope
            )
            retries = (
                await corpus.select_retry_targets(
                    session, department_id=dept_id, **scope
                )
                if args.retry_failed
                else []
            )
            await session.rollback()

        summary.retry_failed = len(retries)
        print(f"\nscope names {len(keys)} catalog keys / {summary.scope_blobs} blobs")
        print(f"  already_current        {summary.already_current}")
        print(f"  new_source             {summary.new_source}")
        print(f"  replacement_candidate  {summary.replacement_candidate}"
              "   (supersede their predecessor only if their ingest succeeds)")
        if args.retry_failed:
            print(f"  retry_failed           {summary.retry_failed}")
        print(f"\n{len(targets)} blobs selected (not already current in {dept_code})")
        for t in targets:
            print(f"  {t.content_sha256[:12]} .{(t.extension or '?'):<5} "
                  f"{t.title[:64]}")
        if args.retry_failed:
            print(f"\n--retry-failed: {len(retries)} FAILED documents in scope")
            for r in retries:
                print(f"  {r.content_sha256[:12]} {r.title[:64]}")

        if args.dry_run:
            print("\n(dry run — nothing created, nothing requeued)")
            return 0
        if not targets and not retries:
            print("\nnothing to do: every blob in scope is already ingested here.")
            await do_report(Session, dept_id)
            return 0

        retry_outcome = None
        if retries:
            retry_outcome = await corpus.requeue_failed(Session, targets=retries)
            print(f"\nrequeued {retry_outcome.requeued} failed documents "
                  f"(same document rows; they may fail again)")
            print(f"  conflict_job       {retry_outcome.conflict_job}")
            for err in retry_outcome.errors:
                print(f"  !! {err}")

        if not targets:
            elapsed = round(time.perf_counter() - started, 1)
            print(f"\nno new blobs to create ({elapsed}s)")
            print("\nA DEPLOYED WORKER MUST DRAIN THESE:  "
                  ".venv/bin/python -m app.rag.worker")
            if args.json:
                Path(args.json).write_text(json.dumps(
                    {"department": dept_code, "scope_keys": len(keys),
                     "created": 0, "elapsed_seconds": elapsed,
                     "retry": retry_outcome.as_dict() if retry_outcome else None},
                    ensure_ascii=False, indent=2))
            return 0

        outcome = await corpus.create_ingest_targets(
            Session,
            department_id=dept_id,
            department_code=dept_code,
            targets=targets,
            rag_docs_dir=settings.rag_docs_dir,
            cohort=cohort_sha or None,
        )
        elapsed = round(time.perf_counter() - started, 1)
        print(f"\nqueued {outcome.created} documents in {elapsed}s")
        print(f"  selected           {outcome.selected}")
        print(f"  created            {outcome.created}")
        print(f"  conflict_document  {outcome.conflict_document}  (raced, not idempotence)")
        print(f"  conflict_job       {outcome.conflict_job}")
        print(f"  missing_blob       {outcome.missing_blob}")
        for err in outcome.errors:
            print(f"  !! {err}")
        print("\nA DEPLOYED WORKER MUST DRAIN THESE:  .venv/bin/python -m app.rag.worker")

        if args.json:
            payload = outcome.as_dict()
            payload.update(
                {"department": dept_code, "cohort_sha256": cohort_sha,
                 "scope_keys": len(keys), "elapsed_seconds": elapsed,
                 "retry": retry_outcome.as_dict() if retry_outcome else None}
            )
            Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
