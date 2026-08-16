#!/usr/bin/env python
"""Ingest a SMALL named set of NRB blobs into department RAG, and search them.

    DATABASE_URL=postgresql+asyncpg://gateway:***@127.0.0.1:5432/local_ai_gateway_p4 \
        .venv/bin/python scripts/nrb_rag_ingest.py --ingest --reset

    DATABASE_URL=... .venv/bin/python scripts/nrb_rag_ingest.py --search

This is a smoke test, not a benchmark. It answers one question — *does recovered
NRB text survive routing, chunking, embedding, storage and retrieval with its
page and route intact?* — over a handful of blobs whose behaviour Phase 6B
already established. Eight documents cannot measure retrieval quality and this
script does not pretend to: it reports what came back, it computes no accuracy.

SCRATCH DATABASE ONLY
    Refuses to run unless `DATABASE_URL` names `local_ai_gateway_p4`. The dev
    database is not a place to put 8 NRB documents and 150 test chunks, and the
    guard is here rather than in a comment because the two URLs differ by one
    suffix.

WHAT IT ACTUALLY EXERCISES
    The production path, not a copy of it: `documents` + `ingest_jobs` rows, then
    `rag.worker.run_once` — the same claim/parse/embed/replace the worker
    process runs, minus its polling loop. The NRB branch is reached exactly as it
    would be in production, through `documents.metadata.origin == "nrb"`.
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

from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.nrb import filestore  # noqa: E402
from app.ollama.client import OllamaClient  # noqa: E402
from app.rag import documents as docs_repo  # noqa: E402
from app.rag import jobs as jobs_repo  # noqa: E402
from app.rag import repository as dept_repo  # noqa: E402
from app.rag import storage, worker  # noqa: E402
from app.rag.embedding import embed_texts  # noqa: E402
from app.rag.models import Department, Document, DocumentChunk  # noqa: E402
from app.rag.retrieval import search_chunks  # noqa: E402

SCRATCH_DB = "local_ai_gateway_p4"
DEPARTMENT = "nrb-scratch"

# The sample. Every entry is a blob whose behaviour Phase 6B already
# established, chosen to cover one distinct routing outcome each — not for size.
SAMPLE = [
    ("075bf12eb087", "clean native Unicode PDF — extracted/clean, 2 pages"),
    ("1a9b6321aa61", "embedded Preeti+Bishall — deterministic conversion, recovered"),
    ("268bcfe86d03", "embedded Preeti circular 2007 — conversion, PARTIAL recovery"),
    ("3d2eca8b9f95", "300 dpi scan, no embedded font — PP-OCRv5"),
    ("c298efaf1f16", "needs_ocr, no text layer at all — PP-OCRv5, 3 pages"),
    ("e08988860534", "the mixed document — p1 OCR, p2-50 conversion"),
    ("7820b1f49fc1", "stripped font names /CIDFont+F1..F6 — conversion stays eligible"),
    ("8df7b02f8a13", "Preeti-encoded workbook — per-CELL conversion"),
]

# Queries chosen from text VISIBLE in the selected documents, per route. A
# generic "NRB circular" query would tell us nothing about whether the recovered
# text is what got indexed.
QUERIES = [
    ("विदेशी विनिमय व्यवस्थापन विभाग", "ocr — the scan's letterhead"),
    ("सम्पत्ति शुद्धीकरण निवारण", "native — Unicode already in the PDF"),
    ("विदेशी लगानी तथा विदेशी ऋण व्यवस्थापन विनियमावली", "mixed doc, converted pages"),
    ("इजाजतपत्रप्राप्त बैंक तथा वित्तीय संस्था", "converted Nepali, several documents"),
    ("लगानी सम्बन्धी सूचना", "ocr — the no-text-layer notice"),
]


def _guard() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if SCRATCH_DB not in url:
        print(
            f"refusing to run: DATABASE_URL must name {SCRATCH_DB}. "
            "NRB work never touches the dev database.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return url


def _find(token: str) -> Path | None:
    base = filestore.base_dir()
    matches = sorted((base / token[:2]).glob(f"{token}*"))
    return matches[0] if matches else None


async def _title_for(session, sha: str, fallback: str) -> tuple[str, dict]:
    """The document's real NRB title, from the catalog in this same database."""
    row = (
        await session.execute(
            text(
                """
                SELECT f.filename, f.source_url, f.comparison_key,
                       s.title, s.page_url, s.document_type, s.published_at
                  FROM nrb_files f
                  LEFT JOIN nrb_source_files sf ON sf.file_id = f.id
                  LEFT JOIN nrb_sources s ON s.id = sf.source_id
                 WHERE f.content_sha256 = :sha
                 ORDER BY sf.ordinal NULLS LAST
                 LIMIT 1
                """
            ),
            {"sha": sha},
        )
    ).mappings().first()
    if row is None:
        return fallback, {}
    title = (row["title"] or row["filename"] or fallback).strip()
    return title[:512], {
        "source_url": row["source_url"],
        "page_url": row["page_url"],
        "comparison_key": row["comparison_key"],
        "document_type": row["document_type"],
        "published_at": str(row["published_at"]) if row["published_at"] else None,
    }


