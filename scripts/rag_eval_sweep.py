"""Sweep the abstention threshold and pool size over the frozen cohort.

Scores each question ONCE and reuses those scores across every threshold — the
reranker is the expensive part and a threshold is just a comparison. 9 thresholds
x 2 pools over 50 questions costs ONE scoring pass, not 18.

The pools are scored ONCE too: `search_chunks` differs between pool sizes only by
the final LIMIT over an RRF-ordered set, so the pool-10 candidate list is exactly
the first 10 of the pool-20 list. Scoring the largest pool and slicing halves the
reranker passes.

`RAG_RERANK_ENABLED` is deliberately NOT required: the sweep calls `rerank`
directly rather than going through `ranking.apply`, so the flag has no effect
here. The flag gates the SERVING path, and it stays false until this sweep has
fitted a threshold.

Usage:
  DATABASE_URL=... .venv/bin/python scripts/rag_eval_sweep.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio  # noqa: E402
import json  # noqa: E402
from pathlib import Path  # noqa: E402

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.session import engine as app_engine  # noqa: E402
from app.ollama.client import OllamaClient  # noqa: E402
from app.rag.embedding import embed_texts  # noqa: E402
from app.rag.eval_metrics import Outcome, score  # noqa: E402
from app.rag.ranking import NO_SIGNAL_SCORE, decide  # noqa: E402
from app.rag.rerank import rerank  # noqa: E402
from app.rag.retrieval import search_chunks  # noqa: E402
from scripts.rag_eval_build_cohort import questions_hash  # noqa: E402

COHORT = Path("docs/rag/retrieval-eval-cohort.json")
THRESHOLDS = [round(0.1 * i, 1) for i in range(1, 10)]
POOLS = [10, 20]


async def _scored(client, dept_id, cohort, pool, settings):
    """(question, chunks, scores) for every cohort question at this pool size."""
    out = []
    for q in cohort["questions"]:
        vectors = await embed_texts(
            client, [q["question"]], mode="query", model=settings.rag_embed_model,
            dim=settings.rag_embed_dim, batch_size=1,
        )
        chunks = await search_chunks(
            department_id=dept_id, query_text=q["question"], query_vector=vectors[0],
            limit=pool, candidate_pool=settings.rag_candidate_pool,
            rrf_k=settings.rag_rrf_k, ef_search=settings.rag_hnsw_ef_search,
        )
        scores = (
            await rerank(client, q["question"], [c.content for c in chunks],
                         model=settings.rag_rerank_model)
            if chunks else []
        )
        out.append((q, chunks, scores))
        print(f"  scored {q['id']} ({len(chunks)} candidates)", flush=True)
    return out


def _report_for(scored, threshold, top_k):
    outcomes = []
    for q, chunks, scores in scored:
        result = decide(chunks, scores, threshold=threshold, top_k=top_k)
        seen: list[str] = []
        for chunk in result.kept:
            if chunk.document_id not in seen:
                seen.append(chunk.document_id)
        outcomes.append(
            Outcome(
                question_id=q["id"],
                answerable=q["kind"] == "answerable",
                # No candidates at all is not an abstention decision; it is the
                # tool's separate zero-results branch. Counted as a refusal here
                # because from the USER's seat it is one.
                abstained=result.abstained or not chunks,
                returned_document_ids=seen,
                expected_document_id=q.get("expect_document_id"),
            )
        )
    return score(outcomes)


async def main() -> None:
    settings = get_settings()
    cohort = json.loads(COHORT.read_text())
    assert questions_hash(cohort["questions"]) == cohort["parameters"]["sha256"], (
        "cohort hash mismatch — the questions changed since freezing"
    )

    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            dept_id = (
                await conn.execute(
                    text("SELECT id FROM departments WHERE code = :c"),
                    {"c": cohort["parameters"]["department"]},
                )
            ).scalar_one()
    finally:
        await engine.dispose()

    rows = []
    client = OllamaClient(settings.ollama_base_url, settings.ollama_timeout)
    try:
        # Score the LARGEST pool only, then slice for the smaller ones. Valid
        # because the pool is the retrieval query's final LIMIT over a set already
        # ordered by rrf_score DESC: the first N rows of a wider pool are exactly
        # the pool-N rows, in the same order. Scores are per (query, passage), so a
        # sliced score list still pairs with its sliced chunk list. This halves the
        # GPU cost of the sweep — the reranker is the only expensive part.
        largest = max(POOLS)
        print(f"scoring at pool={largest} …", flush=True)
        full = await _scored(client, dept_id, cohort, largest, settings)
        for pool in POOLS:
            scored = (
                full if pool == largest
                else [(q, c[:pool], s[:pool]) for q, c, s in full]
            )
            for threshold in THRESHOLDS:
                report = _report_for(scored, threshold, settings.rag_top_k)
                rows.append((pool, threshold, report))
    finally:
        await client.aclose()
        await app_engine.dispose()

    print("\n| pool | threshold | recall@k | MRR | abstention recall | false refusal |")
    print("|---|---|---|---|---|---|")
    for pool, threshold, r in rows:
        flag = " *(no-signal value)*" if threshold == NO_SIGNAL_SCORE else ""
        print(
            f"| {pool} | {threshold}{flag} | {r.recall_at_k:.3f} | {r.mrr:.3f} "
            f"| {r.abstention_recall:.3f} | {r.false_refusal_rate:.3f} |"
        )
    print(
        f"\nCohort: {rows[0][2].answerable} answerable, "
        f"{rows[0][2].unanswerable} unanswerable. "
        f"{NO_SIGNAL_SCORE} is disqualified as an operating point."
    )


if __name__ == "__main__":
    asyncio.run(main())
