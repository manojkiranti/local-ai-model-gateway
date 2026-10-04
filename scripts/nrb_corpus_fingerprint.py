#!/usr/bin/env python
"""Fingerprint one department's corpus, and compare two fingerprints (spec §8.2a, d).

    DATABASE_URL=postgresql+asyncpg://gateway:***@127.0.0.1:5432/local_ai_gateway_build \\
        .venv/bin/python scripts/nrb_corpus_fingerprint.py --department nrb --out "$SCRATCH/before.json"
    .venv/bin/python scripts/nrb_corpus_fingerprint.py --compare before.json after.json --expect-changed ids.txt

OPERATIONAL (app/nrb/dbguard.py). Per document: status, chunk count, the routes
its chunks carry, and a sha256 over its chunks' content AND metadata in order —
metadata, so a detected-but-unrepaired document (text unchanged, now
non-authoritative) still registers as changed. Plus the corpus checks
build_release_db.py prints. Read-only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.nrb import dbguard  # noqa: E402

_DOCS = text("""
    SELECT d.id, d.status, count(c.id) AS chunks,
           encode(sha256(convert_to(coalesce(string_agg(c.content || E'\\x1f' || c.metadata::text,
                  E'\\x1e' ORDER BY c.chunk_index), ''), 'UTF8')), 'hex') AS digest,
           coalesce(array_agg(DISTINCT coalesce(c.metadata->>'route', '-'))
                    FILTER (WHERE c.id IS NOT NULL), '{}') AS routes
      FROM documents d
      JOIN departments dep ON dep.id = d.department_id
      LEFT JOIN document_chunks c ON c.document_id = d.id
     WHERE dep.code = :code
     GROUP BY d.id, d.status ORDER BY d.id
""")
_CHECKS = {
    "chunks_without_embedding": """SELECT count(*) FROM document_chunks c JOIN departments dep
        ON dep.id = c.department_id WHERE dep.code = :code AND c.embedding IS NULL""",
    "ready_without_chunks": """SELECT count(*) FROM documents d JOIN departments dep ON dep.id = d.department_id
        WHERE dep.code = :code AND d.status = 'ready'
        AND NOT EXISTS (SELECT 1 FROM document_chunks c WHERE c.document_id = d.id)""",
}
_REPAIR_SPLIT = text("""SELECT coalesce(c.metadata->>'text_repair', '-') AS repair, count(*) AS n
    FROM document_chunks c JOIN departments dep ON dep.id = c.department_id
    WHERE dep.code = :code GROUP BY 1 ORDER BY 1""")


def compare(before: dict, after: dict, expected: set[str]) -> dict:
    b, a = before["documents"], after["documents"]
    changed = sorted(k for k in a if k in b and a[k]["digest"] != b[k]["digest"])
    unexpected = sorted(set(changed) - expected)
    unchanged_expected = sorted(expected - set(changed))
    route_changes = sorted(k for k in changed if sorted(a[k]["routes"]) != sorted(b[k]["routes"]))
    status_changes = sorted(k for k in a if k in b and a[k]["status"] != b[k]["status"])
    ok = not unexpected and not unchanged_expected and not route_changes and not status_changes \
        and set(a) == set(b)
    return {"ok": ok, "changed": changed, "unexpected": unexpected,
            "unchanged_expected": unchanged_expected, "route_changes": route_changes,
            "status_changes": status_changes}


async def _snapshot(url: str, code: str) -> dict:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            docs = {r.id: {"status": r.status, "chunks": r.chunks, "digest": r.digest,
                           "routes": list(r.routes)}
                    for r in await conn.execute(_DOCS, {"code": code})}
            checks = {name: (await conn.execute(text(sql), {"code": code})).scalar()
                      for name, sql in _CHECKS.items()}
            split = {r.repair: r.n for r in await conn.execute(_REPAIR_SPLIT, {"code": code})}
    finally:
        await engine.dispose()
    return {"documents": docs, "checks": checks, "repair_split": split}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--department")
    ap.add_argument("--out")
    ap.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"))
    ap.add_argument("--expect-changed")
    args = ap.parse_args()
    if args.compare:
        before, after = (json.loads(Path(p).read_text()) for p in args.compare)
        expected = set(Path(args.expect_changed).read_text().split()) if args.expect_changed else set()
        out = compare(before, after, expected)
        out["checks_after"] = after["checks"]
        out["repair_split_after"] = after["repair_split"]
        print(json.dumps(out, indent=2))
        healthy = all(v == 0 for v in after["checks"].values())
        return 0 if out["ok"] and healthy else 1
    url = os.environ.get("DATABASE_URL", "")
    try:
        name = dbguard.require(url, dbguard.BUILD_DATABASES)
    except dbguard.RefusedDatabase as exc:
        print(f"refusing to run: {exc}", file=sys.stderr)
        return 2
    print(f"database: {name}")
    snap = asyncio.run(_snapshot(url, args.department))
    Path(args.out).write_text(json.dumps(snap, indent=2) + "\n")
    print(json.dumps({"documents": len(snap["documents"]), **snap["checks"],
                      "repair_split": snap["repair_split"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
