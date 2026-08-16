#!/usr/bin/env python
"""Phase 6B Task 3 — validate native-2 (and the candidate conversion gate) on the
FROZEN, INDEPENDENT routing holdout. READ-ONLY on the database.

    DATABASE_URL=postgresql+asyncpg://…/local_ai_gateway_p4 \
        .venv/bin/python scripts/nrb_holdout_validate.py \
            --holdout  docs/nrb/phase6b-routing-holdout.json \
            --phase6a  docs/nrb/phase6a-manifest.json \
            --lexicon  docs/nrb/phase6b-lexicon.json \
            --out-json    docs/nrb/phase6b-routing-holdout-profile.json \
            --out-profile docs/nrb/phase6b-routing-holdout-profile.txt \
            --out-review  docs/nrb/phase6b-routing-holdout-manual-review.txt

WHAT THIS IS
    The holdout excludes every Phase 6A comparison_key (proven, and re-checked
    here), so the native-2 rows it reads were produced by the *exact* classifier
    committed at 2a6b498 over files that never influenced it. This command
    measures, on those files:

      * the native-2 status/reason distribution and, for legacy_font_suspected,
        the unit_legacy_ratio bands — the SAME metric the candidate conversion
        gate is defined on (`unit_legacy_ratio >= 0.80`), never native-1's
        document-level legacy_line_ratio;
      * the candidate conversion queue's behaviour, using npttf2utf ONLY as an
        evaluation instrument through `legacy_font.py` — no vendoring, and the
        GPL-3.0 distribution gate is untouched;
      * the input guards on native-2's clean English/numeric/Unicode negatives.

WHAT IT WILL NOT DO
    It converts nothing in the database, writes no extraction row, runs no OCR,
    makes no network request, and NEVER tunes a threshold. Conversion CORRECTNESS
    (is the recovered Devanagari right Nepali?) is not decided here — that is a
    Nepali reader's call, and the manual-review artifact marks those
    `awaiting_nepali_review`. What IS decided here is script-independent: whether a
    routed input is English/numeric/Unicode (a false route) versus glyph-mapped,
    and whether the converter destroyed a clean control.

Exit codes: 0 completed; 1 completed but a blob could not be read; 2 could not
start (leakage detected, missing artifact, converter unavailable).
"""

from __future__ import annotations

import argparse
import asyncio
import collections
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
    routing,
    units,
)

# Named regression cases carried from Task 2, so the holdout report can say
# whether the same shapes behave the same way on unseen data. These are Phase 6A
# blobs and will NOT be in the holdout; they are referenced only if they happen to
# recur by content (a blob NRB republished). Kept for cross-reference labelling.
HIGH_BAND = 0.80
MID_BAND = 0.50
LOW_BAND = 0.20


def _preview(text: str, n: int = 110) -> str:
    return " ".join(text.split())[:n]


def _band(unit_ratio: float) -> str:
    if unit_ratio >= HIGH_BAND:
        return ">=0.80"
    if unit_ratio >= MID_BAND:
        return "0.50-0.80"
    if unit_ratio > LOW_BAND:
        return "0.20-0.50"
    return "<=0.20"


def _legacy_by_units(m: dict) -> bool:
    """Recompute native-2's document-level unit call from stored metrics — the same
    predicate `routing._legacy_by_units` applies, so 'minority-region-only' can be
    separated from 'flagged on the unit ratio'."""
    ratio = float(m.get("unit_legacy_ratio") or 0.0)
    if ratio <= quality.LEGACY_LINE_RATIO:
        return False
    judged = int(m.get("unit_judged") or 0)
    legacy = int(m.get("unit_legacy_candidates") or 0)
    return judged >= routing.MIN_JUDGED_FOR_RATIO or legacy >= routing.MIN_LEGACY_ABSOLUTE


def _legacy_unit_english(ref, text: str, lexicon) -> tuple[int, int]:
    """`(legacy units, how many of them are CONFIDENTLY ENGLISH)`.

    The sharpest false-positive signal available, and the one this holdout was run
    to find: it interrogates the very units native-2 called glyph-mapped, rather
    than the document around them. A document whose "legacy" units are readable
    English was mis-routed, however English-poor the rest of it looks.

    Units are built exactly as native-2 builds them — CELLS for a workbook (`|` is
    a Preeti codepoint, so a rendered row must never be judged), lines otherwise.
    """
    if ref.family == "spreadsheet":
        raw = [c.strip() for line in text.split("\n") for c in line.split(" | ")
               if c.strip()]
    else:
        raw = list(text.splitlines())
    legacy = [u for u in raw if units.assess_unit(u).state == units.STATE_LEGACY]
    english = sum(1 for u in legacy if LX.is_confidently_english(u, lexicon))
    return len(legacy), english


