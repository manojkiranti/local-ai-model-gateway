#!/usr/bin/env python
"""The native-3 cohort measurements (spec §6.3). EVIDENCE: local_ai_gateway_p4 only.

    DATABASE_URL=postgresql+asyncpg://gateway:***@127.0.0.1:5432/local_ai_gateway_p4 \\
        .venv/bin/python scripts/nrb_native3_evidence.py --manifest docs/nrb/native3-cohort.json \\
        [--manifest docs/nrb/native3-cohort-topup.json] \\
        --out-json docs/nrb/native3-cohort-evidence.json --out-txt docs/nrb/native3-cohort-evidence.txt

Measures, over every fetched PDF in the frozen cohort:
  * non-regression — every UNDETECTED document's native pages come back
    byte-identical with the repair engine on (required: 100%);
  * classifier invariance — each native-3 extraction row equals its native-2
    row on status, reason and every native-2 metric (required: 100%);
  * the detector against font contradiction, as two independent signals;
  * the gate: page statuses and run agreement by font identity x producer.
Exits 1 when either required property fails, when any parsed PDF lacks a
native-2 or native-3 extraction row (a missing row cannot be compared), or when
a PDF could not be measured at all (`measure_errors`: unverified, never a pass).
A PDF native-2 itself could not parse (`extraction_failed`) has no native pages
to regress and is recorded, not counted. Recovery runs with no converter
and no OCR: only native pages are compared, and both arms fail the other routes
identically.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.nrb import dbguard, extraction, filestore, fontrepair, manifest, recovery, sniff  # noqa: E402

_ROWS = text("""
    SELECT f.comparison_key, f.fetch_status, f.content_sha256, f.extension,
           e2.status AS s2, e2.reason AS r2, e2.metrics AS m2,
           e3.status AS s3, e3.reason AS r3, e3.metrics AS m3
      FROM nrb_files f
      LEFT JOIN nrb_extractions e2 ON e2.content_sha256 = f.content_sha256 AND e2.extractor_version = 'native-2'
      LEFT JOIN nrb_extractions e3 ON e3.content_sha256 = f.content_sha256 AND e3.extractor_version = 'native-3'
     WHERE f.comparison_key = ANY(:keys)