async def _department(session) -> Department:
    dept = await dept_repo.get_department_by_code(session, DEPARTMENT)
    if dept is None:
        dept = await dept_repo.create_department(
            session, code=DEPARTMENT, name="NRB scratch corpus"
        )
        await session.flush()
    return dept


async def do_ingest(Session, settings, *, reset: bool, blobs: list[str]) -> dict:
    async with Session() as session:
        dept = await _department(session)
        dept_id = dept.id
        if reset:
            docs = (
                await session.execute(
                    select(Document).where(Document.department_id == dept_id)
                )
            ).scalars().all()
            for doc in docs:
                await session.execute(
                    DocumentChunk.__table__.delete().where(
                        DocumentChunk.document_id == doc.id
                    )
                )
                await session.execute(
                    text("DELETE FROM ingest_jobs WHERE document_id = :d"),
                    {"d": doc.id},
                )
                await session.delete(doc)
            print(f"reset: removed {len(docs)} documents from {DEPARTMENT}")
        await session.commit()

    chosen = [(b, why) for b, why in SAMPLE if not blobs or b in blobs]
    created = []
    for short, why in chosen:
        path = _find(short)
        if path is None:
            print(f"{short}: not on disk", file=sys.stderr)
            continue
        data = path.read_bytes()
        sha = path.stem
        extension = path.suffix.lstrip(".").lower()
        key = storage.mint_storage_key(DEPARTMENT, path.name)
        storage.write_document(data, key, settings.rag_docs_dir)

        async with Session() as session:
            title, catalog = await _title_for(session, sha, path.name)
            doc = await docs_repo.create_document(
                session,
                department_id=dept_id,
                title=title,
                source="upload",
                file_type=extension,
                content_hash=docs_repo.content_hash_of(data),
                storage_key=key,
                file_name=path.name,
            )
            # THE marker. `worker._load_chunks_sync` reads exactly this key and
            # nothing else; every other document parses generically.
            doc.meta = {
                "origin": "nrb",
                "blob_sha256": sha,
                "note": why,
                **{k: v for k, v in catalog.items() if v},
            }
            job = await jobs_repo.enqueue(session, document_id=doc.id)
            await session.commit()
            created.append((short, doc.id, job.id, title))
        print(f"queued {short}  {title[:70]}")

    return {"department_id": dept_id, "created": created}


async def do_drain(Session, engine, settings) -> list[dict]:
    """Run the real worker body until the queue is empty, timing each job."""
    client = OllamaClient(settings.ollama_base_url, settings.ollama_timeout)
    timings: list[dict] = []
    try:
        await worker.preflight(client, settings)
        while True:
            started = time.perf_counter()
            did = await worker.run_once(engine, client, settings)
            if not did:
                break
            timings.append({"seconds": round(time.perf_counter() - started, 2)})
            print(f"  job done in {timings[-1]['seconds']}s")
    finally:
        await client.aclose()
    return timings


