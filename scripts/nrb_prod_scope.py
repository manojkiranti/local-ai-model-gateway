#!/usr/bin/env python
"""Draw the PRODUCTION NRB corpus scope from the catalog, and freeze it.

    DATABASE_URL=postgresql+asyncpg://gateway:***@127.0.0.1:5432/local_ai_gateway_build \\
        .venv/bin/python scripts/nrb_prod_scope.py --out docs/nrb/prod-corpus-scope.json

    # then, the scope IS the pipeline's input:
    ... scripts/nrb_pipeline.py --department nrb --cohort docs/nrb/prod-corpus-scope.json

WHY A SCRIPT, NOT A QUERY SOMEBODY RAN ONCE
    The previous attempt at this corpus chose a scope ("89 sources / 90 files /
    ~89 MB") and never wrote it down, so a week later it could not be reproduced.
    This draws the scope from a RULE, freezes the result in the P7 cohort format
    (`scripts/nrb_p7_cohort.py`) that `nrb_pipeline.py --cohort` already reads,
    and records the rule verbatim in the file. The rule has to be re-drawable: at
    cutover production sends a FRESH dump (docs/prod-incident-2026-09-20.md §7),
    and its catalog may carry circulars published after this build.

THE RULE (approved 2026-09-24)
    Every active source of type directive / act / rule_bylaw / forex, whatever
    its date, plus every circular published on or after 2025-07-17 (Nepal time).
    That date is Shrawan 1 of FY 2082/83. It was offered as "this FY's
    circulars", but FY 2082/83 ENDED 2026-07-16, so the approved date covers the
    previous fiscal year AND the current one so far; the date is what was
    approved, and the date is what this uses.

    `admits()` below is the one place the rule lives. The SQL deliberately does
    NOT filter by it — it returns every active source that has a file and
    Python decides — so the tested rule and the drawn scope cannot disagree.

    An UNDATED circular is refused and counted, never guessed.

OPERATIONAL, SO IT ADMITS THE BUILD CLONE
    See app/nrb/dbguard.py. It reads the catalog and writes one file; it changes
    nothing in the database.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app import localtime  # noqa: E402
from app.nrb import dbguard  # noqa: E402

CORE_TYPES = frozenset({"directive", "act", "rule_bylaw", "forex"})
CIRCULAR_SINCE = date(2025, 7, 17)  # Shrawan 1, FY 2082/83 — see the docstring
ROLE = "corpus"

RULE = (
    "active nrb_sources with an attached file, where document_type IN "
    "('directive','act','rule_bylaw','forex') OR (document_type = 'circular' "
    "AND published_at, in Nepal time, is on or after 2025-07-17); one entry per "
    "distinct file (comparison_key)"
)


def admits(document_type: str | None, published: date | None) -> bool:
    """The whole rule. Pure, so it is provable without a database."""
    if document_type in CORE_TYPES:
        return True
    if document_type == "circular":
        return published is not None and published >= CIRCULAR_SINCE
    return False


def _nepal_date(value: datetime | str | None) -> date | None:
    """A timestamptz as a NEPAL calendar date. A circular published at 00:30 on
    Shrawan 1 is 18:45 the day before in UTC; taking `.date()` of the UTC value
    would put it on the wrong side of the cutoff."""
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:  # a bare date string from a test row
        return value.date()
    return value.astimezone(localtime.NPT).date()


def entries(rows: list[dict]) -> list[dict]:
    """One entry per distinct file, ordered by key, so the file is reproducible.
    NRB attaches one file to several posts; the pipeline scopes on files."""
    by_key: dict[str, dict] = {}
    for r in rows:
        key = r["comparison_key"]
        if key in by_key:
            continue
        by_key[key] = {
            "role": ROLE,
            "comparison_key": key,
            "document_type": r.get("document_type"),
            "published_at": str(r.get("published_at")) if r.get("published_at") else None,
            "owner": r.get("owner"),
            "title": r.get("title"),
            "filename": r.get("filename"),
            "extension": r.get("extension"),
        }
    return [by_key[k] for k in sorted(by_key)]


def fingerprint(items: list[dict]) -> str:
    """Identical to nrb_p7_cohort.cohort_sha256: an ORDERED list of [role, key]
    pairs. Kept as its own function rather than imported, because that script
    is an evidence script pinned to p4 and this one is not — a test asserts the
    two agree instead."""
    canonical = json.dumps(
        [[e["role"], e["comparison_key"]] for e in items],
        ensure_ascii=False, separators=(",", ":"), sort_keys=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


_POOL_SQL = text("""
    SELECT nf.comparison_key, s.document_type, s.published_at, s.owner,
           s.title, nf.filename, nf.extension
      FROM nrb_sources s
      JOIN nrb_source_files sf ON sf.source_id = s.id
      JOIN nrb_files nf        ON nf.id = sf.file_id
     WHERE s.is_active
     ORDER BY nf.comparison_key, s.id
""")


async def _draw(url: str) -> tuple[list[dict], dict]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            pool = [dict(r._mapping) for r in await conn.execute(_POOL_SQL)]
    finally:
        await engine.dispose()

    kept: list[dict] = []
    undated_circulars = 0
    for r in pool:
        published = _nepal_date(r["published_at"])
        if r["document_type"] == "circular" and published is None:
            undated_circulars += 1
        if admits(r["document_type"], published):
            kept.append(r)
    items = entries(kept)
    counts = {
        "pool_source_file_pairs": len(pool),
        "files": len(items),
        "by_document_type": dict(sorted(Counter(e["document_type"] for e in items).items())),
        "undated_circulars_refused": undated_circulars,
    }
    return items, counts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True, help="where to write the frozen scope")
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL", "")
    try:
        name = dbguard.require(url, dbguard.BUILD_DATABASES)
    except dbguard.RefusedDatabase as exc:
        print(f"refusing to run: {exc}", file=sys.stderr)
        return 2
    print(f"database: {name}")

    items, counts = asyncio.run(_draw(url))
    payload = {
        "schema_version": 1,
        "purpose": "the production NRB corpus (docs/prod-incident-2026-09-20.md §7)",
        "drawn_at": localtime.now().isoformat(timespec="seconds"),
        "drawn_from": name,
        "draw_rule": RULE,
        "circular_since": CIRCULAR_SINCE.isoformat(),
        "counts": counts,
        "cohort_sha256": fingerprint(items),
        "entries": items,
    }
    Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(counts, ensure_ascii=False, indent=2))
    print(f"cohort_sha256 {payload['cohort_sha256']}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
