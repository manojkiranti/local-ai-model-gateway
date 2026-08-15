#!/usr/bin/env python
"""Phase 6B Task 1 — evaluate deterministic legacy-Nepali font conversion.

    DATABASE_URL=postgresql+asyncpg://…/local_ai_gateway_p4 \
        .venv/bin/python scripts/nrb_legacy_eval.py \
            --manifest docs/nrb/phase6a-manifest.json \
            --lexicon  docs/nrb/phase6b-lexicon.json \
            --out-json docs/nrb/phase6b-legacy-conversion-evaluation.json \
            --out-text docs/nrb/phase6b-legacy-conversion-evaluation.txt

EVIDENCE ONLY. This command:
  * reads `nrb_extractions` and `nrb_files` and writes NEITHER;
  * re-parses blobs already on disk under NRB_FILES_DIR;
  * makes ZERO network requests, runs no OCR, chunks nothing, embeds nothing;
  * wires no production routing — `quality.classify` and the `native-1` rows are
    exactly as Phase 6A committed them;
  * writes only the two artifacts named by `--out-json` / `--out-text`.

THE COHORT IS DRAWN BEFORE ANY CONVERTER RUNS
    Selection ranks blobs by sha256(parent benchmark fingerprint + algorithm +
    severity band + content hash), so it cannot depend on how conversion turned
    out, and a shuffled input produces the same cohort. Nothing outside the
    frozen 400 is eligible.

EVERY MAPPING STARTS FROM THE SAME ORIGINAL
    The five npttf2utf mappings are run independently and never chained: one
    mapping's corruption must not become another's input. No "best mapping wins"
    rule is applied — all scores are recorded and the report states them.

Exit codes: 0 the evaluation completed, 1 it ran but something failed, 2 it could
not start (missing manifest, missing lexicon, converter unavailable, no cohort).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.nrb import (  # noqa: E402
    legacy_convert as LC,
    legacy_eval,
    legacy_font,
    legacy_report,
    lexicon as LX,
    quality,
)
from app.nrb.extraction import EXTRACTOR_VERSION  # noqa: E402

# The six PDFs the Phase 6A Docling calibration found were English tables pypdf
# over-flagged (docs/nrb/phase6a-calibration.txt), plus the hand-reviewed false
# positive from the by-eye validation. Named rather than re-derived: they are
# already frozen evidence, and they are the best negative controls available
# because they sit exactly on the 0.20 boundary the guard has to survive.
ENGLISH_TABLE_CONTROLS = (
    "1ad95ea9f45c", "38c6d43c3ed1", "46e0c17f27a1",
    "7e2257c289d2", "c807fe4e767e", "e4961a48d3d2",
    "05fa82badf94",
)

PER_BAND = 10          # 3 severity bands -> up to 30 PDFs
SPREADSHEETS = 6       # openpyxl is slow (one benchmark workbook took 262s alone)
NO_TEXT_CONTROLS = 3

# The benchmark's Preeti-encoded workbook — Phase 6A's false negative, classified
# `extracted`/`clean` because spreadsheets are judged structurally. Always in the
# spreadsheet cohort: it is the one case we KNOW the answer to.
KNOWN_PREETI_SPREADSHEET = "8df7b02f8a13"

PREVIEW = 110


def _preview(text: str) -> str:
    return " ".join(text.split())[:PREVIEW]


def _match(refs, prefix):
    for ref in refs:
        if ref.content_sha256.startswith(prefix):
            return ref
    return None


async def run(args) -> int:
    from app.db.session import SessionLocal

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    parent = manifest["selection_sha256"]
    benchmark_keys = {e["comparison_key"] for e in manifest["entries"]}
    print(f"benchmark {parent[:12]}…  {len(benchmark_keys)} keys")

    lexicon = LX.load_lexicon(args.lexicon)
    print(f"lexicon   {lexicon.fingerprint[:12]}…  "
          f"{len(lexicon.english):,} english / {len(lexicon.nepali):,} nepali")

    converters = legacy_font.converters()
    if not converters:
        print("ERROR: no converter mappings available", file=sys.stderr)
        return 2
    print(f"converter npttf2utf {converters[0].version}  "
          f"mappings: {[c.mapping for c in converters]}\n")

    async with SessionLocal() as session:
        every = await legacy_eval.load_blob_refs(
            session, extractor_version=args.extractor_version
        )

    # Restrict to the frozen benchmark. A blob is in the cohort only if one of the
    # file rows pointing at it is named by the manifest.
    in_benchmark = [
        r for r in every if benchmark_keys & set(r.comparison_keys)
    ]
    print(f"blobs at {args.extractor_version}: {len(every)} total, "
          f"{len(in_benchmark)} inside the benchmark")

    control_shas = set(ENGLISH_TABLE_CONTROLS)
    candidates = [
        r for r in in_benchmark
        if r.reason == "legacy_font_suspected"
        and r.family == "pdf"
        and not any(r.content_sha256.startswith(p) for p in control_shas)
    ]
    cohort = legacy_eval.select_cohort(
        candidates, parent_fingerprint=parent, per_band=PER_BAND
    )

    # --- negative controls, each a named population ------------------------- #
    controls: list[tuple[str, legacy_eval.BlobRef]] = []
    for prefix in ENGLISH_TABLE_CONTROLS:
        ref = _match(in_benchmark, prefix)
        if ref is not None:
            controls.append(("english_table", ref))
    # A genuine-Unicode control must be genuinely, wholly Unicode. The looser
    # `devanagari_ratio >= 0.30` filter swept in `84862ab6866a` (0.6396), which
    # turns out to be a MIXED document — real Devanagari plus real Preeti, filed
    # `extracted`/`clean` by native-1 — so the converter correctly changed 29 of
    # its lines and the control scored that as a failure. It is reported as its
    # own category instead, because a mixed Unicode/Preeti document classified
    # clean is a finding, not a control.
    for ref in sorted(
        (r for r in in_benchmark if r.devanagari_ratio >= 0.30),
        key=lambda r: r.content_sha256,
    ):
        pure = ref.devanagari_ratio >= 0.90 and ref.legacy_line_ratio <= 0.05
        controls.append(("unicode_nepali" if pure else "mixed_script", ref))
    for ref in sorted(
        (r for r in in_benchmark
         if r.status == "needs_ocr" and r.reason in ("no_text_layer", "sparse_text_layer")),
        key=lambda r: r.content_sha256,
    )[:NO_TEXT_CONTROLS]:
        controls.append(("no_text_layer", ref))

    # --- the spreadsheet cohort, on its own denominator --------------------- #
    sheets = sorted(
        (r for r in in_benchmark if r.family == "spreadsheet" and r.status == "extracted"),
        key=lambda r: r.content_sha256,
    )
    known = _match(sheets, KNOWN_PREETI_SPREADSHEET)
    sheet_cohort = ([known] if known else []) + [
        r for r in sheets[:SPREADSHEETS] if r is not known
    ][: SPREADSHEETS - (1 if known else 0)]

    print(f"cohort: {len(cohort)} PDFs, {len(sheet_cohort)} spreadsheets, "
          f"{len(controls)} controls\n")
    if not cohort:
        print("ERROR: empty cohort", file=sys.stderr)
        return 2

    by_sha = {r.content_sha256: r for r in in_benchmark}
    evidence: dict = {
        "identity": {
            "parent_fingerprint": parent,
            "algorithm": legacy_eval.COHORT_ALGORITHM,
            "cohort_fingerprint": legacy_eval.cohort_fingerprint(
                cohort, parent_fingerprint=parent
            ),
            "lexicon_fingerprint": lexicon.fingerprint,
            "converter_name": converters[0].name,
            "converter_version": converters[0].version,
            "converter_licence": "GPL-3.0 — distribution gate, see requirements-nrb.txt",
            "extractor_version": args.extractor_version,
        },
        "cohort": [e.as_json() for e in cohort],
        "documents": [],
        "controls": [],
        "spreadsheets": [],
        "samples": [],
    }

    per_mapping: dict[str, dict] = {
        c.mapping: {"mapping": c.mapping, "documents": 0, "attempted_lines": 0,
                    "accepted_lines": 0, "ambiguous_lines": 0, "rejected_lines": 0,
                    "scores": []}
        for c in converters
    }
    total_lines = 0
    convert_seconds = 0.0
    failures = 0

    # ----------------------------------------------------------------- PDFs --
    for i, entry in enumerate(cohort, 1):
        ref = by_sha[entry.content_sha256]
        result = legacy_eval.read_blob_text(ref)
        if result.error:
            print(f"  [{i}/{len(cohort)}] {ref.short_sha} SKIP: {result.error}")
            failures += 1
            continue
        before = quality.measure_text(result.text)
        doc: dict = {
            "content_sha256": ref.content_sha256,
            "comparison_keys": list(ref.comparison_keys),
            "band": entry.band,
            "status_before": f"{ref.status}/{ref.reason}",
            "devanagari_before": before.devanagari_ratio,
            "legacy_before": before.legacy_line_ratio,
            "char_count_before": before.char_count,
            "mappings": {},
        }
        for conv in converters:
            t0 = time.monotonic()
            conversion = LC.convert_document(
                result.text, conv, lexicon,
                document_legacy_ratio=before.legacy_line_ratio,
            )
            convert_seconds += time.monotonic() - t0
            total_lines += len(conversion.lines)
            after = quality.measure_text(conversion.text)
            summary = legacy_report.summarise_conversion(conversion)
            summary.update({
                "devanagari_after": after.devanagari_ratio,
                "legacy_after": after.legacy_line_ratio,
                "char_count_after": after.char_count,
            })
            doc["mappings"][conv.mapping] = summary

            agg = per_mapping[conv.mapping]
            agg["documents"] += 1
            agg["attempted_lines"] += summary["attempted_lines"]
            agg["accepted_lines"] += summary["accepted_lines"]
            agg["ambiguous_lines"] += summary["ambiguous_lines"]
            agg["rejected_lines"] += summary["rejected_lines"]
            agg["scores"].append(summary["mean_score"])

            if conv.mapping == "Preeti":
                for line in conversion.lines:
                    if line.validation is not None and len(evidence["samples"]) < 24:
                        evidence["samples"].append({
                            "content_sha256": ref.content_sha256,
                            "band": entry.band,
                            "mapping": conv.mapping,
                            "outcome": line.disposition,
                            "before": _preview(line.original),
                            "after": _preview(line.converted or ""),
                        })
                        break

        best = max(doc["mappings"].values(), key=lambda m: m["mean_score"])
        doc["best_mapping"] = best["mapping"]
        doc["devanagari_after"] = best["devanagari_after"]
        doc["legacy_after"] = best["legacy_after"]
        # "Recovered" is deliberately strict: most of the attempted lines produced
        # usable Unicode AND the document no longer trips the native-1 flag.
        usable = best["accepted_lines"] + best["ambiguous_lines"]
        share = usable / best["attempted_lines"] if best["attempted_lines"] else 0.0
        if share >= 0.60 and best["legacy_after"] <= quality.LEGACY_LINE_RATIO:
            doc["recovery"] = "recovered"
        elif share >= 0.20:
            doc["recovery"] = "partial"
        else:
            doc["recovery"] = "unresolved"
        evidence["documents"].append(doc)
        print(f"  [{i}/{len(cohort)}] {ref.short_sha} {entry.band} "
              f"dev {before.devanagari_ratio:.3f}->{doc['devanagari_after']:.3f} "
              f"legacy {before.legacy_before if False else before.legacy_line_ratio:.3f}"
              f"->{doc['legacy_after']:.3f}  {doc['recovery']}")

    # ------------------------------------------------------------- controls --
    print("\ncontrols:")
    for kind, ref in controls:
        result = legacy_eval.read_blob_text(ref)
        before_metrics = quality.measure_text(result.text)
        conversion = LC.convert_document(
            result.text, legacy_font.converter_for("Preeti"), lexicon,
            document_legacy_ratio=before_metrics.legacy_line_ratio,
        )
        counts = conversion.counts
        row = {
            "content_sha256": ref.content_sha256,
            "kind": kind,
            "status_before": f"{ref.status}/{ref.reason}",
            "lines": len(conversion.lines),
            "guarded": counts[LC.KEPT_ENGLISH] + counts[LC.KEPT_UNICODE],
            "converted": conversion.converted_lines,
            "byte_identical": conversion.text == result.text,
        }
        evidence["controls"].append(row)
        print(f"  {ref.short_sha} {kind:<14} converted={row['converted']:<4} "
              f"{'PASS' if row['byte_identical'] else 'FAIL'}")

    # --------------------------------------------------------- spreadsheets --
    print("\nspreadsheets (separate denominator):")
    for ref in sheet_cohort:
        result = legacy_eval.read_blob_text(ref)
        if result.error:
            print(f"  {ref.short_sha} SKIP: {result.error}")
            continue
        # Cells, not rendered rows. `extraction` joins with ' | ' and `|` is a
        # Preeti codepoint; the row form would destroy every separator.
        rows = [[cell] for cell in result.text.split("\n")]
        flat = [c.strip() for line in rows for c in line[0].split(" | ")]
        conversion = LC.convert_cells([tuple(flat)], legacy_font.converter_for("Preeti"),
                                      lexicon)[0]
        after = quality.measure_text("\n".join(l.text for l in conversion.lines))
        counts = conversion.counts
        evidence["spreadsheets"].append({
            "content_sha256": ref.content_sha256,
            "status_before": f"{ref.status}/{ref.reason}",
            "cells": len(conversion.lines),
            "attempted": counts[LC.CONVERTED] + counts[LC.CONVERTED_UNJUDGED]
                         + counts[LC.AMBIGUOUS_LINE] + counts[LC.AMBIGUOUS_HELD]
                         + counts[LC.REJECTED_LINE],
            "accepted": counts[LC.CONVERTED] + counts[LC.CONVERTED_UNJUDGED],
            "ambiguous": counts[LC.AMBIGUOUS_LINE] + counts[LC.AMBIGUOUS_HELD],
            "rejected": counts[LC.REJECTED_LINE],
            "devanagari_after": after.devanagari_ratio,
        })
        print(f"  {ref.short_sha} cells={len(conversion.lines):<6} "
              f"accepted={counts[LC.CONVERTED] + counts[LC.CONVERTED_UNJUDGED]:<6} "
              f"dev_after={after.devanagari_ratio}")

    # ---------------------------------------------------------- aggregation --
    for agg in per_mapping.values():
        scores = agg.pop("scores")
        agg["mean_score"] = round(sum(scores) / len(scores), 4) if scores else 0.0
        agg["acceptance_rate"] = (
            round(agg["accepted_lines"] / agg["attempted_lines"], 4)
            if agg["attempted_lines"] else 0.0
        )
    evidence["per_mapping"] = [
        per_mapping[c.mapping] for c in converters
    ]

    bands: dict[str, int] = {}
    for e in cohort:
        bands[e.band] = bands.get(e.band, 0) + 1
    evidence["coverage"] = {
        "candidates": len(candidates),
        "pdf_selected": len(cohort),
        "spreadsheet_selected": len(sheet_cohort),
        "control_english": sum(1 for k, _ in controls if k == "english_table"),
        "control_unicode": sum(1 for k, _ in controls if k == "unicode_nepali"),
        "control_mixed_script": sum(1 for k, _ in controls if k == "mixed_script"),
        "control_no_text": sum(1 for k, _ in controls if k == "no_text_layer"),
        "unique_blobs": len({d["content_sha256"] for d in evidence["documents"]})
                        + len(controls) + len(evidence["spreadsheets"]),
        "bands": bands,
    }
    evidence["recovery"] = {
        "recovered": sum(1 for d in evidence["documents"] if d["recovery"] == "recovered"),
        "partial": sum(1 for d in evidence["documents"] if d["recovery"] == "partial"),
        "unresolved": sum(1 for d in evidence["documents"] if d["recovery"] == "unresolved"),
        "english_preservation_failures": sum(
            1 for c in evidence["controls"]
            if c["kind"] == "english_table" and not c["byte_identical"]
        ),
    }
    docs_done = len(evidence["documents"]) * len(converters)
    evidence["performance"] = {
        "lines": total_lines,
        "seconds": round(convert_seconds, 3),
        "lines_per_second": round(total_lines / convert_seconds, 1) if convert_seconds else 0.0,
        "documents_per_second": round(docs_done / convert_seconds, 3) if convert_seconds else 0.0,
    }

    Path(args.out_json).write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    Path(args.out_text).write_text(
        legacy_report.render_evaluation(evidence), encoding="utf-8"
    )
    print(f"\nwrote {args.out_json}")
    print(f"wrote {args.out_text}")
    return 1 if failures else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest", default="docs/nrb/phase6a-manifest.json")
    p.add_argument("--lexicon", default="docs/nrb/phase6b-lexicon.json")
    p.add_argument("--out-json",
                   default="docs/nrb/phase6b-legacy-conversion-evaluation.json")
    p.add_argument("--out-text",
                   default="docs/nrb/phase6b-legacy-conversion-evaluation.txt")
    p.add_argument("--extractor-version", default=EXTRACTOR_VERSION)
    return asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
