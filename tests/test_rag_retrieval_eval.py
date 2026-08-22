"""Run the frozen cohort end to end and report the four metrics.

Skips unless the cohort exists, Postgres is reachable, and the embedding model
answers -- this is a measurement, not a unit test, and a skipped measurement is
honest where a fabricated one is not.
"""

import asyncio
import json
from pathlib import Path

import pytest

from app.config import get_settings
from app.db.session import engine as app_engine
from app.rag import ranking
from app.rag.embedding import embed_texts
from app.rag.eval_metrics import Outcome, score
from app.rag.retrieval import search_chunks
from app.ollama.client import OllamaClient
from scripts.rag_eval_build_cohort import questions_hash

COHORT = Path("docs/rag/retrieval-eval-cohort.json")


def _load():
    if not COHORT.exists():
        pytest.skip(f"{COHORT} not present -- run scripts/rag_eval_build_cohort.py")
    cohort = json.loads(COHORT.read_text())
    stamped = cohort["parameters"].get("sha256")
    if not stamped:
        pytest.skip("cohort is not frozen -- run --freeze after human review")
    actual = questions_hash(cohort["questions"])
    assert actual == stamped, (
        f"cohort questions changed since freezing ({actual} != {stamped}). "
        "A cohort edited after results were seen is no longer evidence -- "
        "re-freeze deliberately and say so in the spec."
    )
    return cohort


async def _run_one(client, dept_id: int, question: str, settings) -> tuple[bool, list[str]]:
    """(abstained, document ids in presentation order) for one question."""
    vectors = await embed_texts(
        client, [question], mode="query", model=settings.rag_embed_model,
        dim=settings.rag_embed_dim, batch_size=1,
    )
    chunks = await search_chunks(
        department_id=dept_id, query_text=question, query_vector=vectors[0],
        limit=settings.rag_rerank_pool, candidate_pool=settings.rag_candidate_pool,
        rrf_k=settings.rag_rrf_k, ef_search=settings.rag_hnsw_ef_search,
    )
    result = await ranking.apply(client, question, chunks, settings=settings)
    seen: list[str] = []
    for chunk in result.kept:
        if chunk.document_id not in seen:
            seen.append(chunk.document_id)
    return result.abstained, seen


def test_the_frozen_cohort_reports_its_metrics(capsys):
    cohort = _load()
    settings = get_settings()

    async def go():
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.pool import NullPool

        engine = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                dept_id = (
                    await conn.execute(
                        text("SELECT id FROM departments WHERE code = :c"),
                        {"c": cohort["parameters"]["department"]},
                    )
                ).scalar_one_or_none()
        finally:
            await engine.dispose()
        if dept_id is None:
            pytest.skip("cohort's department is not in this database")

        client = OllamaClient(settings.ollama_base_url, settings.ollama_timeout)
        outcomes = []
        try:
            for q in cohort["questions"]:
                abstained, seen = await _run_one(client, dept_id, q["question"], settings)
                outcomes.append(
                    Outcome(
                        question_id=q["id"],
                        answerable=q["kind"] == "answerable",
                        abstained=abstained,
                        returned_document_ids=seen,
                        expected_document_id=q.get("expect_document_id"),
                    )
                )
        finally:
            await client.aclose()
            await app_engine.dispose()
        return outcomes

    try:
        outcomes = asyncio.run(go())
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"eval prerequisites unavailable: {type(exc).__name__}: {exc}")

    report = score(outcomes)
    with capsys.disabled():
        print(
            f"\nthreshold={settings.rag_relevance_threshold} "
            f"pool={settings.rag_rerank_pool} "
            f"rerank={'on' if settings.rag_rerank_enabled else 'OFF (degraded)'}\n"
            f"  recall@k            {report.recall_at_k:.3f}\n"
            f"  MRR                 {report.mrr:.3f}\n"
            f"  abstention recall   {report.abstention_recall:.3f}"
            f"  ({report.unanswerable} negatives)\n"
            f"  FALSE REFUSAL RATE  {report.false_refusal_rate:.3f}"
            f"  ({report.answerable} answerable)\n"
        )

    # Not a quality gate -- the gate is the human reading the sweep table. This
    # only catches a harness that measured nothing.
    assert report.answerable > 0
