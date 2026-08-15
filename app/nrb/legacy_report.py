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
