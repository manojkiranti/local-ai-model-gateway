#!/usr/bin/env python
"""Compare native-1 and native-2 over the frozen Phase 6A benchmark. READ-ONLY.

    DATABASE_URL=postgresql+asyncpg://…/local_ai_gateway_p4 \
        .venv/bin/python scripts/nrb_native2_compare.py \
            --manifest docs/nrb/phase6a-manifest.json \
            --out docs/nrb/phase6b-native2-comparison.txt

Reads `nrb_extractions` at both versions and writes ONE text file. It parses
nothing, downloads nothing, converts nothing and writes no database row — the two
sets of rows already exist, produced by `nrb_extract.py` at each version, and
this only puts them side by side.

WHY A SEPARATE COMMAND
    §11.9 and the Phase 6A plan both require a rule change to be re-run over the
    SAME frozen manifest with both versions reported together. A transition matrix
    computed inside the extraction pass would only ever see one version.

WHAT IT WILL NOT DO
    Claim a transition is correct. A `suspicious -> clean` move is a change, not a
    vindication; the report counts them and names the ones that were reviewed by
    eye, and the manual work is a separate committed file.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.nrb import legacy_report  # noqa: E402
from app.nrb.extraction import EXTRACTOR_VERSION  # noqa: E402
from app.nrb.routing import EXTRACTOR_VERSION_V2  # noqa: E402

# Frozen evidence from Phase 6A / 6B Task 1. Named, not re-derived: they are
# already committed measurements, and re-deriving them here would let this report
# disagree with the files it is meant to be compared against.
ENGLISH_TABLE_CONTROLS = {
    "1ad95ea9f45c": "docling-rescue table",
    "38c6d43c3ed1": "docling-rescue table",
    "46e0c17f27a1": "docling-rescue table",
    "7e2257c289d2": "docling-rescue table",
    "c807fe4e767e": "docling-rescue table",
    "e4961a48d3d2": "docling-rescue table",
    "05fa82badf94": "hand-reviewed false positive (6A STEP 5)",
}
KNOWN_PREETI_SPREADSHEET = "8df7b02f8a13"
KNOWN_MIXED_SCRIPT = "84862ab6866a"
KNOWN_UNKNOWN_ENCODING = "9892625b8531"


async def load(session, version: str, keys: set[str]) -> dict[str, dict]:
    from sqlalchemy import select

    from app.nrb.models import NRBExtraction, NRBFile

    stmt = (
        select(
            NRBExtraction.content_sha256, NRBExtraction.status,
            NRBExtraction.reason, NRBExtraction.warnings,
            NRBExtraction.media_family, NRBExtraction.legacy_line_ratio,
            NRBExtraction.devanagari_ratio, NRBExtraction.metrics,
            NRBFile.comparison_key,
        )
        .join(NRBFile, NRBFile.content_sha256 == NRBExtraction.content_sha256)
        .where(NRBExtraction.extractor_version == version)
        .order_by(NRBExtraction.content_sha256)
    )
    out: dict[str, dict] = {}
    for row in (await session.execute(stmt)).all():
        if row.comparison_key not in keys:
            continue
        out.setdefault(row.content_sha256, {
            "sha": row.content_sha256, "status": row.status, "reason": row.reason,
            "warnings": list(row.warnings or []), "family": row.media_family,
            "legacy": row.legacy_line_ratio or 0.0,
            "dev": row.devanagari_ratio or 0.0,
            "metrics": dict(row.metrics or {}),
        })
    return out


def _band(ratio: float) -> str:
    if ratio >= 0.80:
        return ">=0.80"
    if ratio >= 0.50:
        return "0.50-0.80"
    if ratio > 0.20:
        return "0.20-0.50"
    return "<=0.20"


async def run(args) -> int:
    from app.db.session import SessionLocal

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    parent = manifest["selection_sha256"]
    keys = {e["comparison_key"] for e in manifest["entries"]}

    async with SessionLocal() as session:
        v1 = await load(session, args.v1, keys)
        v2 = await load(session, args.v2, keys)

    shared = sorted(set(v1) & set(v2))
    ev: dict = {
        "identity": {
            "parent_fingerprint": parent,
            "manifest_entries": len(keys),
            "v1": args.v1, "v2": args.v2,
            "v1_rows": len(v1), "v2_rows": len(v2),
            "compared": len(shared),
            "unavailable": len(keys) - len(v1),
        },
        "status_transitions": collections.Counter(
            (v1[s]["status"], v2[s]["status"]) for s in shared
        ),
        "reason_transitions": collections.Counter(
            (v1[s]["reason"], v2[s]["reason"]) for s in shared
        ),
    }

    legacy1 = {s for s in shared if v1[s]["reason"] == "legacy_font_suspected"}
    legacy2 = {s for s in shared if v2[s]["reason"] == "legacy_font_suspected"}
    ev["suspicious_to_clean"] = sorted(legacy1 - legacy2)
    ev["clean_to_suspicious"] = sorted(legacy2 - legacy1)

    ev["english_controls"] = [
        {
            "sha": sha, "note": note,
            "v1": f"{v1[full]['status']}/{v1[full]['reason']}",
            "v2": f"{v2[full]['status']}/{v2[full]['reason']}",
            "legacy_before": v1[full]["legacy"],
            "unit_ratio": v2[full]["metrics"].get("unit_legacy_ratio"),
            "corrected": v1[full]["reason"] == "legacy_font_suspected"
                         and v2[full]["reason"] != "legacy_font_suspected",
        }
        for sha, note in ENGLISH_TABLE_CONTROLS.items()
        for full in [next((s for s in shared if s.startswith(sha)), None)]
        if full
    ]

    sheets = [s for s in shared if v1[s]["family"] == "spreadsheet"]
    ev["spreadsheets"] = {
        "total": len(sheets),
        "with_text_cells": sum(
            1 for s in sheets
            if (v2[s]["metrics"].get("spreadsheet_text_cells") or 0) > 0
        ),
        "v1_legacy": sum(1 for s in sheets if v1[s]["reason"] == "legacy_font_suspected"),
        "v2_legacy": sum(1 for s in sheets if v2[s]["reason"] == "legacy_font_suspected"),
        "newly_flagged": sorted(
            s for s in sheets
            if v2[s]["reason"] == "legacy_font_suspected"
            and v1[s]["reason"] != "legacy_font_suspected"
        ),
    }

    ev["minority"] = sorted(
        s for s in shared
        if "minority_legacy_region" in v2[s]["warnings"]
    )

    ev["bands"] = {
        "v1": collections.Counter(_band(v1[s]["legacy"]) for s in legacy1),
        "v2_by_legacy_line_ratio": collections.Counter(
            _band(v2[s]["legacy"]) for s in legacy2
        ),
        "v2_by_unit_ratio": collections.Counter(
            _band(float(v2[s]["metrics"].get("unit_legacy_ratio") or 0.0))
            for s in legacy2
        ),
    }

    ev["named_cases"] = {}
    for label, prefix in (("known preeti spreadsheet", KNOWN_PREETI_SPREADSHEET),
                          ("known mixed unicode+preeti", KNOWN_MIXED_SCRIPT),
                          ("known unknown encoding", KNOWN_UNKNOWN_ENCODING)):
        full = next((s for s in shared if s.startswith(prefix)), None)
        if full:
            ev["named_cases"][label] = {
                "sha": prefix,
                "v1": f"{v1[full]['status']}/{v1[full]['reason']}",
                "v2": f"{v2[full]['status']}/{v2[full]['reason']}",
                "warnings": v2[full]["warnings"],
                "legacy_line_ratio": v1[full]["legacy"],
                "unit_ratio": v2[full]["metrics"].get("unit_legacy_ratio"),
                "legacy_units": v2[full]["metrics"].get("unit_legacy_candidates"),
                "max_run": v2[full]["metrics"].get("unit_max_legacy_run"),
                "contested": v2[full]["metrics"].get("unit_contested_legacy_ratio"),
            }

    ev["changed_detail"] = [
        {
            "sha": s, "family": v1[s]["family"],
            "v1": f"{v1[s]['status']}/{v1[s]['reason']}",
            "v2": f"{v2[s]['status']}/{v2[s]['reason']}",
            "legacy_line_ratio": v1[s]["legacy"],
            "unit_ratio": v2[s]["metrics"].get("unit_legacy_ratio"),
            "legacy_units": v2[s]["metrics"].get("unit_legacy_candidates"),
            "max_run": v2[s]["metrics"].get("unit_max_legacy_run"),
            "contested": v2[s]["metrics"].get("unit_contested_legacy_ratio"),
            "english_units": v2[s]["metrics"].get("unit_english"),
            "warnings": v2[s]["warnings"],
        }
        for s in shared
        if (v1[s]["status"], v1[s]["reason"]) != (v2[s]["status"], v2[s]["reason"])
    ]

    Path(args.out).write_text(legacy_report.render_native2(ev), encoding="utf-8")
    print(f"compared {len(shared)} blobs")
    print(f"  suspicious -> clean : {len(ev['suspicious_to_clean'])}")
    print(f"  clean -> suspicious : {len(ev['clean_to_suspicious'])}")
    print(f"  minority regions    : {len(ev['minority'])}")
    print(f"wrote {args.out}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest", default="docs/nrb/phase6a-manifest.json")
    p.add_argument("--out", default="docs/nrb/phase6b-native2-comparison.txt")
    p.add_argument("--v1", default=EXTRACTOR_VERSION)
    p.add_argument("--v2", default=EXTRACTOR_VERSION_V2)
    return asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
