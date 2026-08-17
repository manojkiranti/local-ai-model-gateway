#!/usr/bin/env python
"""Run the shared NRB pipeline. A THIN adapter over `app.nrb.pipeline`.

    DATABASE_URL=postgresql+asyncpg://gateway:***@127.0.0.1:5432/local_ai_gateway_p4 \
        .venv/bin/python scripts/nrb_pipeline.py --department nrb-p7 \
            --cohort docs/nrb/phase7-validation-cohort.json

    ... scripts/nrb_pipeline.py --status            # the most recent run
    ... scripts/nrb_pipeline.py --status --run 12   # one run, reconciled

THIS SCRIPT CONTAINS NO PIPELINE LOGIC, ON PURPOSE
    It parses arguments, calls `pipeline.start`, and prints. The admin API and
    any future schedule will call the same function with the same
    `PipelineScope`, so there is one implementation of the sequence and one
    place where its safety rules live. The older stage scripts
    (`nrb_sync.py`, `nrb_fetch.py`, `nrb_extract.py`,
    `nrb_rag_ingest_corpus.py`) are unchanged and remain the right tools for
    diagnosing a single stage.

ENQUEUEING IS NOT FINISHING
    The run ends `awaiting_jobs` whenever it queued anything: the RAG worker is
    a separate process and owns the rest.

        .venv/bin/python -m app.rag.worker      # the thing that does the work
        scripts/nrb_pipeline.py --status        # ...then ask again

    `--status` reconciles the run against its OWN jobs, so it is what moves a
    finished run to `succeeded` / `partial` / `failed`.

IT REFUSES TO RUN UNBOUNDED WITHOUT BEING TOLD
    Same rule as `nrb_fetch.py` and `nrb_rag_ingest_corpus.py`. Every stage is
    idempotent, so an unscoped incremental run is a legitimate thing for the
    code to support — but the full-corpus storage decision is still open
    (§20.7), so saying "all of it" has to be deliberate: `--all`.

SCRATCH DATABASE ONLY
    Refuses unless `DATABASE_URL` names `local_ai_gateway_p4`, and prints the
    resolved database name before touching anything.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path  # noqa: E402

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.nrb import pipeline  # noqa: E402

SCRATCH_DB = "local_ai_gateway_p4"


def _guard() -> str:
    url = os.environ.get("DATABASE_URL", "")
    name = url.rsplit("/", 1)[-1].split("?")[0]
    if name != SCRATCH_DB:
        print(f"refusing to run: DATABASE_URL resolves to database {name!r}, "
              f"but NRB work runs only against {SCRATCH_DB!r}.", file=sys.stderr)
        raise SystemExit(2)
    print(f"database: {name}")
    return url


def _cohort_keys(path: str) -> tuple[list[str], str]:
    payload = json.loads(Path(path).read_text())
    entries = payload.get("entries") or []
    return [e["comparison_key"] for e in entries], payload.get("cohort_sha256", "")


def _render(run: pipeline.RunView) -> None:
    print(f"\nrun {run.id}  [{run.status}]  stage={run.stage}  "
          f"trigger={run.trigger}"
          + (f"  by={run.requested_by}" if run.requested_by else ""))
    if run.department:
        print(f"  department      {run.department}")
    if run.error:
        print(f"  error           {run.error}")
    for stage in ("sync", "fetch", "extract", "rag"):
        counters = run.counters.get(stage)
        if not counters:
            continue
        interesting = {k: v for k, v in sorted(counters.items()) if v}
        print(f"  {stage:<8} " + (
            "  ".join(f"{k}={v}" for k, v in interesting.items()) or "(nothing to do)"
        ))
    if run.jobs:
        print("  rag jobs        " + "  ".join(
            f"{k}={v}" for k, v in sorted(run.jobs.items())
        ))
    if run.status == pipeline.PIPELINE_AWAITING:
        print("\n  waiting on the RAG worker:  .venv/bin/python -m app.rag.worker")
        print("  then re-check:              scripts/nrb_pipeline.py --status")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--department", help="department code the RAG stage targets")
    ap.add_argument("--stage", action="append", default=[], choices=pipeline.STAGES,
                    help="run only these stages (repeatable; default: all)")
    ap.add_argument("--cohort", help="frozen cohort JSON; its entries are the scope")
    ap.add_argument("--key", action="append", default=[])
    ap.add_argument("--section", action="append", default=[])
    ap.add_argument("--owner", action="append", default=[])
    ap.add_argument("--year", action="append", default=[], type=int)
    ap.add_argument("--resource-type", action="append", default=[])
    ap.add_argument("--extension", action="append", default=[])
    ap.add_argument("--limit", type=int)
    ap.add_argument("--retry-failed", action="store_true",
                    help="also requeue FAILED documents in scope (NOT a recovery "
                         "refresh — see scripts/nrb_recovery_cache.py --purge)")
    ap.add_argument("--all", action="store_true",
                    help="the whole catalog — say it explicitly")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--trigger", default="cli", choices=("cli", "api", "schedule"))
    ap.add_argument("--requested-by")
    ap.add_argument("--status", action="store_true",
                    help="report a run instead of starting one")
    ap.add_argument("--run", type=int, help="with --status: which run")
    ap.add_argument("--json", help="write the run summary here")
    args = ap.parse_args()

    url = _guard()
    engine = create_async_engine(url)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        if args.status:
            async with Session() as session:
                run = (
                    await pipeline.reconcile(session, args.run)
                    if args.run else await pipeline.latest_run(session)
                )
                if run and run.status == pipeline.PIPELINE_AWAITING and not args.run:
                    run = await pipeline.reconcile(session, run.id)
                await session.commit()
            if run is None:
                print("\nno pipeline runs recorded")
                return 0
            _render(run)
            return 0

        keys = list(args.key)
        if args.cohort:
            cohort_keys, sha = _cohort_keys(args.cohort)
            keys.extend(cohort_keys)
            print(f"cohort: {args.cohort}\n  sha256 {sha}\n  {len(cohort_keys)} keys")

        scope = pipeline.PipelineScope(
            department=args.department,
            stages=tuple(args.stage) or pipeline.STAGES,
            keys=tuple(keys),
            sections=tuple(args.section),
            owners=tuple(args.owner),
            years=tuple(args.year),
            resource_types=tuple(args.resource_type),
            extensions=tuple(args.extension),
            limit=args.limit,
            retry_failed=args.retry_failed,
            all_files=args.all,
        )
        if not scope.is_bounded and not args.all:
            print(
                "refusing to run unbounded without --all. Every stage is "
                "idempotent, so an incremental full update is supported — but "
                "the full-corpus storage decision is still open, so saying "
                "'all of it' has to be deliberate.",
                file=sys.stderr,
            )
            return 2
        if "rag" in scope.stages and not scope.department:
            print("the rag stage needs --department (or drop it with --stage)",
                  file=sys.stderr)
            return 2

        try:
            run = await pipeline.start(
                scope, trigger=args.trigger, requested_by=args.requested_by,
                engine=engine, session_factory=Session, dry_run=args.dry_run,
            )
        except pipeline.PipelineBusy as busy:
            print(f"\n{busy}", file=sys.stderr)
            if busy.run:
                _render(busy.run)
            return 3

        _render(run)
        if args.json:
            Path(args.json).write_text(json.dumps(run.as_dict(), indent=2))
        return 0 if run.status != pipeline.PIPELINE_FAILED else 1
    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