async def report(Session, dept_id: int) -> None:
    async with Session() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT d.id, d.title, d.status, d.chunk_count, d.metadata->>'blob_sha256' AS sha,
                           j.status AS job_status, j.error
                      FROM documents d
                      LEFT JOIN ingest_jobs j ON j.document_id = d.id
                     WHERE d.department_id = :dept
                     ORDER BY d.created_at
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
                           count(DISTINCT c.page_number) AS pages
                      FROM document_chunks c
                     WHERE c.department_id = :dept
                     GROUP BY 1 ORDER BY 2 DESC
                    """
                ),
                {"dept": dept_id},
            )
        ).mappings().all()
        await session.rollback()

    print("\n--- documents ---")
    for r in rows:
        note = "" if r["job_status"] == "succeeded" else f"  !! {r['job_status']}: {r['error']}"
        print(f"  {(r['sha'] or '')[:12]}  {r['status']:<8} {r['chunk_count']:>4} chunks  "
              f"{(r['title'] or '')[:52]}{note}")
    print("\n--- chunks by route ---")
    for r in routes:
        print(f"  {str(r['route']):<20} {r['chunks']:>4} chunks over {r['pages']} distinct page numbers")


async def do_search(Session, settings, dept_id: int, limit: int) -> None:
    client = OllamaClient(settings.ollama_base_url, settings.ollama_timeout)
    try:
        for query, why in QUERIES:
            vector = (
                await embed_texts(
                    client, [query], mode="query",
                    model=settings.rag_embed_model,
                    dim=settings.rag_embed_dim,
                    batch_size=settings.rag_embed_batch,
                )
            )[0]
            hits = await search_chunks(
                department_id=dept_id,
                query_text=query,
                query_vector=vector,
                limit=limit,
                candidate_pool=settings.rag_rerank_pool,
                rrf_k=settings.rag_rrf_k,
                ef_search=settings.rag_hnsw_ef_search,
            )
            print(f"\n### {query}\n    ({why})")
            if not hits:
                print("    NO HITS")
                continue
            # `search_chunks` deliberately does not return chunk metadata — it is
            # the generic retrieval surface. The route is read back here, which is
            # also proof it survived the round trip into Postgres.
            async with Session() as session:
                meta = dict(
                    (
                        await session.execute(
                            text(
                                "SELECT id, metadata FROM document_chunks "
                                "WHERE id = ANY(:ids)"
                            ),
                            {"ids": [h.chunk_id for h in hits]},
                        )
                    ).all()
                )
                await session.rollback()
            for rank, hit in enumerate(hits, start=1):
                m = meta.get(hit.chunk_id) or {}
                page = "-" if hit.page_number is None else f"p{hit.page_number}"
                print(
                    f"  {rank}. {hit.title[:44]:<44} {page:<5} "
                    f"{str(m.get('route')):<18} rrf={hit.rrf_score:.4f}"
                )
                print(f"     {' '.join(hit.content.split())[:150]}")
    finally:
        await client.aclose()


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ingest", action="store_true")
    ap.add_argument("--search", action="store_true")
    ap.add_argument("--reset", action="store_true", help="clear the scratch department first")
    ap.add_argument("--blob", action="append", default=[], help="restrict the sample")
    ap.add_argument("--limit", type=int, default=5, help="hits per query")
    ap.add_argument("--json", help="write the ingest report here")
    args = ap.parse_args()
    if not (args.ingest or args.search):
        ap.error("choose --ingest and/or --search")

    url = _guard()
    settings = get_settings()
    engine = create_async_engine(url)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    report_data: dict = {}
    try:
        async with Session() as session:
            dept = await _department(session)
            await session.commit()
            dept_id = dept.id

        if args.ingest:
            started = time.perf_counter()
            result = await do_ingest(Session, settings, reset=args.reset, blobs=args.blob)
            dept_id = result["department_id"]
            print(f"\ndraining {len(result['created'])} jobs ...")
            timings = await do_drain(Session, engine, settings)
            elapsed = round(time.perf_counter() - started, 1)
            print(f"\ningest wall clock: {elapsed}s for {len(timings)} jobs")
            report_data = {"elapsed_seconds": elapsed, "jobs": timings}
            await report(Session, dept_id)

        if args.search:
            await do_search(Session, settings, dept_id, args.limit)
    finally:
        await engine.dispose()

    if args.json and report_data:
        Path(args.json).write_text(json.dumps(report_data, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