def _english_share(text: str, lexicon) -> float:
    """Share of substantive lines that are CONFIDENTLY English — the script-
    independent signal for a false route. A high-band legacy verdict over text this
    English would be a false positive of exactly the kind §22 warns about."""
    lines = [ln for ln in text.splitlines() if len(ln.split()) >= 3]
    if not lines:
        return 0.0
    hits = sum(1 for ln in lines if LX.is_confidently_english(ln, lexicon))
    return round(hits / len(lines), 4)


def _doc_ratio(ref) -> float:
    """The document severity used to gate unjudged-unit conversion.

    native-2's OWN unit_legacy_ratio — NOT native-1's legacy_line_ratio. This is
    the whole point of validating native-2: a document it routes at unit ratio 1.0
    while its native-1 legacy_line_ratio sits below 0.20 (the big Preeti workbooks,
    and every minority case) is overwhelmingly legacy by the router's judgement, so
    its unjudged headings and cells should convert. Feeding legacy_line_ratio here
    would under-convert exactly the population native-2 exists to catch. For a
    native-2-clean document the ratio is low, so the unjudged path stays closed and
    only positively glyph-mapped units are ever touched.
    """
    return float(ref.metrics.get("unit_legacy_ratio") or 0.0)


def _convert(ref, result, lexicon, conv):
    """Convert one blob with ONE mapping. Spreadsheets per cell (never the rendered
    ' | ' row — `|` is a Preeti codepoint), text per line. Returns the
    DocumentConversion and the after-metrics."""
    ratio = _doc_ratio(ref)
    if ref.family == "spreadsheet":
        flat = [
            c.strip()
            for line in result.text.split("\n")
            for c in line.split(" | ")
            if c.strip()
        ]
        conversion = LC.convert_cells(
            [tuple(flat)], conv, lexicon, document_legacy_ratio=ratio
        )[0]
        after_text = "\n".join(l.text for l in conversion.lines)
    else:
        conversion = LC.convert_document(
            result.text, conv, lexicon, document_legacy_ratio=ratio
        )
        after_text = conversion.text
    return conversion, quality.measure_text(after_text)


def _recovery(summary: dict, legacy_after: float) -> str:
    """Task 1's strict recovery label, reused verbatim. NOT a correctness claim —
    it says the converter produced usable Unicode and cleared the native-1 flag,
    which is a routing/recovery-rate signal, not semantic verification."""
    attempted = summary["attempted_lines"]
    usable = summary["accepted_lines"] + summary["ambiguous_lines"] + summary["ambiguous_held_lines"]
    share = usable / attempted if attempted else 0.0
    if share >= 0.60 and legacy_after <= quality.LEGACY_LINE_RATIO:
        return "recovered"
    if share >= 0.20:
        return "partial"
    return "unresolved"


