"""Deterministic rendering for the Phase 6B legacy-conversion evaluation.

Separate from `report.py` on purpose: that module renders the standing Phase 2-6A
passes (inventory, sync, fetch, profile, calibration) and is 1,600 lines of
production reporting. This renders ONE bounded evaluation artifact, and keeping it
apart means the evaluation can be deleted or superseded without touching the
reporting every other phase depends on.

Deterministic in the same sense as the rest: counters in a fixed order, ratios
rounded, no timestamps in the body, so two runs over the same evidence produce
byte-identical output and a diff means the measurement changed.
"""

from __future__ import annotations

from typing import Any, Sequence

from . import legacy_convert as LC

__all__ = ["render_evaluation", "summarise_conversion"]

_RULE = "=" * 80
_THIN = "-" * 80


def summarise_conversion(conversion: LC.DocumentConversion) -> dict[str, Any]:
    """One document-under-one-mapping, reduced to counters and rates."""
    counts = dict(conversion.counts)
    attempted = (
        counts[LC.CONVERTED]
        + counts[LC.CONVERTED_UNJUDGED]
        + counts[LC.AMBIGUOUS_LINE]
        + counts[LC.AMBIGUOUS_HELD]
        + counts[LC.REJECTED_LINE]
    )
    accepted = counts[LC.CONVERTED] + counts[LC.CONVERTED_UNJUDGED]
    scores = [
        line.validation.score
        for line in conversion.lines
        if line.validation is not None
    ]
    return {
        "mapping": conversion.mapping,
        "counts": counts,
        "attempted_lines": attempted,
        "accepted_lines": accepted,
        "ambiguous_lines": counts[LC.AMBIGUOUS_LINE],
        "ambiguous_held_lines": counts[LC.AMBIGUOUS_HELD],
        "rejected_lines": counts[LC.REJECTED_LINE],
        "guarded_english_lines": counts[LC.KEPT_ENGLISH],
        "guarded_unicode_lines": counts[LC.KEPT_UNICODE],
        "acceptance_rate": round(accepted / attempted, 4) if attempted else 0.0,
        "mean_score": round(sum(scores) / len(scores), 4) if scores else 0.0,
    }


def _table(rows: Sequence[Sequence[str]], headers: Sequence[str]) -> str:
    widths = [
        max(len(str(headers[i])), *(len(str(r[i])) for r in rows)) if rows
        else len(str(headers[i]))
        for i in range(len(headers))
    ]
    out = ["  ".join(str(h).ljust(w) for h, w in zip(headers, widths)).rstrip()]
    out.append("  ".join("-" * w for w in widths))
    for row in rows:
        out.append("  ".join(str(c).ljust(w) for c, w in zip(row, widths)).rstrip())
    return "\n".join(out)