""")


def classification_equal(v2: dict, v3: dict) -> bool:
    if (v2["status"], v2["reason"]) != (v3["status"], v3["reason"]):
        return False
    m2, m3 = v2["metrics"] or {}, v3["metrics"] or {}
    return all(m3.get(k) == v for k, v in m2.items() if k != "duration_ms")


def invariance_verdict(row: dict) -> str:
    """"missing" when either extraction row is absent (it cannot be compared and
    must FAIL the run, not be skipped), else "checked" or "failed"."""
    if row["s2"] is None or row["s3"] is None:
        return "missing"
    same = classification_equal(
        {"status": row["s2"], "reason": row["r2"], "metrics": row["m2"]},
        {"status": row["s3"], "reason": row["r3"], "metrics": row["m3"]})
    return "checked" if same else "failed"


def row_route(row: dict) -> str:
    """"extraction_failed" when native-2 could not parse the PDF (nothing to
    measure, never measured), else "measure"."""
    return "extraction_failed" if row["s2"] == "failed" else "measure"


def crosstab(records: list[dict]) -> dict:
    cell = Counter((r["detected"], r["contradicts"]) for r in records)
    return {
        "both": cell[(True, True)], "neither": cell[(False, False)],
        "detected_only": cell[(True, False)], "contradiction_only": cell[(False, True)],
        "false_negative_candidates": sorted(r["sha"] for r in records if r["contradicts"] and not r["detected"]),
        "false_positive_candidates": sorted(r["sha"] for r in records if r["detected"] and not r["contradicts"]),
    }


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (round(centre - half, 4), round(centre + half, 4))


def _producer(path: Path) -> str:
    try:
        from pypdf import PdfReader

        meta = PdfReader(str(path)).metadata or {}
        return str(meta.get("/Producer") or meta.get("/Creator") or "")
    except Exception:  # noqa: BLE001
        return ""


def _measure(path: Path, engine: fontrepair.FontRepairEngine) -> dict:
    result = extraction.extract_file(path, family="pdf", extension="pdf", extractor_version="native-2")
    off = recovery.recover(path, result)
    on = recovery.recover(path, result, repair=engine)
    native_off = [p for p in off.pages if p.route == recovery.ROUTE_NATIVE]
    native_on = [p for p in on.pages if p.route == recovery.ROUTE_NATIVE]
    detected = any(w.startswith("tounicode_suspected") for w in on.warnings)
    statuses = Counter(p.detail.get("repair", "passthrough") for p in native_on)
    return {
        "detected": detected,
        "identical": [p.text for p in native_off] == [p.text for p in native_on],
        "statuses": dict(statuses),
        "runs": sum(p.detail.get("runs", 0) for p in native_on),
        "runs_matched": sum(p.detail.get("runs_matched", 0) for p in native_on),
        "pages": [{"page": p.page_number, "status": p.detail.get("repair"),
                   "font_identities": p.detail.get("font_identities", []),
                   "illegal_before": p.detail.get("illegal_before"),
                   "illegal_after": p.detail.get("illegal_after")}
                  for p in native_on if p.detail.get("repair")],
    }


async def _rows(url: str, keys: list[str]) -> list[dict]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return [dict(r._mapping) for r in await conn.execute(_ROWS, {"keys": keys})]
    finally:
        await engine.dispose()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--manifest", action="append", required=True)
    ap.add_argument("--out-json", required=True)
    ap.add_argument("--out-txt", required=True)
    args = ap.parse_args()
    url = os.environ.get("DATABASE_URL", "")
    try:
        name = dbguard.require(url, dbguard.EVIDENCE_DATABASES)
    except dbguard.RefusedDatabase as exc:
        print(f"refusing to run: {exc}", file=sys.stderr)
        return 2
    print(f"database: {name}")

    stratum: dict[str, str] = {}
    for path in args.manifest:
        for entry in manifest.read_manifest(path).entries:
            stratum[entry["comparison_key"]] = entry["sampling_stratum"]
    rows = asyncio.run(_rows(url, list(stratum)))
    engine = fontrepair.FontRepairEngine()
    records, pages_out, invariance_failures = [], [], []
    invariance_missing_native3: list[str] = []
    extraction_failed: list[str] = []
    measure_errors: list[str] = []
    fonts_report_errors: list[str] = []
    invariance_checked = 0
    for row in rows:
        sha = row["content_sha256"]
        base = {"key": row["comparison_key"], "stratum": stratum[row["comparison_key"]],
                "fetch_status": row["fetch_status"]}
        if row["fetch_status"] != "fetched" or not sha:
            records.append({**base, "parsed": False})
            continue
        path = filestore.resolve_path(filestore.storage_key_for(sha, row["extension"]))
        try:
            with path.open("rb") as handle:
                family = sniff.family_for(sniff.sniff(handle.read(4096))[0])
        except OSError:
            # A catalogued blob absent from disk is a recorded gap, not a crash.
            records.append({**base, "sha": sha[:12], "parsed": False, "missing_blob": True})
            continue
        if family != "pdf":
            records.append({**base, "sha": sha[:12], "parsed": False, "family": family})
            continue
        if row_route(row) == "extraction_failed":
            extraction_failed.append(sha[:12])
            records.append({**base, "sha": sha[:12], "parsed": False, "extraction_failed": True})
            continue
        verdict = invariance_verdict(row)
        if verdict == "missing":
            invariance_missing_native3.append(sha[:12])
        else:
            invariance_checked += 1
            if verdict == "failed":
                invariance_failures.append(sha[:12])
        try:
            measured = _measure(path, engine)
            report = fontrepair.fonts_report(path)
            producer = _producer(path)
        except Exception as exc:  # noqa: BLE001 - an unmeasurable document fails the run
            measure_errors.append(sha[:12])
            records.append({**base, "sha": sha[:12], "parsed": False, "measure_error": type(exc).__name__})
            continue
        if "error" in report:
            fonts_report_errors.append(sha[:12])
        # `contradicts` is present on both the success and the error shape.
        records.append({**base, "sha": sha[:12], "sha_full": sha, "parsed": True, "producer": producer,
                        "contradicts": report["contradicts"], "fonts": report.get("fonts"), **measured})
        for page in measured["pages"]:
            pages_out.append({"sha": sha, "producer": producer, "stratum": base["stratum"], **page})

    parsed = [r for r in records if r.get("parsed")]
    regressions = [r["sha"] for r in parsed if not r["detected"] and not r["identical"]]
    random_parsed = [r for r in parsed if r["stratum"] == "random"]
    random_detected = sum(1 for r in random_parsed if r["detected"])
    enriched_detected = sum(1 for r in parsed if r["stratum"] == "enriched" and r["detected"])
    gate = defaultdict(Counter)
    for page in pages_out:
        key = f"{'+'.join(page['font_identities']) or '-'} | {page['producer'][:30]}"
        gate[key][page["status"]] += 1
    runs = sum(r["runs"] for r in parsed)
    matched = sum(r["runs_matched"] for r in parsed)
    summary = {
        "database": name, "engine": engine.version,
        "entries": len(stratum), "fetched_pdfs_parsed": len(parsed),
        "detected": sum(1 for r in parsed if r["detected"]),
        "enriched_detected": enriched_detected,
        "top_up_required": enriched_detected < 20,
        "prevalence_random": {"detected": random_detected, "parsed": len(random_parsed),
                              "wilson95": wilson(random_detected, len(random_parsed))},
        "non_regression": {"undetected": sum(1 for r in parsed if not r["detected"]),
                           "regressions": regressions},
        "classifier_invariance": {"checked": invariance_checked,
                                  "missing_native3": invariance_missing_native3,
                                  "failures": invariance_failures},
        "extraction_failed": extraction_failed,
        "measure_errors": measure_errors,
        "fonts_report_errors": fonts_report_errors,
        "crosstab": crosstab(parsed),
        "run_agreement": {"runs": runs, "matched": matched,
                          "rate": round(matched / runs, 4) if runs else None},
        "gate_by_font_and_producer": {k: dict(v) for k, v in sorted(gate.items())},
    }
    Path(args.out_json).write_text(json.dumps({"summary": summary, "documents": records,
                                               "pages": pages_out}, ensure_ascii=False, indent=2) + "\n")
    lines = [f"native-3 cohort evidence — {name}, engine {engine.version}", ""]
    lines += [f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in summary.items()
              if k not in ("database", "engine")]
    Path(args.out_txt).write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    ok = not regressions and not invariance_failures and not invariance_missing_native3 and not measure_errors
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