async def run(args) -> int:
    from app.db.session import SessionLocal

    holdout = json.loads(Path(args.holdout).read_text(encoding="utf-8"))
    phase6a = json.loads(Path(args.phase6a).read_text(encoding="utf-8"))
    hold_entries = {e["comparison_key"]: e for e in holdout["entries"]}
    hold_keys = set(hold_entries)
    p6a_keys = {e["comparison_key"] for e in phase6a["entries"]}

    # A leakage guard at RUN time, not just in the committed test: if the holdout
    # and Phase 6A ever share a key, the whole exercise is void and must stop.
    leak = hold_keys & p6a_keys
    if leak:
        print(f"ERROR: {len(leak)} Phase 6A keys leaked into the holdout — aborting",
              file=sys.stderr)
        return 2

    lexicon = LX.load_lexicon(args.lexicon)
    converters = legacy_font.converters()
    if not converters:
        print("ERROR: no converter mappings available", file=sys.stderr)
        return 2
    preeti = legacy_font.converter_for("Preeti")

    async with SessionLocal() as session:
        every = await legacy_eval.load_blob_refs(
            session, extractor_version=routing.EXTRACTOR_VERSION_V2
        )

    # A blob is in the holdout iff one of the file rows pointing at it is a holdout
    # key. Deduped by content_sha256 already (load_blob_refs merges).
    refs = [r for r in every if hold_keys & set(r.comparison_keys)]
    refs.sort(key=lambda r: r.content_sha256)

    def entry_for(ref):
        for k in ref.comparison_keys:
            if k in hold_entries:
                return hold_entries[k]
        return {}

    # ---------------------------------------------------------------- profile --
    ev: dict = {
        "identity": {
            "holdout_fingerprint": holdout["selection_sha256"],
            "phase6a_fingerprint": phase6a["selection_sha256"],
            "holdout_keys": len(hold_keys),
            "phase6a_exclusion_verified_empty_intersection": True,
            "extractor_version": routing.EXTRACTOR_VERSION_V2,
            "lexicon_fingerprint": lexicon.fingerprint,
            "converter_name": converters[0].name,
            "converter_version": converters[0].version,
            "converter_licence": "GPL-3.0 — distribution gate, see requirements-nrb.txt",
            "mappings_available": [c.mapping for c in converters],
            "unique_blobs": len(refs),
        },
        "coverage": {
            "requested": len(hold_keys),
            "fetched_unique_blobs": len(refs),
            "not_extracted_keys": len(hold_keys)
            - len({k for r in refs for k in r.comparison_keys if k in hold_keys}),
        },
    }

    status_counts = collections.Counter(r.status for r in refs)
    reason_counts = collections.Counter(r.reason for r in refs)
    ev["status_distribution"] = dict(sorted(status_counts.items()))
    ev["reason_distribution"] = dict(sorted(reason_counts.items()))

    legacy_refs = [r for r in refs if r.reason == "legacy_font_suspected"]
    band_counts = collections.Counter()
    minority_only = []
    for r in legacy_refs:
        m = r.metrics
        by_units = _legacy_by_units(m)
        if by_units:
            band_counts[_band(float(m.get("unit_legacy_ratio") or 0.0))] += 1
        else:
            band_counts["minority-region-only"] += 1
            minority_only.append(r.content_sha256)
    ev["legacy_bands"] = dict(band_counts)
    ev["minority_region_total"] = sum(
        1 for r in legacy_refs if int(r.metrics.get("minority_legacy_detected") or 0)
    )
    ev["minority_region_only"] = sorted(minority_only)

    # by cohort / year / document_type / resource_type
    def _bucketed(field_fn):
        out: dict[str, dict[str, int]] = {}
        for r in refs:
            key = field_fn(r)
            b = out.setdefault(str(key), {"total": 0, "legacy": 0})
            b["total"] += 1
            if r.reason == "legacy_font_suspected":
                b["legacy"] += 1
        return dict(sorted(out.items()))

    from app.nrb.sampling import year_cohort
    ev["by_cohort"] = _bucketed(lambda r: year_cohort(entry_for(r).get("year")))
    ev["by_year"] = _bucketed(lambda r: entry_for(r).get("year"))
    ev["by_document_type"] = _bucketed(lambda r: entry_for(r).get("document_type"))
    ev["by_resource_type"] = _bucketed(lambda r: entry_for(r).get("resource_type"))

    # spreadsheets specifically
    sheets = [r for r in refs if r.family == "spreadsheet"]
    ev["spreadsheets"] = {
        "total": len(sheets),
        "legacy": sum(1 for r in sheets if r.reason == "legacy_font_suspected"),
        "flagged": sorted(r.content_sha256 for r in sheets
                          if r.reason == "legacy_font_suspected"),
    }

    # -------------------------------------------------- conversion evaluation --
    # Over EVERY suspicious blob (the candidate + measured bands), Preeti only for
    # the primary path; all mappings recorded on the high band for a mapping-choice
    # sanity check. Correctness is not claimed — see recovery vs confirmed.
    read_failures = 0
    conv_seconds = 0.0
    documents = []
    for r in legacy_refs:
        m = r.metrics
        by_units = _legacy_by_units(m)
        unit_ratio = float(m.get("unit_legacy_ratio") or 0.0)
        band = _band(unit_ratio) if by_units else "minority-region-only"
        result = legacy_eval.read_blob_text(r)
        if result.error:
            read_failures += 1
            documents.append({
                "content_sha256": r.content_sha256, "band": band,
                "family": r.family, "error": result.error,
            })
            continue
        t0 = time.monotonic()
        conversion, after = _convert(r, result, lexicon, preeti)
        conv_seconds += time.monotonic() - t0
        summary = legacy_report.summarise_conversion(conversion)
        english_share = _english_share(result.text, lexicon)
        n_legacy, n_legacy_english = _legacy_unit_english(r, result.text, lexicon)
        legacy_english_share = round(n_legacy_english / n_legacy, 4) if n_legacy else 0.0
        recovery = _recovery(summary, after.legacy_line_ratio)
        # Script-independent routing verdict, sharpest evidence FIRST. A document
        # whose flagged units are themselves readable English is mis-routed, and
        # that judgement outranks any downstream conversion outcome: the converter
        # will happily turn an English accounting label into Devanagari, which is
        # precisely the failure Task 1 proved output signals cannot see.
        if legacy_english_share >= 0.50:
            verdict = "false_route_english"
        elif recovery in ("recovered", "partial"):
            verdict = "legacy_recovered"      # glyph-mapped, converter produced Unicode
        elif english_share >= 0.50:
            verdict = "false_route_english"   # readable English routed as legacy
        elif after.devanagari_ratio >= LC.MIN_DEVANAGARI_AFTER:
            verdict = "unknown_or_other_legacy_mapping"  # Devanagari-ish but unvalidated
        else:
            verdict = "unresolved"
        documents.append({
            "content_sha256": r.content_sha256,
            "comparison_keys": list(r.comparison_keys),
            "band": band,
            "family": r.family,
            "unit_legacy_ratio": unit_ratio,
            "legacy_line_ratio": r.legacy_line_ratio,
            "devanagari_before": r.devanagari_ratio,
            "devanagari_after": after.devanagari_ratio,
            "legacy_after": after.legacy_line_ratio,
            "english_share": english_share,
            "legacy_units": n_legacy,
            "legacy_units_english": n_legacy_english,
            "legacy_units_english_share": legacy_english_share,
            "attempted": summary["attempted_lines"],
            "accepted": summary["accepted_lines"],
            "ambiguous": summary["ambiguous_lines"] + summary["ambiguous_held_lines"],
            "rejected": summary["rejected_lines"],
            "guarded_english": summary["guarded_english_lines"],
            "guarded_unicode": summary["guarded_unicode_lines"],
            "recovery": recovery,
            "routing_verdict": verdict,
        })
    ev["conversion_documents"] = documents

    def _queue(pred):
        return [d for d in documents if "error" not in d and pred(d)]

    high = _queue(lambda d: d["band"] == ">=0.80")
    mid = _queue(lambda d: d["band"] == "0.50-0.80")
    low = _queue(lambda d: d["band"] == "0.20-0.50")
    minor = _queue(lambda d: d["band"] == "minority-region-only")

    def _verdicts(rows):
        return dict(collections.Counter(d["routing_verdict"] for d in rows))

    ev["conversion_by_band"] = {
        ">=0.80 (candidate queue)": {"n": len(high), "verdicts": _verdicts(high)},
        "0.50-0.80": {"n": len(mid), "verdicts": _verdicts(mid)},
        "0.20-0.50": {"n": len(low), "verdicts": _verdicts(low)},
        "minority-region-only": {"n": len(minor), "verdicts": _verdicts(minor)},
    }

    # The candidate gate's headline numbers (§24). "true legacy" and "recovered"
    # are kept apart deliberately (§25): routing precision != converter recovery.
    ev["candidate_queue"] = {
        "metric": "unit_legacy_ratio >= 0.80 AND status=suspicious/legacy_font_suspected",
        "routed": len(high),
        "false_route_english": sum(1 for d in high if d["routing_verdict"] == "false_route_english"),
        "legacy_recovered": sum(1 for d in high if d["routing_verdict"] == "legacy_recovered"),
        "unknown_or_other_mapping": sum(1 for d in high if d["routing_verdict"] == "unknown_or_other_legacy_mapping"),
        "unresolved": sum(1 for d in high if d["routing_verdict"] == "unresolved"),
        "recovered": sum(1 for d in high if d["recovery"] == "recovered"),
        "partial": sum(1 for d in high if d["recovery"] == "partial"),
    }

    # ---------------------------------------------------- input-guard controls --
    # TRUE negative controls: native-2 clean AND carrying ZERO legacy units
    # (unit_legacy_candidates == 0). The converter must touch NOTHING — a text
    # document reconstructs byte-identically, a spreadsheet converts zero cells.
    # This is the clean §16/§22 guarantee, uncontaminated by any minority legacy
    # content. Deterministic: lowest content_sha256 in each class.
    clean = [r for r in refs if r.reason == "clean"]
    no_legacy = [r for r in clean if int(r.metrics.get("unit_legacy_candidates") or 0) == 0]
    english_like = sorted(
        (r for r in no_legacy
         if int(r.metrics.get("unit_english") or 0) >= 3 and r.devanagari_ratio < 0.05),
        key=lambda r: r.content_sha256,
    )[:5]
    unicode_like = sorted(
        (r for r in no_legacy if r.devanagari_ratio >= 0.30),
        key=lambda r: r.content_sha256,
    )[:5]
    controls = []
    for kind, group in (("english_numeric_clean", english_like),
                        ("genuine_unicode", unicode_like)):
        for r in group:
            result = legacy_eval.read_blob_text(r)
            if result.error:
                read_failures += 1
                continue
            conversion, _ = _convert(r, result, lexicon, preeti)
            controls.append({
                "content_sha256": r.content_sha256,
                "kind": kind,
                "family": r.family,
                "devanagari_ratio": r.devanagari_ratio,
                "converted_units": conversion.converted_lines,
                "byte_identical": conversion.text == result.text
                if r.family != "spreadsheet" else None,
                "guarded_english": conversion.counts[LC.KEPT_ENGLISH],
                "guarded_unicode": conversion.counts[LC.KEPT_UNICODE],
            })
    ev["input_guard_controls"] = controls
    ev["input_guard_summary"] = {
        "english_numeric": {
            "n": sum(1 for c in controls if c["kind"] == "english_numeric_clean"),
            "any_converted": sum(1 for c in controls
                                 if c["kind"] == "english_numeric_clean" and c["converted_units"]),
        },
        "genuine_unicode": {
            "n": sum(1 for c in controls if c["kind"] == "genuine_unicode"),
            "any_converted": sum(1 for c in controls
                                 if c["kind"] == "genuine_unicode" and c["converted_units"]),
        },
    }

    # ------------------------------------------------ §23 false-negative scan --
    # native-2 CLEAN documents that nonetheless carry legacy units
    # (unit_legacy_candidates > 0 but the document was not flagged). These are the
    # honest false-negative candidates: legacy Nepali the router let through. NOT
    # guard failures — the converter only ever touches a unit the detector itself
    # calls glyph-mapped (English/Unicode units are kept by the guards, which run
    # first). Reported with the count of legacy units the converter confirmed, so a
    # scattered singleton (noise) is distinguishable from a real missed region.
    fn_candidates = sorted(
        (r for r in clean if int(r.metrics.get("unit_legacy_candidates") or 0) > 0),
        key=lambda r: r.content_sha256,
    )
    false_negatives = []
    for r in fn_candidates:
        result = legacy_eval.read_blob_text(r)
        if result.error:
            read_failures += 1
            continue
        conversion, _ = _convert(r, result, lexicon, preeti)
        false_negatives.append({
            "content_sha256": r.content_sha256,
            "family": r.family,
            "unit_legacy_candidates": int(r.metrics.get("unit_legacy_candidates") or 0),
            "unit_judged": int(r.metrics.get("unit_judged") or 0),
            "unit_legacy_ratio": float(r.metrics.get("unit_legacy_ratio") or 0.0),
            "unit_max_legacy_run": int(r.metrics.get("unit_max_legacy_run") or 0),
            "converted_units": conversion.converted_lines,
            "guarded_english": conversion.counts[LC.KEPT_ENGLISH],
        })
    ev["false_negative_scan"] = {
        "clean_documents": sum(1 for r in refs if r.reason == "clean"),
        "clean_with_legacy_units": len(fn_candidates),
        "detail": false_negatives,
    }

    # ------------------------------------------- the false-positive class (§22) --
    # Documents native-2 routed as legacy whose FLAGGED UNITS are actually readable
    # English. Reported as its own finding, by band, because a class confined below
    # the conversion gate is a caveat while the same class inside it would be a
    # production blocker.
    fp = [d for d in documents
          if "error" not in d and d["legacy_units_english_share"] >= 0.50]
    ev["false_positive_class"] = {
        "definition": ">=50% of the units native-2 flagged legacy are confidently English",
        "total": len(fp),
        "of_suspicious": len([d for d in documents if "error" not in d]),
        "in_high_band": sum(1 for d in fp if d["band"] == ">=0.80"),
        "by_band": dict(collections.Counter(d["band"] for d in fp)),
        "detail": [
            {
                "content_sha256": d["content_sha256"], "family": d["family"],
                "band": d["band"], "unit_legacy_ratio": d["unit_legacy_ratio"],
                "legacy_units": d["legacy_units"],
                "legacy_units_english": d["legacy_units_english"],
            }
            for d in sorted(fp, key=lambda x: x["content_sha256"])
        ],
    }

    ev["performance"] = {
        "conversion_seconds": round(conv_seconds, 2),
        "suspicious_blobs_converted": len([d for d in documents if "error" not in d]),
    }
    ev["read_failures"] = read_failures

    Path(args.out_json).write_text(
        json.dumps(ev, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    Path(args.out_profile).write_text(_render_profile(ev), encoding="utf-8")
    Path(args.out_review).write_text(
        _render_review(ev, refs, hold_entries, lexicon, preeti), encoding="utf-8"
    )
    print(f"holdout blobs: {len(refs)}   suspicious: {len(legacy_refs)}   "
          f"high-band queue: {len(high)}")
    print(f"wrote {args.out_json}")
    print(f"wrote {args.out_profile}")
    print(f"wrote {args.out_review}")
    return 1 if read_failures else 0


def _render_profile(ev: dict) -> str:
    i = ev["identity"]
    out: list[str] = []
    w = out.append
    w("=" * 80)
    w("NRB PHASE 6B TASK 3 — INDEPENDENT ROUTING HOLDOUT PROFILE")
    w("=" * 80)
    w("")
    w("Generated by scripts/nrb_holdout_validate.py. READ-ONLY: no extraction row,")
    w("no conversion, no OCR, no network. Conversion CORRECTNESS is a Nepali")
    w("reader's call — see the manual-review companion. What is decided here is")
    w("script-independent: false routes (English/numeric/Unicode) and destroyed")
    w("clean controls.")
    w("")
    w(f"holdout fingerprint   {i['holdout_fingerprint']}")
    w(f"phase6a excluded      {i['phase6a_fingerprint']}  (intersection empty)")
    w(f"extractor version     {i['extractor_version']}")
    w(f"lexicon              {i['lexicon_fingerprint'][:16]}…")
    w(f"converter            {i['converter_name']} {i['converter_version']}  "
      f"({i['converter_licence']})")
    w(f"unique blobs          {i['unique_blobs']}")
    c = ev["coverage"]
    w(f"coverage              {c['fetched_unique_blobs']} unique blobs of "
      f"{c['requested']} holdout keys")
    w("")
    w("-" * 80)
    w("NATIVE-2 STATUS / REASON DISTRIBUTION")
    w("-" * 80)
    for k, v in ev["status_distribution"].items():
        w(f"  status  {k:<24} {v}")
    w("")
    for k, v in ev["reason_distribution"].items():
        w(f"  reason  {k:<24} {v}")
    w("")
    w("legacy_font_suspected by unit_legacy_ratio band:")
    for k, v in sorted(ev["legacy_bands"].items()):
        w(f"    {k:<22} {v}")
    w(f"  region rule co-fires on {ev['minority_region_total']} of the flagged docs "
      f"(most genuine legacy docs also satisfy it);")
    w(f"  routed SOLELY by the region rule (low global ratio): "
      f"{len(ev['minority_region_only'])}")
    w("")
    w("-" * 80)
    w("SPREADSHEETS")
    w("-" * 80)
    s = ev["spreadsheets"]
    w(f"  total {s['total']}   flagged legacy {s['legacy']}")
    for sha in s["flagged"]:
        w(f"    {sha[:12]}")
    w("")
    w("-" * 80)
    w("CANDIDATE CONVERSION QUEUE  (unit_legacy_ratio >= 0.80)")
    w("-" * 80)
    q = ev["candidate_queue"]
    w(f"  metric: {q['metric']}")
    w(f"  routed:                    {q['routed']}")
    w(f"  false route (English):     {q['false_route_english']}")
    w(f"  legacy recovered:          {q['legacy_recovered']}")
    w(f"  unknown/other mapping:     {q['unknown_or_other_mapping']}")
    w(f"  unresolved:                {q['unresolved']}")
    w(f"    of which recovered:      {q['recovered']}   partial: {q['partial']}")
    w("")
    w("conversion routing verdicts by band:")
    for band, d in ev["conversion_by_band"].items():
        w(f"  {band:<26} n={d['n']:<4} {d['verdicts']}")
    w("")
    w("-" * 80)
    w("INPUT-GUARD CONTROLS  (native-2 clean; must survive the converter untouched)")
    w("-" * 80)
    g = ev["input_guard_summary"]
    w(f"  english/numeric clean: {g['english_numeric']['n']} controls, "
      f"{g['english_numeric']['any_converted']} had ANY unit converted")
    w(f"  genuine unicode:       {g['genuine_unicode']['n']} controls, "
      f"{g['genuine_unicode']['any_converted']} had ANY unit converted")
    for c in ev["input_guard_controls"]:
        bi = c["byte_identical"]
        tag = "byte-identical" if bi else ("cells" if bi is None else "CHANGED")
        w(f"    {c['content_sha256'][:12]} {c['kind']:<22} conv={c['converted_units']:<3} {tag}")
    w("")
    w("-" * 80)
    w("FALSE-POSITIVE CLASS  (flagged units that are actually readable English)")
    w("-" * 80)
    f = ev["false_positive_class"]
    w(f"  definition: {f['definition']}")
    w(f"  blobs:                     {f['total']} of {f['of_suspicious']} suspicious")
    w(f"  IN THE HIGH BAND (>=0.80): {f['in_high_band']}   <-- the number that gates production")
    w(f"  by band: {f['by_band']}")
    for d in f["detail"]:
        w(f"    {d['content_sha256'][:12]} {d['family']:<11} band={d['band']:<12} "
          f"unit_ratio={d['unit_legacy_ratio']:<7} "
          f"legacy_units={d['legacy_units']} english={d['legacy_units_english']}")
    w("")
    w("-" * 80)
    w("FALSE-NEGATIVE SCAN  (native-2 CLEAN docs that still carry legacy units)")
    w("-" * 80)
    fn = ev["false_negative_scan"]
    w(f"  clean documents:            {fn['clean_documents']}")
    w(f"  clean WITH legacy units:    {fn['clean_with_legacy_units']}  "
      f"(false-negative candidates — legacy content the router let through)")
    for d in fn["detail"]:
        w(f"    {d['content_sha256'][:12]} {d['family']:<11} "
          f"legacy_units={d['unit_legacy_candidates']:<3} judged={d['unit_judged']:<4} "
          f"ratio={d['unit_legacy_ratio']:<7} max_run={d['unit_max_legacy_run']:<3} "
          f"converted={d['converted_units']}")
    w("")
    w("-" * 80)
    w("BY YEAR COHORT (total / legacy)")
    w("-" * 80)
    for k, b in ev["by_cohort"].items():
        w(f"  {k:<14} {b['total']:>3} / {b['legacy']}")
    w("")
    w("BY RESOURCE TYPE (total / legacy)")
    for k, b in ev["by_resource_type"].items():
        w(f"  {k:<14} {b['total']:>3} / {b['legacy']}")
    w("")
    w("BY DOCUMENT TYPE (total / legacy)")
    for k, b in ev["by_document_type"].items():
        w(f"  {k:<20} {b['total']:>3} / {b['legacy']}")
    w("")
    w("=" * 80)
    w("EVALUATION & IMPROVEMENT")
    w("=" * 80)
    w("Success metric — routing precision of the >=0.80 queue: of blobs routed to")
    w("  the candidate deterministic-conversion queue, the share that are genuinely")
    w("  glyph-mapped legacy Nepali rather than English/numeric/Unicode false")
    w("  routes. Proxy for eventual searchable-document yield (SQL-adjacent: every")
    w("  false route is a document that would be corrupted before it is indexed).")
    w("Eval — this frozen 150-file holdout, disjoint from Phase 6A; native-2 run")
    w("  unchanged; each routed blob scored script-independently (false route vs")
    w("  glyph-mapped) plus the input-guard controls. Nepali-reader confirmation of")
    w("  recovered Nepali is the labelled complement, tracked in the review file.")
    w("Feedback capture — the manual-review companion records per-case verdicts and")
    w("  awaiting_nepali_review items; corrections feed a future native-3 cohort,")
    w("  never a retune of native-2 against THIS holdout.")
    w("Review loop — re-run on a fresh holdout whenever the classifier changes")
    w("  (any threshold/rule move forces a new extractor version and a new cohort).")
    w("")
    return "\n".join(out) + "\n"


def _render_review(ev, refs, hold_entries, lexicon, preeti) -> str:
    """Deterministic manual-review evidence bundles (§20/§21). Text, not crops:
    original extraction, converted Unicode, metrics, disposition — the same
    evidence Tasks 1-2 used. A Nepali reader confirms the script-DEPENDENT cases;
    the English/numeric/Unicode ones are marked confirmed here."""
    docs = {d["content_sha256"]: d for d in ev["conversion_documents"] if "error" not in d}

    def _entry(sha):
        r = next(r for r in refs if r.content_sha256 == sha)
        for k in r.comparison_keys:
            if k in hold_entries:
                return hold_entries[k]
        return {}

    def sample(band, n):
        rows = sorted((d for d in docs.values() if d["band"] == band),
                      key=lambda d: d["content_sha256"])
        return rows[:n]

    out: list[str] = []
    w = out.append
    w("=" * 80)
    w("NRB PHASE 6B TASK 3 — MANUAL REVIEW OF THE ROUTING HOLDOUT")
    w("=" * 80)
    w("")
    w("Hand-checkable evidence for the independent holdout. Sampling is")
    w("deterministic (lowest content_sha256 per band). STATUS labels:")
    w("  confirmed_correct     — script-independent (English/numeric/Unicode); verified here")
    w("  confirmed_wrong       — a false route or destroyed control, verified here")
    w("  awaiting_nepali_review — recovered Nepali; needs a competent reader")
    w("  ambiguous / unresolved — the validator could not vouch and neither can I")
    w("")

    def block(title, rows):
        w("-" * 80)
        w(title)
        w("-" * 80)
        if not rows:
            w("  (none in this holdout)")
            w("")
            return
        for d in rows:
            e = _entry(d["content_sha256"])
            if d["routing_verdict"] == "false_route_english":
                status = "confirmed_wrong (English routed as legacy)"
            elif d["routing_verdict"] == "legacy_recovered":
                status = "awaiting_nepali_review (glyph-mapped; converter produced Unicode)"
            elif d["routing_verdict"] == "unknown_or_other_legacy_mapping":
                status = "ambiguous (Devanagari emerged; Preeti did not validate)"
            else:
                status = "unresolved (no usable Unicode; likely unknown encoding or OCR)"
            w(f"  {d['content_sha256'][:12]}  {d['family']}  "
              f"{e.get('document_type','?')}/{e.get('year','?')}")
            w(f"    unit_legacy_ratio={d['unit_legacy_ratio']}  "
              f"legacy_line_ratio={d['legacy_line_ratio']}  "
              f"dev {d['devanagari_before']}->{d['devanagari_after']}  "
              f"english_share={d['english_share']}")
            w(f"    attempted={d['attempted']} accepted={d['accepted']} "
              f"ambiguous={d['ambiguous']} rejected={d['rejected']} "
              f"guarded(en/uni)={d['guarded_english']}/{d['guarded_unicode']}")
            w(f"    recovery={d['recovery']}  verdict={d['routing_verdict']}")
            w(f"    STATUS: {status}")
            w("")

    block("HIGH BAND >=0.80 — candidate conversion queue (up to 10)",
          sample(">=0.80", 10))
    block("MIDDLE BAND 0.50-0.80 (up to 5)", sample("0.50-0.80", 5))
    block("LOW BAND 0.20-0.50 (up to 5)", sample("0.20-0.50", 5))
    block("MINORITY-REGION ROUTED (up to 5)", sample("minority-region-only", 5))

    sheets = sorted((d for d in docs.values() if d["family"] == "spreadsheet"),
                    key=lambda d: d["content_sha256"])[:5]
    block("SPREADSHEET LEGACY CASES (up to 5)", sheets)

    w("-" * 80)
    w("FALSE POSITIVES — flagged units that are readable English (confirmed here)")
    w("-" * 80)
    f = ev["false_positive_class"]
    if not f["detail"]:
        w("  (none)")
    for d in f["detail"]:
        w(f"  {d['content_sha256'][:12]} {d['family']:<11} band={d['band']}  "
          f"unit_ratio={d['unit_legacy_ratio']}  "
          f"{d['legacy_units_english']}/{d['legacy_units']} flagged units are English")
        w("    STATUS: confirmed_wrong (English routed as legacy — script-independent)")
    w(f"  IN THE HIGH BAND (candidate conversion queue): {f['in_high_band']}")
    w("")
    w("-" * 80)
    w("INPUT-GUARD CONTROLS — native-2 clean, ZERO legacy units, must be untouched")
    w("-" * 80)
    for c in ev["input_guard_controls"]:
        bi = c["byte_identical"]
        untouched = (bi is True) or (bi is None and c["converted_units"] == 0)
        status = ("confirmed_correct (converter touched nothing)" if untouched
                  else "confirmed_wrong (converter changed a zero-legacy control)")
        w(f"  {c['content_sha256'][:12]} {c['kind']:<22} "
          f"dev={c['devanagari_ratio']} converted={c['converted_units']}")
        w(f"    STATUS: {status}")
    w("")
    w("-" * 80)
    w("FALSE-NEGATIVE CANDIDATES — native-2 CLEAN docs carrying legacy units (up to 8)")
    w("-" * 80)
    fn = ev["false_negative_scan"]["detail"][:8]
    if not fn:
        w("  (none — every clean document carried zero legacy units)")
    for d in fn:
        # A run of >=3 legacy units in a clean doc is a real missed region; a
        # scattered singleton is likely noise the router was right to ignore.
        kind = ("likely missed legacy region" if d["unit_max_legacy_run"] >= 3
                else "scattered legacy unit(s) — likely noise")
        w(f"  {d['content_sha256'][:12]} {d['family']:<11} "
          f"legacy_units={d['unit_legacy_candidates']} judged={d['unit_judged']} "
          f"max_run={d['unit_max_legacy_run']} converted={d['converted_units']}")
        w(f"    STATUS: awaiting_nepali_review ({kind})")
    w("")
    w("=" * 80)
    w("SUMMARY OF WHAT NEEDS A NEPALI READER")
    w("=" * 80)
    w("Every HIGH/MIDDLE/LOW/MINORITY/SPREADSHEET case marked")
    w("awaiting_nepali_review is a glyph-mapped input the converter turned into")
    w("Unicode; whether that Unicode is CORRECT Nepali is not asserted here. The")
    w("English/numeric/Unicode controls and any false_route_english case are")
    w("script-independent and are labelled confirmed above.")
    w("")
    return "\n".join(out) + "\n"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--holdout", default="docs/nrb/phase6b-routing-holdout.json")
    p.add_argument("--phase6a", default="docs/nrb/phase6a-manifest.json")
    p.add_argument("--lexicon", default="docs/nrb/phase6b-lexicon.json")
    p.add_argument("--out-json", default="docs/nrb/phase6b-routing-holdout-profile.json")
    p.add_argument("--out-profile", default="docs/nrb/phase6b-routing-holdout-profile.txt")
    p.add_argument("--out-review", default="docs/nrb/phase6b-routing-holdout-manual-review.txt")
    return asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