def render_evaluation(evidence: dict[str, Any]) -> str:
    """The whole evaluation report, from the evidence dict the pass produced."""
    L: list[str] = []
    ident = evidence["identity"]

    L += [_RULE, "NRB PHASE 6B TASK 1 — LEGACY-FONT CONVERSION EVALUATION", _RULE, ""]
    L += [
        "Evidence only. No production routing was wired, no nrb_extractions row was",
        "written, no OCR ran, nothing was chunked, embedded or ingested, and no HTTP",
        "request was made. Every byte came from NRB_FILES_DIR, already on disk.",
        "",
        "IDENTITY",
        _THIN,
        f"  parent benchmark        {ident['parent_fingerprint']}",
        f"  evaluation algorithm    {ident['algorithm']}",
        f"  cohort fingerprint      {ident['cohort_fingerprint']}",
        f"  lexicon fingerprint     {ident['lexicon_fingerprint']}",
        f"  converter               {ident['converter_name']} {ident['converter_version']}"
        f"  ({ident['converter_licence']})",
        f"  extractor version       {ident['extractor_version']}",
        "",
    ]

    cov = evidence["coverage"]
    L += [
        "COVERAGE",
        _THIN,
        f"  legacy_font_suspected in the benchmark   {cov['candidates']}",
        f"  PDF evaluation cohort selected           {cov['pdf_selected']}",
        f"  spreadsheet cohort selected              {cov['spreadsheet_selected']}",
        f"  negative controls  english tables        {cov['control_english']}",
        f"                     genuine unicode       {cov['control_unicode']}",
        f"                     mixed unicode+preeti  {cov.get('control_mixed_script', 0)}"
        "   (not a control — see below)",
        f"                     no text layer         {cov['control_no_text']}",
        f"  unique blobs evaluated                   {cov['unique_blobs']}",
        "",
        "  severity bands (legacy_line_ratio), PDF cohort:",
    ]
    for band, n in sorted(cov["bands"].items()):
        L.append(f"    {band:<12} {n}")
    L.append("")

    L += [_RULE, "PER MAPPING — PDF COHORT", _RULE, ""]
    rows = []
    for m in evidence["per_mapping"]:
        rows.append([
            m["mapping"], m["documents"], m["attempted_lines"], m["accepted_lines"],
            m["ambiguous_lines"], m["rejected_lines"],
            f"{m['acceptance_rate']:.4f}", f"{m['mean_score']:.4f}",
        ])
    L.append(_table(rows, ["mapping", "docs", "attempted", "accepted",
                           "ambiguous", "rejected", "accept_rate", "mean_score"]))
    L += ["", "  `accepted` = validated against structure AND vocabulary.",
          "  `ambiguous` = structurally sound, vocabulary too thin to confirm; the",
          "  conversion is kept and counted apart, never folded into `accepted`.",
          "  `rejected` = validation refused; the ORIGINAL text is preserved.", ""]

    L += [_RULE, "BEFORE / AFTER — PDF COHORT, BEST MAPPING PER DOCUMENT", _RULE, ""]
    rows = []
    for d in evidence["documents"]:
        rows.append([
            d["content_sha256"][:12], d["band"], d["best_mapping"],
            f"{d['devanagari_before']:.4f}", f"{d['devanagari_after']:.4f}",
            f"{d['legacy_before']:.4f}", f"{d['legacy_after']:.4f}",
            d["status_before"], d["recovery"],
        ])
    L.append(_table(rows, ["blob", "band", "mapping", "dev_before", "dev_after",
                           "legacy_before", "legacy_after", "status", "recovery"]))
    L.append("")

    rec = evidence["recovery"]
    L += [
        "RECOVERY",
        _THIN,
        f"  recovered      {rec['recovered']}   (>=60% of attempted lines accepted or ambiguous,",
        "                      legacy_line_ratio falls below the 0.20 flag)",
        f"  partial        {rec['partial']}",
        f"  unresolved     {rec['unresolved']}",
        f"  english preservation failures   {rec['english_preservation_failures']}",
        "",
    ]

    L += [_RULE, "NEGATIVE CONTROLS", _RULE, ""]
    L += ["  A control PASSES when the reconstructed document is BYTE-IDENTICAL to",
          "  the original: not merely 'mostly unchanged'.",
          "",
          "  `mixed_script` rows are NOT controls. They are documents native-1 called",
          "  clean that hold real Devanagari AND real Preeti in one file, so changed",
          "  lines there are correct conversions. They are listed because a mixed",
          "  Unicode/Preeti document passing as clean is a finding in its own right.",
          ""]
    rows = []
    for c in evidence["controls"]:
        rows.append([
            c["content_sha256"][:12], c["kind"], c["lines"], c["guarded"],
            c["converted"], "PASS" if c["byte_identical"] else "FAIL",
        ])
    L.append(_table(rows, ["blob", "control", "lines", "guarded", "converted",
                           "result"]))
    L.append("")

    if evidence["spreadsheets"]:
        L += [_RULE, "SPREADSHEETS — SEPARATE DENOMINATOR", _RULE, ""]
        L += ["  Reported apart from the PDF cohort throughout. Conversion is PER CELL:",
              "  extraction renders a row as ' | '.join(cells) and `|` is a Preeti",
              "  codepoint, so converting rendered rows destroys every separator.", ""]
        rows = []
        for s in evidence["spreadsheets"]:
            rows.append([
                s["content_sha256"][:12], s["status_before"], s["cells"],
                s["attempted"], s["accepted"], s["ambiguous"], s["rejected"],
                f"{s['devanagari_after']:.4f}",
            ])
        L.append(_table(rows, ["blob", "native-1", "cells", "attempted", "accepted",
                               "ambiguous", "rejected", "dev_after"]))
        L.append("")

    perf = evidence["performance"]
    L += [
        _RULE, "PERFORMANCE", _RULE, "",
        f"  lines converted        {perf['lines']:,}",
        f"  conversion time        {perf['seconds']:.2f}s",
        f"  lines/sec              {perf['lines_per_second']:,.0f}",
        f"  documents/sec          {perf['documents_per_second']:.2f}",
        "",
        "  For comparison, from Phase 6A's measured run over the same corpus:",
        "    pypdf native extraction   4,285 pages in 211.6s",
        "    Docling                   37 PDFs in 2,354.9s  (76.2x pypdf)",
        "  OCR cost is NOT measured here and no claim is made about it.",
        "",
    ]

    L += [_RULE, "SAMPLES FOR MANUAL REVIEW", _RULE, ""]
    for s in evidence["samples"]:
        L += [
            f"  {s['content_sha256'][:12]}  band={s['band']}  mapping={s['mapping']}"
            f"  outcome={s['outcome']}",
            f"    BEFORE : {s['before']}",
            f"    AFTER  : {s['after']}",
            "",
        ]

    return "\n".join(L) + "\n"


