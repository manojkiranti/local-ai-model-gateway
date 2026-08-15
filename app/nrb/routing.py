"""native-2: the routing classifier. Pure — no I/O, no converter, no model.

Native-1 asks "did extraction produce trustworthy text". Native-2 asks the same
question with two corrections and one addition, all of them forced by the frozen
400-file benchmark. It **classifies only**: it converts nothing, imports nothing
from `legacy_font`, and must work on a machine where npttf2utf was never
installed.

WHAT CHANGED, AND WHY EACH CHANGE EXISTS
----------------------------------------
**1. Tables are no longer mistaken for Nepali.** `05fa82badf94` is a completely
readable English statistics table that native-1 calls `legacy_font_suspected`,
and the Docling calibration found six more of exactly that shape. The cause was
measured, not guessed: over 355 flagged lines in those seven documents the
intra-word-symbol rule fired on **89.3%** while the vowel-less rule fired on 2.5%
— `2,123,180.00` and `FIU-Nepal` are not glyph-mapped words. `units.py` fixes the
signal itself; nothing here re-tunes a threshold.

**2. Spreadsheets are read.** `quality.classify` returns on structure alone for a
workbook — are there cells? — so `8df7b02f8a13`, whose cells are Preeti Nepali,
is `extracted`/`clean`, and so were all 44 parsed benchmark spreadsheets.
Native-2 judges a workbook's **cells**, never the `" | "`-joined row that
`extraction.py` renders for storage: `|` is itself a Preeti codepoint. A valid
workbook structure and trustworthy cell text are two different claims.

**3. A legacy minority is no longer diluted away.** `84862ab6866a` holds genuine
Unicode Devanagari *and* about 29 lines of genuine Preeti; the Unicode majority
drags its document-wide ratio to 0.0444 and native-1 passes it as clean. **The
answer is not to lower 0.20** — that would trade this false negative for a flood
of false positives and destroy the meaning of the existing severity evidence.
Instead native-2 adds a *region* signal: enough confidently-legacy units, in a
contiguous run, among the units that could plausibly be glyph-mapped at all.

WHAT DELIBERATELY DID NOT CHANGE
--------------------------------
`legacy_line_ratio >= 0.20` still means what it meant, is still computed the
native-1 way by `quality.measure_text`, and is still reported — Phase 6B Task 1's
severity evidence is stated in those terms and must stay readable. Native-2 adds
signals beside it rather than redefining it. The status/reason vocabulary is
unchanged: detected legacy encoding is still `suspicious`/`legacy_font_suspected`,
because native-2 detects a legacy CANDIDATE and never identifies a font mapping.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from . import quality, units

__all__ = [
    "EXTRACTOR_VERSION_V2",
    "MINORITY_MIN_CONTESTED_RATIO",
    "MINORITY_MIN_LEGACY_UNITS",
    "MIN_JUDGED_FOR_RATIO",
    "MIN_LEGACY_ABSOLUTE",
    "MINORITY_MIN_RUN",
    "RoutingEvidence",
    "classify_v2",
    "minority_legacy_detected",
    "unit_metrics",
]

EXTRACTOR_VERSION_V2 = "native-2"

# --- the minority-region rule ----------------------------------------------- #
# All three must hold. Each one alone produces nonsense on this corpus:
#
#   * a COUNT alone flags any long document with a few odd lines;
#   * a RUN alone flags a stray table fragment;
#   * a RATIO alone is exactly the global measure that already missed the case.
#
# Together they say: "there is a contiguous region of real size, and it is a
# meaningful share of the text that could plausibly be glyph-mapped". The
# contested denominator is the load-bearing part — it excludes Unicode and
# positively-English units, so a Preeti section inside a Unicode document is
# measured against the *other latin text*, not against the whole document.
MINORITY_MIN_LEGACY_UNITS = 10
MINORITY_MIN_RUN = 3
MINORITY_MIN_CONTESTED_RATIO = 0.50

# --- the small-denominator floor -------------------------------------------- #
# A ratio over four judged units is not a measurement. Marking uninformative
# units `unjudged` (which is right) shrinks the denominator, and on the benchmark
# that turned six documents suspicious on the strength of ONE legacy unit out of
# three or four judged — `5504d7f5eb74`, `e7d44c7f04c0`, `917712da7765`,
# `834f70add6e7`, `333cad7d028a`, `8a57093ed457`. Native-1 never saw this because
# its denominator included every numeric row.
#
# So the ratio rule needs either a real denominator OR enough legacy units to
# speak for themselves. The absolute escape hatch matters: a short document that
# is ENTIRELY Preeti has few units and must still flag — `0503f02d7d8c` is 15
# judged units, all legacy.
MIN_JUDGED_FOR_RATIO = 8
MIN_LEGACY_ABSOLUTE = 4


@dataclass(frozen=True)
class RoutingEvidence:
    """Everything native-2 may look at: native-1's evidence plus assessed units.

    Composition rather than a subclass, so `quality.Evidence` keeps exactly the
    fields native-1 was measured with and cannot acquire a native-2 concern.
    """

    base: quality.Evidence
    units: tuple[units.UnitAssessment, ...]
    profile: units.UnitProfile

    @classmethod
    def build(
        cls, base: quality.Evidence, unit_texts: Sequence[str]
    ) -> "RoutingEvidence":
        assessed = tuple(units.assess_unit(u) for u in unit_texts)
        return cls(base=base, units=assessed, profile=units.profile_units(assessed))


def minority_legacy_detected(profile: units.UnitProfile) -> bool:
    """A legacy REGION inside a document whose global ratio looks innocent.

    Deliberately conservative on all three axes — see the threshold comments. The
    regression case is `84862ab6866a`: Unicode-majority, `legacy_line_ratio`
    0.0444, and a real Preeti section inside it.
    """
    return (
        profile.legacy >= MINORITY_MIN_LEGACY_UNITS
        and profile.max_legacy_run >= MINORITY_MIN_RUN
        and profile.contested_legacy_ratio >= MINORITY_MIN_CONTESTED_RATIO
    )


def _legacy_by_units(profile: units.UnitProfile) -> bool:
    """The document-level legacy call, over the three-state denominator.

    Same 0.20 share native-1 uses, but of *judged* units — a numeric row or a
    blank line is now `unjudged` and sits in neither half, instead of counting as
    evidence of cleanliness. The threshold itself is untouched; what changed is
    that the denominator no longer contains units that mean nothing.

    Guarded by the small-denominator floor, because that same improvement makes a
    ratio computable over three units. See `MIN_JUDGED_FOR_RATIO`.
    """
    if profile.legacy_unit_ratio <= quality.LEGACY_LINE_RATIO:
        return False
    return (
        profile.judged >= MIN_JUDGED_FOR_RATIO
        or profile.legacy >= MIN_LEGACY_ABSOLUTE
    )


def unit_metrics(profile: units.UnitProfile) -> dict[str, Any]:
    """Native-2's added metrics, for the `metrics` JSONB.

    Namespaced `unit_*` / `minority_*` so a reader can tell at a glance which
    numbers are native-1's and which are new, and so the native-1 keys keep their
    exact meaning in a side-by-side comparison. No column, no migration: these
    explain routing and are queried occasionally, which is what JSONB is for.
    """
    return {
        "unit_total": profile.units,
        "unit_judged": profile.judged,
        "unit_legacy_candidates": profile.legacy,
        "unit_trusted": profile.trusted,
        "unit_unjudged": profile.unjudged,
        "unit_english": profile.english_units,
        "unit_unicode": profile.unicode_units,
        "unit_numeric": profile.numeric_units,
        "unit_legacy_ratio": profile.legacy_unit_ratio,
        "unit_contested_legacy_ratio": profile.contested_legacy_ratio,
        "unit_max_legacy_run": profile.max_legacy_run,
        "minority_legacy_detected": int(minority_legacy_detected(profile)),
    }


def classify_v2(evidence: RoutingEvidence) -> quality.Verdict:
    """The native-2 status of one extraction. Deterministic, ordered.

    Rules 1-3 and 5 are native-1's, unchanged and in the same order — a failed
    parse, an unsupported family, an image and a PDF's page structure are not what
    this version is correcting, and re-deciding them would make the comparison
    between versions unreadable.

    Ties still break toward `suspicious`. A wrong document that parses cleanly is
    the failure the whole phase exists to prevent.
    """
    base = evidence.base
    profile = evidence.profile
    warnings: list[str] = []

    # 1-3. Unchanged from native-1.
    if base.error is not None:
        return quality.Verdict(quality.STATUS_FAILED, "parser_error")
    if base.family in quality.UNSUPPORTED_FAMILIES:
        return quality.Verdict(quality.STATUS_UNSUPPORTED, "no_native_parser")
    if base.family == "image":
        return quality.Verdict(quality.STATUS_NEEDS_OCR, "image_file")
    if not base.parsed or base.text_metrics is None:
        return quality.Verdict(quality.STATUS_FAILED, "parser_error")

    metrics = base.text_metrics

    # 4. Spreadsheets. Structure is still checked FIRST — an empty workbook is an
    #    empty workbook — but it no longer ENDS the classification. The cells then
    #    go through the same linguistic rules as any other text, which is the
    #    whole point of this version.
    if base.sheets is not None:
        if base.sheets.non_empty_cells == 0:
            return quality.Verdict(quality.STATUS_SUSPICIOUS, "empty_spreadsheet")
        if _legacy_by_units(profile) or minority_legacy_detected(profile):
            return quality.Verdict(
                quality.STATUS_SUSPICIOUS, "legacy_font_suspected", ()
            )
        return quality.Verdict(quality.STATUS_EXTRACTED, "clean", ())

    if metrics.token_count < quality.LEGACY_MIN_TOKENS:
        warnings.append("insufficient_text")

    # 5. PDF structure — unchanged.
    if base.pages is not None and base.pages.page_count > 0:
        if base.pages.text_page_coverage < quality.COVERAGE_NEEDS_OCR:
            return quality.Verdict(
                quality.STATUS_NEEDS_OCR, "no_text_layer", tuple(warnings)
            )
        if base.pages.median_chars_per_text_page < quality.MIN_CHARS_PER_PAGE:
            return quality.Verdict(
                quality.STATUS_NEEDS_OCR, "sparse_text_layer", tuple(warnings)
            )

    # 6. Suspicion, in severity order. The legacy test is the only rule whose
    #    substance differs from native-1, and it now has two ways to fire.
    if _legacy_by_units(profile):
        return quality.Verdict(
            quality.STATUS_SUSPICIOUS, "legacy_font_suspected", tuple(warnings)
        )
    if minority_legacy_detected(profile):
        # A real legacy REGION in a document whose global ratio is innocent. The
        # warning names why, because a reader looking at a 0.04 ratio and a
        # `suspicious` verdict would otherwise think the classifier had misfired.
        return quality.Verdict(
            quality.STATUS_SUSPICIOUS,
            "legacy_font_suspected",
            tuple(warnings) + ("minority_legacy_region",),
        )
    if metrics.replacement_char_ratio > quality.MAX_REPLACEMENT_RATIO:
        return quality.Verdict(
            quality.STATUS_SUSPICIOUS, "replacement_characters", tuple(warnings)
        )
    if metrics.control_char_ratio > quality.MAX_CONTROL_RATIO:
        return quality.Verdict(
            quality.STATUS_SUSPICIOUS, "control_characters", tuple(warnings)
        )
    if metrics.printable_ratio < quality.MIN_PRINTABLE_RATIO:
        return quality.Verdict(
            quality.STATUS_SUSPICIOUS, "low_printable_ratio", tuple(warnings)
        )
    if base.pages is not None and base.pages.page_count > 0:
        coverage = base.pages.text_page_coverage
        if coverage < quality.COVERAGE_SUSPICIOUS:
            return quality.Verdict(
                quality.STATUS_SUSPICIOUS, "partial_text_coverage", tuple(warnings)
            )
        if coverage < quality.COVERAGE_WARN:
            warnings.append("partial_text_coverage")

    if metrics.non_whitespace_chars == 0:
        return quality.Verdict(
            quality.STATUS_NEEDS_OCR, "no_text_layer", tuple(warnings)
        )

    return quality.Verdict(quality.STATUS_EXTRACTED, "clean", tuple(warnings))
