#!/usr/bin/env python
"""Which NRB documents does native-3's detector flag, and what would the repair do?

    DATABASE_URL=postgresql+asyncpg://gateway:***@127.0.0.1:5432/local_ai_gateway_build \\
        .venv/bin/python scripts/nrb_native3_detect.py --department nrb \\
        --out "$SCRATCH/detect.json" [--repair-report] [--ids-out "$SCRATCH/ids.txt"]

Three uses (docs/superpowers/plans/2026-10-02-native3-font-repair.md):
  * CHECKPOINT A — `--repair-report` over the production scope (development
    evidence): run agreement, pages repaired, every failure classified;
  * Task 14 — `--ids-out` lists the documents to re-ingest;
  * `--only` narrows to blob prefixes while debugging one document.

OPERATIONAL (app/nrb/dbguard.py): reads documents and blobs, writes files,
changes nothing in the database. It runs the classifier and the repair exactly
as recovery would (`recovery.page_routes`, `fontrepair.document_evidence`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.files import documents as file_documents  # noqa: E402
from app.nrb import dbguard, extraction, filestore, fontrepair, recovery  # noqa: E402

_DOCS_SQL = text("""
    SELECT d.id, d.title, d.content_hash, d.file_type
      FROM documents d JOIN departments dep ON dep.id = d.department_id
     WHERE dep.code = :code AND d.status = 'ready' AND d.metadata->>'origin' = 'nrb'
     ORDER BY d.id
""")


def _producer(path: Path) -> str:
    try:
        from pypdf import PdfReader

        meta = PdfReader(str(path)).metadata or {}
        return str(meta.get("/Producer") or meta.get("/Creator") or "")
    except Exception:  # noqa: BLE001
        return ""


def examine(path: Path, engine: fontrepair.FontRepairEngine, *, repair: bool) -> dict:
    result = extraction.extract_file(path, family="pdf", extension="pdf", extractor_version="native-2")
    plan = recovery.plan_document(family=result.family, status=result.status,
                                  reason=result.reason, metrics=result.metrics)
    pages = list(file_documents.read_pdf_pages(path).pages)
    routes = recovery.page_routes(path, plan, pages)
    native = [n for n, (route, _) in enumerate(routes, start=1) if route == recovery.ROUTE_NATIVE]
    record = {"plan": plan.plan, "page_count": len(pages)}  # "pages" is evidence's per-page list
    if repair:
        record |= fontrepair.document_evidence(engine, path, pages, native)
    else:
        record["native_pages"] = len(native)
        record["detection"] = fontrepair.detect([pages[n - 1] for n in native]).as_dict()
    return record


async def _documents(url: str, code: str) -> list[dict]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return [dict(r._mapping) for r in await conn.execute(_DOCS_SQL, {"code": code})]
    finally:
        await engine.dispose()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--department", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--repair-report", action="store_true")
    ap.add_argument("--ids-out", default=None)
    ap.add_argument("--only", nargs="*", default=None, help="blob sha prefixes")
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL", "")
    try:
        name = dbguard.require(url, dbguard.BUILD_DATABASES)
    except dbguard.RefusedDatabase as exc:
        print(f"refusing to run: {exc}", file=sys.stderr)
        return 2
    print(f"database: {name}")

    engine = fontrepair.FontRepairEngine()
    print(f"engine: {engine.version}")
    rows = asyncio.run(_documents(url, args.department))
    records = []
    for row in rows:
        sha = row["content_hash"]
        if (row["file_type"] or "").lower() != "pdf":
            continue
        if args.only and not any(sha.startswith(p) for p in args.only):
            continue
        path = filestore.resolve_path(filestore.storage_key_for(sha, row["file_type"]))
        if not path.exists():
            records.append({"document_id": row["id"], "sha": sha[:12], "missing": True})
            continue
        record = examine(path, engine, repair=args.repair_report)
        record |= {"document_id": row["id"], "sha": sha[:12], "sha_full": sha, "title": row["title"],
                   "producer": _producer(path)}
        records.append(record)
        if record.get("detection", {}).get("suspect"):
            print(f"  detected {sha[:12]} {record['detection']['density'] * 1000:.2f}/1k "
                  f"{record.get('statuses', '')} {row['title'][:50]}", flush=True)

    detected = [r for r in records if r.get("detection", {}).get("suspect")]
    summary = {"documents": len(records), "detected": len(detected)}
    if args.repair_report:
        statuses = Counter()
        for r in detected:
            statuses.update(r.get("statuses", {}))
        runs = sum(r.get("runs", 0) for r in detected)
        matched = sum(r.get("runs_matched", 0) for r in detected)
        summary |= {"page_statuses": dict(statuses), "runs": runs, "runs_matched": matched,
                    "run_agreement": round(matched / runs, 4) if runs else None}
        by_producer: dict[str, Counter] = {}
        for r in detected:
            by_producer.setdefault(r["producer"][:40], Counter()).update(r.get("statuses", {}))
        summary["by_producer"] = {k: dict(v) for k, v in by_producer.items()}
    Path(args.out).write_text(json.dumps({"database": name, "engine": engine.version,
                                          "summary": summary, "documents": records},
                                         ensure_ascii=False, indent=2) + "\n")
    if args.ids_out:
        Path(args.ids_out).write_text("".join(f"{r['document_id']}\n" for r in detected))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
