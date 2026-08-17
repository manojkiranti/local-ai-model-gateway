#!/usr/bin/env python
"""End-to-end supersession exercise on SYNTHETIC NRB records (Phase 7 step 3).

    DATABASE_URL=postgresql+asyncpg://gateway:***@127.0.0.1:5432/local_ai_gateway_p4 \
        .venv/bin/python scripts/nrb_supersession_exercise.py
    ... scripts/nrb_supersession_exercise.py --cleanup

WHY SYNTHETIC, AND WHY THAT IS THE STRONGER CHOICE
    The whole exercise is about VERSION ORDER, and NRB's catalog keeps none: a
    republished file overwrites `nrb_files.content_sha256` in place, so there is
    no real record whose successive versions could be replayed. Four generated
    PDFs under one `comparison_key` give a deterministic order, and they do it
    without mutating a single row of real catalog evidence — the frozen Phase 6
    cohorts, the 31-document Phase 7 cohort and the real blobs are untouched.

    Everything downstream of the fixture is REAL: the corpus driver selects
    them, `app.rag.worker` recovers, chunks and embeds them, promotion runs in
    the worker's own transaction, and the retrieval assertions go through the
    production hybrid-search SQL.

WHAT IT PROVES, IN ORDER
    1. A ingests and is retrievable.
    2. New bytes for the same key report as a `replacement_candidate`, not a
       `new_source`, and A keeps serving while B is queued.
    3. B FAILS (its stored file is removed) — A is still current and still
       retrievable. This is the rule the whole task exists for.
    4. `--retry-failed` requeues B, it succeeds, and only THEN is A archived.
       Retrieval now returns B and never A.
    5. C fails — B is still current. A failed candidate never retires anything.
    6. D succeeds, superseding B *and* retiring the still-failed C. A version a
       newer one has already superseded is then no longer retryable, so an
       operator cannot resurrect a stale revision after the fact.

    Step 6 is the one that cannot be inferred from the others: it is where
    version order and completion order come apart. The complementary case — an
    older candidate that is still live when a newer one is already current —
    cannot be produced by this flow (the newer one's promotion archives it
    first) and is covered by `tests/test_nrb_supersession.py`, where the state
    can be constructed directly.

SCRATCH DATABASE ONLY, AND IT CLEANS UP AFTER ITSELF
    Refuses unless `DATABASE_URL` names `local_ai_gateway_p4`. Everything lives
    in one throwaway department and one throwaway `nrb_files` row, removed at
    the end (and by `--cleanup` if a run was interrupted).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path  # noqa: E402

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.nrb import corpus  # noqa: E402
from app.ollama.client import OllamaClient  # noqa: E402
from app.rag import repository as dept_repo  # noqa: E402
from app.rag import worker  # noqa: E402
from app.rag.retrieval import _SEARCH_SQL, _vector_literal  # noqa: E402

SCRATCH_DB = "local_ai_gateway_p4"
DEPT = "nrb-p7-supersede"
KEY = "https://www.nrb.org.np/exercise/monetary-policy-circular.pdf"
VERSIONS = ("ALPHA", "BRAVO", "CHARLIE", "DELTA")


def _guard() -> str:
    url = os.environ.get("DATABASE_URL", "")
    name = url.rsplit("/", 1)[-1].split("?")[0]
    if name != SCRATCH_DB:
        print(f"refusing to run: DATABASE_URL resolves to {name!r}, not "
              f"{SCRATCH_DB!r}.", file=sys.stderr)
        raise SystemExit(2)
    print(f"database: {name}")
    return url


def _pdf(marker: str) -> bytes:
    """A tiny real PDF with distinctive, searchable English text."""
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=14)
    pdf.cell(0, 10, "Nepal Rastra Bank monetary policy circular", ln=1)
    pdf.cell(0, 10, f"This is revision {marker} of the circular.", ln=1)
    pdf.multi_cell(
        0, 8,
        f"Revision {marker} restates the cash reserve ratio and the statutory "
        f"liquidity ratio for licensed institutions. Marker {marker} appears "
        f"only in revision {marker} of this document.",
    )
    return bytes(pdf.output())


async def _publish(Session, marker: str) -> str:
    """Put one version's bytes in the blob store and point the catalog at them.

    This is the synthetic half: exactly what a sync + fetch pass would leave
    behind for a republished file — the same `comparison_key`, a new
    `content_sha256`, and the previous version's blob still on disk.
    """
    from app.nrb import filestore

    data = _pdf(marker)
    sha = hashlib.sha256(data).hexdigest()
    storage_key = f"{sha[:2]}/{sha}.pdf"
    path = filestore.resolve_path(storage_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)

    async with Session() as session:
        await session.execute(
            text(
                """
                INSERT INTO nrb_files (comparison_key, source_url, filename,
                    extension, resource_type, type_source, host, fetch_status,
                    content_sha256, content_length, storage_key)
                VALUES (:k, :k, 'circular.pdf', 'pdf', 'document', 'extension',
                        'www.nrb.org.np', 'fetched', :sha, :len, :store)
                ON CONFLICT (comparison_key) DO UPDATE
                   SET content_sha256 = EXCLUDED.content_sha256,
                       storage_key    = EXCLUDED.storage_key,
                       content_length = EXCLUDED.content_length
                """
            ),
            {"k": KEY, "sha": sha, "len": len(data), "store": storage_key},
        )
        await session.commit()
    return sha


async def _drive(Session, dept_id: int, dept_code: str, *, retry: bool = False):
    """One corpus-driver pass. Returns `(summary, created, requeued)`."""
    settings = get_settings()
    async with Session() as session:
        summary = await corpus.summarise_scope(
            session, department_id=dept_id, keys=[KEY]
        )
        targets = await corpus.select_ingest_targets(
            session, department_id=dept_id, keys=[KEY]
        )
        retries = (
            await corpus.select_retry_targets(
                session, department_id=dept_id, keys=[KEY]
            )
            if retry else []
        )
        await session.rollback()

    created = 0
    if targets:
        out = await corpus.create_ingest_targets(
            Session, department_id=dept_id, department_code=dept_code,
            targets=targets, rag_docs_dir=settings.rag_docs_dir,
        )
        created = out.created
    requeued = 0
    if retries:
        requeued = (await corpus.requeue_failed(Session, targets=retries)).requeued
    return summary, created, requeued


async def _drain(engine, client, settings) -> int:
    handled = 0
    while await worker.run_once(engine, client, settings):
        handled += 1
    return handled


async def _state(Session, dept_id: int) -> list[tuple[str, str, str, str | None]]:
    async with Session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT d.id, d.status, substr(d.content_hash, 1, 12), "
                    "       d.metadata->>'superseded_by' "
                    "  FROM documents d WHERE d.department_id = :d "
                    " ORDER BY d.created_at"
                ),
                {"d": dept_id},
            )
        ).all()
        await session.rollback()
    return [tuple(r) for r in rows]


async def _retrieve(Session, dept_id: int, query: str) -> set[str]:
    """Markers found by the PRODUCTION retrieval SQL. Lexical channel only —
    the dense vector is a constant, so what this asserts is document filtering,
    not ranking quality."""
    settings = get_settings()
    dim = settings.rag_embed_dim
    async with Session() as session:
        rows = (
            await session.execute(
                text(_SEARCH_SQL),
                {
                    "qvec": _vector_literal([0.0] * (dim - 1) + [1.0]),
                    "qtext": query, "dept": dept_id,
                    "pool": 100, "rrf_k": 60, "limit": 20,
                },
            )
        ).mappings().all()
        await session.rollback()
    return {m for m in VERSIONS for r in rows if m in r["content"]}


async def _hide(Session, dept_id: int, sha_prefix: str, settings) -> Path:
    """Remove a candidate's stored copy so its ingest fails.

    A missing `RAG_DOCS_DIR` file is a real failure mode (§18's storage
    defects), it fails in the parse stage exactly where a recovery failure
    would, and it is trivially reversible — which is what a failure/retry
    exercise needs.
    """
    async with Session() as session:
        key = (
            await session.execute(
                text("SELECT storage_key FROM documents WHERE department_id = :d "
                     "  AND content_hash LIKE :p || '%'"),
                {"d": dept_id, "p": sha_prefix},
            )
        ).scalar_one()
        await session.rollback()
    from app.rag.storage import resolve_storage_path

    path = resolve_storage_path(key, settings.rag_docs_dir)
    hidden = path.with_suffix(path.suffix + ".hidden")
    path.rename(hidden)
    return hidden


def _restore(hidden: Path) -> None:
    hidden.rename(hidden.with_suffix(""))


async def _cleanup(Session) -> None:
    async with Session() as session:
        for statement in (
            "DELETE FROM document_chunks WHERE department_id IN "
            "(SELECT id FROM departments WHERE code = :c)",
            "DELETE FROM ingest_jobs WHERE document_id IN (SELECT d.id FROM documents d"
            " JOIN departments dp ON dp.id = d.department_id WHERE dp.code = :c)",
            "DELETE FROM documents WHERE department_id IN "
            "(SELECT id FROM departments WHERE code = :c)",
            "DELETE FROM departments WHERE code = :c",
        ):
            await session.execute(text(statement), {"c": DEPT})
        await session.execute(
            text("DELETE FROM nrb_files WHERE comparison_key = :k"), {"k": KEY}
        )
        await session.commit()
    print("cleaned up the exercise department, catalog row and jobs")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cleanup", action="store_true", help="remove and exit")
    ap.add_argument("--keep", action="store_true", help="leave the rows behind")
    args = ap.parse_args()

    url = _guard()
    settings = get_settings()
    engine = create_async_engine(url)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    client = OllamaClient(settings.ollama_base_url, timeout=settings.ollama_timeout)
    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}{('  — ' + detail) if detail else ''}")
        if not ok:
            failures.append(label)

    try:
        if args.cleanup:
            await _cleanup(Session)
            return 0
        await _cleanup(Session)

        async with Session() as session:
            dept = await dept_repo.get_department_by_code(session, DEPT)
            if dept is None:
                dept = await dept_repo.create_department(
                    session, code=DEPT, name="Phase 7 supersession exercise"
                )
            await session.commit()
            dept_id, dept_code = dept.id, dept.code

        # --- 1. A ------------------------------------------------------- #
        print("\n1. publish revision ALPHA and ingest it")
        sha_a = await _publish(Session, "ALPHA")
        summary, created, _ = await _drive(Session, dept_id, dept_code)
        check("ALPHA is a new_source", summary.new_source == 1 and created == 1,
              f"new_source={summary.new_source} replacement={summary.replacement_candidate}")
        await _drain(engine, client, settings)
        state = await _state(Session, dept_id)
        check("ALPHA is current", state == [(state[0][0], "ready", sha_a[:12], None)],
              str(state))
        check("ALPHA is retrievable",
              await _retrieve(Session, dept_id, "cash reserve ratio") == {"ALPHA"})

        # --- 2/3. B is published, then FAILS -------------------------- #
        print("\n2. publish revision BRAVO — a replacement candidate")
        sha_b = await _publish(Session, "BRAVO")
        summary, created, _ = await _drive(Session, dept_id, dept_code)
        check("BRAVO is a replacement_candidate, not a new_source",
              summary.replacement_candidate == 1 and summary.new_source == 0
              and created == 1,
              f"replacement={summary.replacement_candidate} new={summary.new_source}")
        check("ALPHA still current while BRAVO is queued",
              [s for s in await _state(Session, dept_id) if s[1] == "ready"]
              == [(state[0][0], "ready", sha_a[:12], None)])

        print("\n3. BRAVO's ingest FAILS")
        hidden = await _hide(Session, dept_id, sha_b[:12], settings)
        await _drain(engine, client, settings)
        state = await _state(Session, dept_id)
        ready = [s for s in state if s[1] == "ready"]
        check("BRAVO failed", any(s[2] == sha_b[:12] and s[1] == "failed" for s in state),
              str(state))
        check("ALPHA REMAINS CURRENT after the failed replacement",
              len(ready) == 1 and ready[0][2] == sha_a[:12], str(ready))
        check("ALPHA is still retrievable",
              await _retrieve(Session, dept_id, "cash reserve ratio") == {"ALPHA"})

        # --- 4. retry B, it succeeds, only NOW is A archived ----------- #
        print("\n4. --retry-failed BRAVO; it succeeds and supersedes ALPHA")
        _restore(hidden)
        _, _, requeued = await _drive(Session, dept_id, dept_code, retry=True)
        check("BRAVO requeued", requeued == 1)
        await _drain(engine, client, settings)
        state = await _state(Session, dept_id)
        ready = [s for s in state if s[1] == "ready"]
        archived = {s[2]: s[3] for s in state if s[1] == "archived"}
        check("BRAVO is current", len(ready) == 1 and ready[0][2] == sha_b[:12],
              str(state))
        check("ALPHA archived and stamped superseded_by",
              sha_a[:12] in archived and archived[sha_a[:12]] is not None)
        check("retrieval returns BRAVO and not ALPHA",
              await _retrieve(Session, dept_id, "cash reserve ratio") == {"BRAVO"})

        # --- 5. C fails; B keeps serving ------------------------------ #
        print("\n5. publish CHARLIE and let it FAIL — BRAVO must keep serving")
        sha_c = await _publish(Session, "CHARLIE")
        await _drive(Session, dept_id, dept_code)
        hidden_c = await _hide(Session, dept_id, sha_c[:12], settings)
        await _drain(engine, client, settings)
        ready = [s for s in await _state(Session, dept_id) if s[1] == "ready"]
        check("BRAVO still current after CHARLIE failed",
              len(ready) == 1 and ready[0][2] == sha_b[:12], str(ready))
        check("retrieval still returns BRAVO",
              await _retrieve(Session, dept_id, "cash reserve ratio") == {"BRAVO"})

        # --- 6. D succeeds; the stale failed C is retired, not retried - #
        print("\n6. DELTA succeeds — and retires the still-failed CHARLIE")
        sha_d = await _publish(Session, "DELTA")
        await _drive(Session, dept_id, dept_code)
        await _drain(engine, client, settings)
        state = await _state(Session, dept_id)
        ready = [s for s in state if s[1] == "ready"]
        charlie = next(s for s in state if s[2] == sha_c[:12])
        check("DELTA is current", len(ready) == 1 and ready[0][2] == sha_d[:12],
              str(ready))
        check("the failed CHARLIE was archived by the newer DELTA, not left "
              "retryable", charlie[1] == "archived" and charlie[3] is not None,
              str(charlie))

        # Retrying a version a newer one has already superseded is a no-op, and
        # that is the point: `select_retry_targets` requires `failed`, and
        # promotion archived it. An operator cannot resurrect a stale version by
        # retrying it after the fact.
        _restore(hidden_c)
        _, _, requeued = await _drive(Session, dept_id, dept_code, retry=True)
        check("a superseded failure is no longer retryable", requeued == 0,
              f"requeued={requeued}")
        await _drain(engine, client, settings)
        ready = [s for s in await _state(Session, dept_id) if s[1] == "ready"]
        check("DELTA is still the only current version",
              len(ready) == 1 and ready[0][2] == sha_d[:12], str(ready))
        check("retrieval returns DELTA only",
              await _retrieve(Session, dept_id, "cash reserve ratio") == {"DELTA"})

        # --- history --------------------------------------------------- #
        print("\n7. history is intact")
        async with Session() as session:
            recoveries = (
                await session.execute(
                    text("SELECT count(*) FROM nrb_recoveries WHERE content_sha256 "
                         "= ANY(:s)"),
                    {"s": [sha_a, sha_b, sha_c, sha_d]},
                )
            ).scalar_one()
            files = (
                await session.execute(
                    text("SELECT count(*) FROM nrb_files WHERE comparison_key = :k"),
                    {"k": KEY},
                )
            ).scalar_one()
            await session.rollback()
        from app.nrb import filestore

        blobs = sum(
            1 for s in (sha_a, sha_b, sha_c, sha_d)
            if filestore.resolve_path(f"{s[:2]}/{s}.pdf").exists()
        )
        check("all four blobs still on disk", blobs == 4, f"{blobs}/4")
        # Three, not four: CHARLIE never recovered — its stored copy was hidden,
        # so the job failed before the recovery cache was reached, and DELTA
        # then archived it. The claim being tested is that ARCHIVING a version
        # does not purge its recovery, which ALPHA and BRAVO carry.
        check("recovery rows survive being archived", recoveries == 3,
              f"{recoveries} rows for the 3 versions that ever recovered")
        check("the catalog row survives", files == 1)

        print(f"\n=== {'ALL CHECKS PASSED' if not failures else 'FAILURES: ' + str(failures)} ===")
        return 0 if not failures else 1
    finally:
        if not args.keep:
            await _cleanup(Session)
        await client.aclose()
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