def render_native2(ev: dict[str, Any]) -> str:
    """The native-1 vs native-2 comparison, from the evidence the compare pass built.

    Deterministic: every counter is emitted in sorted order and no timestamp
    enters the body, so re-running over unchanged rows reproduces the file byte
    for byte and a diff means a measurement moved.
    """
    L: list[str] = []
    ident = ev["identity"]

    L += [_RULE, "NRB PHASE 6B TASK 2 — native-1 vs native-2 OVER THE FROZEN BENCHMARK",
          _RULE, ""]
    L += [
        "Classification only. No legacy conversion ran, npttf2utf was never invoked,",
        "no OCR, no chunking, no embeddings, no pgvector, and zero HTTP requests —",
        "the same 381 blobs Phase 6A fetched, re-read from disk and re-judged.",
        "native-1's rows are untouched and sit alongside native-2's for comparison.",
        "",
        "IDENTITY",
        _THIN,
        f"  parent benchmark      {ident['parent_fingerprint']}",
        f"  manifest entries      {ident['manifest_entries']}",
        f"  {ident['v1']} rows           {ident['v1_rows']}",
        f"  {ident['v2']} rows           {ident['v2_rows']}",
        f"  compared (both)       {ident['compared']}",
        f"  never fetched (404)   {ident['unavailable']}   — a stated gap, not retried",
        "",
    ]

    L += [_RULE, "STATUS TRANSITIONS", _RULE, ""]
    rows = [[a, "->", b, n] for (a, b), n in sorted(ev["status_transitions"].items())]
    L.append(_table(rows, ["native-1", "", "native-2", "blobs"]))
    L.append("")

    L += [_RULE, "REASON TRANSITIONS", _RULE, ""]
    rows = [[a, "->", b, n] for (a, b), n in sorted(ev["reason_transitions"].items())]
    L.append(_table(rows, ["native-1", "", "native-2", "blobs"]))
    L += [
        "",
        f"  legacy_font_suspected -> not suspected : {len(ev['suspicious_to_clean'])}",
        f"  not suspected -> legacy_font_suspected : {len(ev['clean_to_suspicious'])}",
        "",
        "  A transition is a CHANGE, not a vindication. The manual review of these",
        "  cases is in docs/nrb/phase6b-native2-manual-review.txt.",
        "",
    ]

    L += [_RULE, "ENGLISH-TABLE NEGATIVE CONTROLS", _RULE, ""]
    L += ["  The six Docling-rescue tables from phase6a-calibration.txt plus the",
          "  hand-reviewed false positive from phase6a-profile.txt STEP 5. Every one",
          "  is readable English that native-1 flagged.", ""]
    rows = [
        [c["sha"], f"{c['legacy_before']:.4f}",
         f"{c['unit_ratio']:.4f}" if c["unit_ratio"] is not None else "-",
         c["v1"], c["v2"], "CORRECTED" if c["corrected"] else "unchanged"]
        for c in ev["english_controls"]
    ]
    L.append(_table(rows, ["blob", "n1_legacy", "n2_unit", "native-1", "native-2",
                           "result"]))
    corrected = sum(1 for c in ev["english_controls"] if c["corrected"])
    L += ["", f"  corrected: {corrected} of {len(ev['english_controls'])}", ""]

    sheets = ev["spreadsheets"]
    L += [_RULE, "SPREADSHEETS", _RULE, ""]
    L += [
        "  native-1 judges a workbook STRUCTURALLY and returns before any linguistic",
        "  rule, so every parsed benchmark spreadsheet came back clean. native-2",
        "  scores its CELLS — never the ' | '-joined row, because `|` is a Preeti",
        "  codepoint.",
        "",
        f"  benchmark spreadsheets           {sheets['total']}",
        f"  with text-bearing cells          {sheets['with_text_cells']}",
        f"  native-1 legacy-suspected        {sheets['v1_legacy']}",
        f"  native-2 legacy-suspected        {sheets['v2_legacy']}",
        f"  newly flagged by native-2        {len(sheets['newly_flagged'])}",
        "",
    ]
    for sha in sheets["newly_flagged"]:
        L.append(f"    {sha[:12]}")
    L.append("")

    L += [_RULE, "MINORITY LEGACY REGIONS", _RULE, ""]
    L += [
        "  Documents routed suspicious by the REGION rule despite a global",
        "  legacy_line_ratio the 0.20 threshold would pass. The global threshold was",
        "  NOT lowered — see docs/nrb-integration.md §13.",
        "",
        f"  detected: {len(ev['minority'])}",
        "",
    ]
    for sha in ev["minority"][:40]:
        L.append(f"    {sha[:12]}")
    L.append("")

    L += [_RULE, "NAMED REGRESSION CASES", _RULE, ""]
    for label, c in sorted(ev["named_cases"].items()):
        L += [
            f"  {label}  ({c['sha']})",
            f"    native-1 {c['v1']}   ->   native-2 {c['v2']}",
            f"    legacy_line_ratio={c['legacy_line_ratio']}  unit_ratio={c['unit_ratio']}"
            f"  legacy_units={c['legacy_units']}  max_run={c['max_run']}"
            f"  contested={c['contested']}",
            f"    warnings={c['warnings']}",
            "",
        ]

    L += [_RULE, "LEGACY SEVERITY DISTRIBUTION", _RULE, ""]
    L += ["  Over blobs each version calls legacy_font_suspected. The native-1",
          "  legacy_line_ratio is reported for BOTH versions so Phase 6B Task 1's",
          "  severity evidence stays readable, alongside native-2's own unit ratio.",
          ""]
    order = [">=0.80", "0.50-0.80", "0.20-0.50", "<=0.20"]
    rows = [
        [band,
         ev["bands"]["v1"].get(band, 0),
         ev["bands"]["v2_by_legacy_line_ratio"].get(band, 0),
         ev["bands"]["v2_by_unit_ratio"].get(band, 0)]
        for band in order
    ]
    L.append(_table(rows, ["band", "n1 (n1 ratio)", "n2 (n1 ratio)",
                           "n2 (unit ratio)"]))
    L += ["", "  `<=0.20` under native-2 is the minority-region population: flagged by",
          "  the region rule, not by the global ratio.", ""]

    if ev["changed_detail"]:
        L += [_RULE, "EVERY CHANGED BLOB", _RULE, ""]
        rows = [
            [c["sha"][:12], c["family"], c["v1"], c["v2"],
             f"{c['legacy_line_ratio']:.4f}",
             f"{c['unit_ratio']:.4f}" if c["unit_ratio"] is not None else "-",
             c["legacy_units"] if c["legacy_units"] is not None else "-",
             c["max_run"] if c["max_run"] is not None else "-",
             f"{c['contested']:.3f}" if c["contested"] is not None else "-",
             c["english_units"] if c["english_units"] is not None else "-",
             ",".join(c["warnings"]) or "-"]
            for c in ev["changed_detail"]
        ]
        L.append(_table(rows, ["blob", "family", "native-1", "native-2",
                               "n1_ratio", "unit_ratio", "leg_units", "run",
                               "contested", "eng_units", "warnings"]))
        L.append("")

    return "\n".join(L) + "\n"
