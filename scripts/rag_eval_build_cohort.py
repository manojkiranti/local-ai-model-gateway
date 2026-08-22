"""Build eval-cohort CANDIDATES from a department's ingested chunks.

Answerable questions are generated from chunks, which gives a free gold label:
the chunk's document is the expected document. Unanswerable negatives are
ideally real questions built from a DIFFERENT department's chunks -- real
questions about real documents that this department genuinely does not hold.
When no second populated department exists (--negatives-from omitted), a
placeholder is emitted instead, forcing a human to author the negatives.

**Output is candidates, not a cohort.** A human reviews and edits, then re-runs
with --freeze to stamp the hash. Limits, restated in the file it writes:

1. Chunk-derived questions reuse their source document's vocabulary, so they
   flatter the lexical channel. The answerable set measures an UPPER BOUND on
   recall; the negatives carry the trustworthy signal.
2. One department's corpus supports no population claim.

Usage:
  DATABASE_URL=... .venv/bin/python scripts/rag_eval_build_cohort.py \
      --department hr --negatives-from finance --answerable 40 --negatives 10 \
      --out docs/rag/retrieval-eval-cohort.json
  # or, with only one populated department:
  DATABASE_URL=... .venv/bin/python scripts/rag_eval_build_cohort.py \
      --department risk_compliance --answerable 40 --negatives 10 \
      --out docs/rag/retrieval-eval-cohort.json
  # human edits the file, then:
  DATABASE_URL=... .venv/bin/python scripts/rag_eval_build_cohort.py \
      --freeze docs/rag/retrieval-eval-cohort.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.config import get_settings  # noqa: E402

# Long enough to carry a real claim, short enough that a generated question is
# about one thing.
MIN_CHUNK_CHARS = 300


def questions_hash(questions: list[dict]) -> str:
    payload = json.dumps(
        questions, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode()
    return hashlib.sha256(payload).hexdigest()


async def _sample(engine, department_code: str, limit: int) -> list[dict]:
    sql = text(
        """
        SELECT c.content, c.section, c.document_id, d.title
          FROM document_chunks c
          JOIN documents   d ON d.id = c.document_id
          JOIN departments dep ON dep.id = c.department_id
         WHERE dep.code = :code
           AND d.status = 'ready'
           AND length(c.content) >= :min_chars
         ORDER BY c.document_id, c.chunk_index
        """
    )
    async with engine.connect() as conn:
        rows = (
            await conn.execute(sql, {"code": department_code, "min_chars": MIN_CHUNK_CHARS})
        ).mappings().all()

    # Spread across documents rather than taking the first N chunks of the first
    # document -- otherwise a 24-document corpus is measured on two of them.
    by_doc: dict[str, list[dict]] = {}
    for row in rows:
        by_doc.setdefault(row["document_id"], []).append(dict(row))
    rng = random.Random(20260822)  # fixed: regenerating must not reshuffle
    picked: list[dict] = []
    while len(picked) < limit and any(by_doc.values()):
        for doc_id in list(by_doc):
            bucket = by_doc[doc_id]
            if not bucket:
                del by_doc[doc_id]
                continue
            picked.append(bucket.pop(rng.randrange(len(bucket))))
            if len(picked) == limit:
                break
    return picked


def _draft_question(row: dict) -> str:
    """A placeholder a human rewrites. Deliberately NOT model-generated here:
    the generator must run with no GPU, and a human is reviewing every line
    anyway."""
    head = " ".join(row["content"].split())[:160]
    section = row["section"] or row["title"]
    return f"[REVIEW — rewrite as a user question about: {section}] {head}"


def _placeholder_negatives(count: int) -> list[dict]:
    return [
        {
            "id": f"n{i:03d}",
            "kind": "unanswerable",
            "question": (
                "[REVIEW — write a question this department's corpus "
                "genuinely cannot answer]"
            ),
            "why": (
                "human-authored: no second populated department exists to "
                "borrow from"
            ),
        }
        for i in range(1, count + 1)
    ]


async def build(args) -> dict:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        positives = await _sample(engine, args.department, args.answerable)
        if args.negatives_from:
            neg_rows = await _sample(engine, args.negatives_from, args.negatives)
        else:
            neg_rows = None
        async with engine.connect() as conn:
            doc_count = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM documents d JOIN departments dep"
                        " ON dep.id = d.department_id"
                        " WHERE dep.code = :c AND d.status = 'ready'"
                    ),
                    {"c": args.department},
                )
            ).scalar_one()
            chunk_count = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM document_chunks c JOIN departments dep"
                        " ON dep.id = c.department_id WHERE dep.code = :c"
                    ),
                    {"c": args.department},
                )
            ).scalar_one()
    finally:
        await engine.dispose()

    if not positives:
        raise SystemExit(
            f"no ready documents with chunks >= {MIN_CHUNK_CHARS} chars in "
            f"department '{args.department}' — ingest the corpus first"
        )

    questions = [
        {
            "id": f"q{i:03d}",
            "kind": "answerable",
            "question": _draft_question(row),
            "expect_document_id": row["document_id"],
            "expect_section": row["section"],
        }
        for i, row in enumerate(positives, start=1)
    ]

    if neg_rows is not None:
        questions += [
            {
                "id": f"n{i:03d}",
                "kind": "unanswerable",
                "question": _draft_question(row),
                "why": f"drawn from the '{args.negatives_from}' corpus, not '{args.department}'",
            }
            for i, row in enumerate(neg_rows, start=1)
        ]
    else:
        questions += _placeholder_negatives(args.negatives)

    limitations = [
        "Chunk-derived questions reuse their source document's vocabulary, "
        "so they flatter the lexical channel. The answerable set measures an "
        "upper bound on recall; the negatives carry the trustworthy signal.",
        "One department's corpus supports no population claim. Re-sweep for a "
        "corpus that differs in size or character.",
        "expect_document_id is DOCUMENT granularity: the right document via "
        "the wrong passage scores as a hit. expect_section is diagnostic only.",
    ]
    if args.negatives_from is None:
        limitations.append(
            "Negatives are human-authored placeholders, not drawn from a "
            "second department's corpus, because only one department "
            "(risk_compliance) is populated at generation time. This is a "
            "different bias from corpus-derived negatives, not a lesser one: "
            "a human must invent questions this department's corpus "
            "genuinely cannot answer, rather than borrowing real questions "
            "from elsewhere."
        )

    return {
        "parameters": {
            "generated_at": date.today().isoformat(),
            "department": args.department,
            "negatives_from": args.negatives_from,
            "document_count": int(doc_count),
            "chunk_count": int(chunk_count),
            "sha256": None,  # stamped by --freeze, AFTER human review
        },
        "limitations": limitations,
        "questions": questions,
    }


def freeze(path: Path) -> None:
    cohort = json.loads(path.read_text())
    unreviewed = [q["id"] for q in cohort["questions"] if "[REVIEW" in q["question"]]
    if unreviewed:
        raise SystemExit(
            f"{len(unreviewed)} question(s) still carry the REVIEW marker "
            f"({', '.join(unreviewed[:5])}…). A cohort is evidence only if a "
            "human wrote its questions."
        )
    cohort["parameters"]["sha256"] = questions_hash(cohort["questions"])
    path.write_text(json.dumps(cohort, indent=2, ensure_ascii=False) + "\n")
    print(f"frozen: {cohort['parameters']['sha256']}")
    print(f"{len(cohort['questions'])} questions")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--department")
    parser.add_argument("--negatives-from", default=None)
    parser.add_argument("--answerable", type=int, default=40)
    parser.add_argument("--negatives", type=int, default=10)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--freeze", type=Path)
    args = parser.parse_args()

    if args.freeze:
        freeze(args.freeze)
        return
    if not (args.department and args.out):
        raise SystemExit("--department and --out are required")

    cohort = asyncio.run(build(args))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(cohort, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {args.out} — {len(cohort['questions'])} CANDIDATES")
    print("Review every question, then re-run with --freeze to stamp the hash.")


if __name__ == "__main__":
    main()
