"""Route legacy-font text to a converter, and refuse the result when it is wrong.

Pure — no DB, no HTTP, no filesystem. The converter arrives as a
`legacy_font.LegacyFontConverter` and the vocabulary as a `lexicon.Lexicon`, so
every rule here is testable with hand-authored strings.

WHY THE ROUTING IS SHAPED LIKE THIS
-----------------------------------
Three measurements from the Phase 6B spike (2026-08-15) dictate the design, and
each one killed a simpler version of it.

**1. Never convert a whole document as one string.** The benchmark contains real
mixed documents — a Preeti Nepali covering note over a genuine English annex —
and that is precisely how the native-1 detector was built (`quality.py`'s 7-of-49
finding). Conversion is per line for text and PER CELL for spreadsheets, because
`extraction.py` joins cells with `" | "` and `|` is itself a Preeti codepoint:
converting a rendered row turns every delimiter into `्र`.

**2. A line the detector could not judge is NOT a clean line.**
`quality.line_looks_glyph_mapped` returns `None` below four tokens, and those
lines are excluded from both halves of `legacy_line_ratio`. Across the 179
`legacy_font_suspected` blobs that is **26,087 of 99,328 non-empty lines (26.3%)**
— the titles, dates, आदेश numbers and table cells. `g]kfn /fi6« a}+s`
(नेपाल राष्ट्र बैंक) is three tokens. A router with two branches leaves every one
of them in Preeti and produces a document that reads as recovered while its
heading is garbage, so `None` inside a legacy document is a CANDIDATE, tracked as
its own disposition.

**3. Producing Devanagari is not succeeding.** The benchmark's known English
table converts to 91% Devanagari with `legacy_line_ratio` 0.2632 → 0.0 and its
character count intact. Every naive success signal fires on a destroyed table. So
there are two guards BEFORE the converter (`devanagari.py`, `lexicon.py`
document why each is necessary and neither is sufficient) and a validation pass
after it, and a rejected line keeps its original text rather than its conversion.

Ambiguity is a first-class outcome. Where several mappings pass validation, all
their scores are recorded and none is chosen — whether the corpus can be resolved
to one mapping is a finding of this evaluation, not an assumption inside it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Sequence

from . import devanagari, lexicon as lexicon_mod, quality
from .legacy_font import ConverterUnavailable, LegacyFontConverter

__all__ = [
    "ACCEPTED",
    "AMBIGUOUS",
    "DISPOSITIONS",
    "DocumentConversion",
    "LineOutcome",
    "MAX_EXPANSION",
    "MIN_DEVANAGARI_AFTER",
    "MIN_SHRINKAGE",
    "UNJUDGED_MIN_LEGACY_RATIO",
    "REJECTED",
    "ValidationOutcome",
    "convert_cells",
    "convert_document",
    "validate_conversion",
]

# --- line dispositions ------------------------------------------------------ #
# A closed vocabulary, for the same reason `quality.REASONS` is one: these are
# counted and reported, and a typo'd disposition would silently vanish from every
# total while still looking like a processed line.
KEPT_CLEAN = "kept_clean"                # detector says this line is not legacy
KEPT_UNICODE = "kept_unicode"            # already real Devanagari — guard 1
KEPT_ENGLISH = "kept_english"            # confidently English — guard 2
KEPT_EMPTY = "kept_empty"                # nothing to convert
CONVERTED = "converted"                  # attempted, validated, replaced
CONVERTED_UNJUDGED = "converted_unjudged"  # ditto, but the detector had no opinion
AMBIGUOUS_LINE = "ambiguous"             # structurally sound, vocabulary can't vouch
AMBIGUOUS_HELD = "ambiguous_held"        # ditto, but the document is not clearly legacy
REJECTED_LINE = "rejected"               # attempted, validation refused, original kept
FAILED_LINE = "failed"                   # the converter itself errored

DISPOSITIONS = (
    KEPT_CLEAN, KEPT_UNICODE, KEPT_ENGLISH, KEPT_EMPTY,
    CONVERTED, CONVERTED_UNJUDGED, AMBIGUOUS_LINE, AMBIGUOUS_HELD,
    REJECTED_LINE, FAILED_LINE,
)
# An ambiguous line passed every structural check and only lacks a dictionary rich
# enough to confirm it. Whether that is good enough to REPLACE the original depends
# on the document, by the same principle that gates unjudged lines: inside a
# document that is overwhelmingly legacy an unconfirmable line is almost certainly
# Nepali, and inside a barely-flagged one it is almost certainly a misflagged data
# row. `ambiguous_held` is the second case — converted, measured, and NOT applied.
#
# Measured: this is what stops `Series No. ST940(kha) ST941(kha)…` and
# `MachhapuchureBank Ltd. 3500.00 300.00` — real rows from the English-table
# controls, whose latin runs are codes and proper nouns the guard's vocabulary
# cannot know — from being rewritten as Devanagari.
#
# Counted apart from `converted` everywhere, so "recovered" never silently absorbs
# "probably recovered" — the distinction §12 of the brief asks for.
_REPLACED = frozenset({CONVERTED, CONVERTED_UNJUDGED, AMBIGUOUS_LINE})

# --- validation outcomes ---------------------------------------------------- #
ACCEPTED = "accepted"
REJECTED = "rejected"
AMBIGUOUS = "ambiguous"

# --- validation thresholds -------------------------------------------------- #
# Deliberately conservative, and NOT tuned to make a mapping look good. Each is
# an obvious-catastrophe bound; the fine discrimination between mappings is left
# to the recorded scores, because the evaluation's job is to find out whether
# that discrimination is possible at all.

MIN_DEVANAGARI_AFTER = 0.30
# A converted legacy line should be substantially Devanagari. Set low because a
# real line is full of digits, punctuation and Rs./% markers that stay latin.

MAX_ILLEGAL_CLUSTER_RATIO = 0.02
# Per Devanagari character. Correct conversions measured 0.0000; the destroyed
# English table measured 0.0667. Two orders of margin, so this is not a knob.

MAX_LATIN_RESIDUE_RATIO = 0.20
# Tokens holding both scripts. Correct conversion: 0.0. English wreckage: 0.75.

MIN_SHRINKAGE = 0.50
MAX_EXPANSION = 2.00
# Non-whitespace characters, after / before. Devanagari legitimately EXPANDS —
# the Preeti workbook went 236,905 → 337,763 characters (+42.6%) because
# combining marks cost a codepoint each — so a naive "length must be preserved"
# rule would reject every correct conversion. These bounds catch a converter that
# emptied a line or exploded it, and nothing subtler.

UNJUDGED_MIN_LEGACY_RATIO = 0.80
# A line the detector could not judge is converted ONLY inside a document that is
# overwhelmingly legacy. The threshold is Phase 6A's own top severity band, where
# §11 says the text is "unusable throughout, not merely doubtful" — not a value
# invented here, and deliberately far above the 0.20 flag.
#
# Measured, and this is why it exists: converting unjudged lines in every flagged
# document destroyed FIVE of the seven English-table negative controls. The lines
# lost were `No.`, `S.No`, `Net`, `ago.`, `Reporting Stats`, `Head Assistants: 5`
# — short table cells that carry too little vocabulary for the English guard to
# veto and too few tokens for the detector to judge. Documents in the 0.20-0.50
# band are exactly the population Phase 6A identified as over-flagged tables, so
# extending the benefit of the doubt to their short lines is backwards.
#
# The cost is stated rather than hidden: an unjudged heading in a 0.20-0.80
# document stays in Preeti and is reported unresolved. That is the conservative
# direction — a missed heading is a gap, a converted English cell is corruption.

MIN_NEPALI_WORDS_TO_JUDGE = 5
MIN_NEPALI_WORD_RATE = 0.05
# Vocabulary is a REJECT rule only where there is enough vocabulary to judge. A
# short heading of proper nouns can legitimately score zero; a paragraph of
# Devanagari with no recognisable Nepali word in it is a wrong mapping.
#
# The rate floor is LOW, and honestly so. The Nepali half of the lexicon is built
# from the only genuine Unicode Devanagari in the benchmark — 6 blobs, 343 words
# — so it recognises common function words and little else. Measured separation
# on a correct-vs-wrong mapping of the same line is 0.125 against 0.100: real,
# repeatable, and far too narrow to decide a mapping on. So vocabulary is used
# here to catch the catastrophe (a wall of Devanagari containing no Nepali at
# all) and contributes to the reported score, and the evaluation reports that a
# richer lexicon is what a future mapping-identification stage would need.


@dataclass(frozen=True)
class ValidationOutcome:
    """Why one conversion was accepted, rejected or left ambiguous."""

    outcome: str
    reasons: tuple[str, ...]
    devanagari_ratio_before: float
    devanagari_ratio_after: float
    illegal_cluster_ratio: float
    latin_residue_ratio: float
    nepali_word_rate: float
    nepali_words_judged: int
    length_ratio: float
    # The single comparable number for "how good is this mapping on this text".
    # Reported, never thresholded — see the module docstring on mapping choice.
    score: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_conversion(
    original: str, converted: str, lexicon: lexicon_mod.Lexicon
) -> ValidationOutcome:
    """Did this conversion recover Nepali, or destroy something?

    Ordered, all reasons collected rather than first-match-wins: a rejected line
    is evidence, and knowing it failed on THREE grounds rather than one is what
    distinguishes a wrong mapping from a marginal one.
    """
    before = devanagari.measure_devanagari(original)
    after = devanagari.measure_devanagari(converted)
    orig_len = sum(1 for ch in original if not ch.isspace())
    conv_len = sum(1 for ch in converted if not ch.isspace())
    length_ratio = round(conv_len / orig_len, 4) if orig_len else 0.0
    rate, judged = lexicon_mod.nepali_word_rate(converted, lexicon)

    reasons: list[str] = []

    # C. catastrophic loss — checked first, because everything below is
    #    meaningless on an empty string.
    if orig_len and conv_len == 0:
        reasons.append("empty_output")
    elif length_ratio < MIN_SHRINKAGE:
        reasons.append("excessive_shrinkage")
    elif length_ratio > MAX_EXPANSION:
        reasons.append("excessive_expansion")

    # A. Devanagari must actually emerge.
    if after.devanagari_ratio < MIN_DEVANAGARI_AFTER:
        reasons.append("no_devanagari_emerged")

    # The two signals that separate recovery from vandalism.
    if after.illegal_cluster_ratio > MAX_ILLEGAL_CLUSTER_RATIO:
        reasons.append("illegal_devanagari_clusters")
    if after.latin_residue_ratio > MAX_LATIN_RESIDUE_RATIO:
        reasons.append("latin_residue")

    # STRUCTURE REJECTS; VOCABULARY ONLY CONFIRMS. The asymmetry is measured, not
    # stylistic. Used as a veto, the vocabulary rule threw out correct Nepali:
    # `सञ्चालक समितिले देहायका विनियमहरु बनाएको छ ।` is a flawless conversion and
    # scores zero, because none of those words are among the 343 the lexicon
    # learned from six documents. Genuine Preeti lines with >=8 Devanagari words
    # score 0.0 about 10% of the time (13/140 and 31/248 on two real circulars),
    # while the destroyed English table scores 0.0 on 5 of 5. The signal is real
    # and it is far too coarse to reject on — so a line vocabulary cannot vouch
    # for is AMBIGUOUS, never rejected, and the report counts it apart from a
    # confirmed conversion.
    if reasons:
        outcome = REJECTED
    elif judged >= MIN_NEPALI_WORDS_TO_JUDGE and rate >= MIN_NEPALI_WORD_RATE:
        outcome = ACCEPTED
    else:
        outcome = AMBIGUOUS
        reasons.append(
            "low_nepali_word_rate"
            if judged >= MIN_NEPALI_WORDS_TO_JUDGE
            else "insufficient_vocabulary_to_confirm"
        )

    # A blend, for ranking mappings against each other on the SAME text. It has
    # no threshold and decides nothing on its own.
    score = round(
        after.devanagari_ratio
        + rate
        - 4.0 * after.illegal_cluster_ratio
        - 2.0 * after.latin_residue_ratio,
        4,
    )

    return ValidationOutcome(
        outcome=outcome,
        reasons=tuple(reasons),
        devanagari_ratio_before=before.devanagari_ratio,
        devanagari_ratio_after=after.devanagari_ratio,
        illegal_cluster_ratio=after.illegal_cluster_ratio,
        latin_residue_ratio=after.latin_residue_ratio,
        nepali_word_rate=rate,
        nepali_words_judged=judged,
        length_ratio=length_ratio,
        score=score,
    )


@dataclass(frozen=True)
class LineOutcome:
    """One line (or one cell) and what happened to it."""

    index: int
    disposition: str
    original: str
    converted: str | None
    validation: ValidationOutcome | None
    error: str | None = None
    # The line terminator as it appeared in the source (`\n`, `\r\n`, or `""` on
    # a final line with none). Carried so reconstruction is byte-exact rather
    # than merely line-exact — see `DocumentConversion.text`.
    ending: str = ""

    @property
    def text(self) -> str:
        """What belongs in the reconstructed document.

        A rejected or guarded line yields its ORIGINAL, unchanged. That is the
        preservation guarantee the mixed-document test asserts on, and it is a
        property of this one accessor rather than of caller discipline.
        """
        if self.disposition in _REPLACED and self.converted is not None:
            return self.converted
        return self.original


@dataclass(frozen=True)
class DocumentConversion:
    """One document converted under ONE mapping, and everything that decided it."""

    converter_name: str
    mapping: str
    converter_version: str
    lexicon_fingerprint: str
    lines: tuple[LineOutcome, ...]
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def text(self) -> str:
        """The reconstructed document.

        Each line carries its own original terminator, so a document in which
        nothing was converted reconstructs **byte-identically** — including CRLF
        and a missing final newline. `"\\n".join(splitlines())` does not: it
        silently normalises line endings and drops a trailing newline, which
        would make the English-table and Unicode negative controls report a diff
        they did not cause and quietly weaken the preservation guarantee those
        controls exist to prove.
        """
        return "".join(line.text + line.ending for line in self.lines)

    @property
    def converted_lines(self) -> int:
        return sum(1 for line in self.lines if line.disposition in _REPLACED)

    def unchanged(self) -> tuple[LineOutcome, ...]:
        return tuple(line for line in self.lines if line.disposition not in _REPLACED)


def _latin_share(line: str) -> float:
    """Latin letters as a share of non-whitespace characters.

    The same quantity `quality._line_looks_glyph_mapped` computes for its
    `LEGACY_MIN_LATIN` gate, recomputed here rather than exported, because it is
    two lines and the alternative is widening `quality`'s public surface for an
    intermediate value.
    """
    non_ws = sum(1 for ch in line if not ch.isspace())
    if not non_ws:
        return 0.0
    return sum(1 for ch in line if ch.isascii() and ch.isalpha()) / non_ws


def _classify_line(
    line: str, lexicon: lexicon_mod.Lexicon, *, document_legacy_ratio: float
) -> tuple[str, bool]:
    """`(disposition-if-not-converted, is a conversion candidate)`.

    Guard order is load-bearing. The Unicode guard runs FIRST because the
    converter is not a no-op on correct Devanagari — measured, it turns
    `(मनी लाउन्डररङ)` into `९मनी लाउन्डररङ०` and `मन्त्रिपररषद्` into
    `मन्त्रपिररषद्` while RAISING `devanagari_ratio` from 0.9616 to 0.9936. Nothing
    downstream would catch that, so it must never be attempted.
    """
    if not line.strip():
        return KEPT_EMPTY, False

    # Guard 1 — already real Devanagari. Same threshold the detector itself uses
    # to exempt a line, so the two cannot disagree about what "is Devanagari".
    shape = devanagari.measure_devanagari(line)
    if shape.devanagari_ratio > quality.LEGACY_MAX_DEVANAGARI:
        return KEPT_UNICODE, False

    # Guard 2 — confidently English. On the RAW text; see `is_confidently_english`
    # for why judging the output instead cannot work.
    if lexicon_mod.is_confidently_english(line, lexicon):
        return KEPT_ENGLISH, False

    verdict = quality.line_looks_glyph_mapped(line)
    if verdict is True:
        return CONVERTED, True
    if verdict is None:
        # Too short for the detector to have an opinion. Inside a legacy document
        # that is a heading, not a clean line — see the module docstring.
        #
        # But it must still BE latin text. A short line the detector skipped
        # includes every numeric table cell, and Preeti maps ASCII digits to
        # Devanagari digits: `1,234.00` converts to `ज्ञ,द्दघद्ध।ण्ण्`, which has a
        # high Devanagari ratio, no illegal clusters and no latin residue — it
        # passes every validation rule while destroying a number. The share
        # required is `quality.LEGACY_MIN_LATIN`, the detector's OWN condition
        # for "this is latin text, not a numeric row", so the unjudged branch is
        # held to the same standard as the judged one rather than to a new
        # threshold invented here.
        if (
            document_legacy_ratio >= UNJUDGED_MIN_LEGACY_RATIO
            and _latin_share(line) >= quality.LEGACY_MIN_LATIN
        ):
            return CONVERTED_UNJUDGED, True
        return KEPT_CLEAN, False
    return KEPT_CLEAN, False


def _convert_units(
    units: Sequence[str],
    converter: LegacyFontConverter,
    lexicon: lexicon_mod.Lexicon,
    *,
    document_legacy_ratio: float,
    endings: Sequence[str] | None = None,
) -> tuple[LineOutcome, ...]:
    """The shared engine. A "unit" is a line for text, a cell for a spreadsheet."""
    outcomes: list[LineOutcome] = []
    endings = endings or [""] * len(units)
    for index, unit in enumerate(units):
        ending = endings[index]
        disposition, is_candidate = _classify_line(
            unit, lexicon, document_legacy_ratio=document_legacy_ratio
        )
        if not is_candidate:
            outcomes.append(
                LineOutcome(index, disposition, unit, None, None, ending=ending)
            )
            continue
        try:
            converted = converter.convert(unit)
        except ConverterUnavailable as exc:
            # Explicit, per unit, and the original survives. A converter that
            # failed must never be indistinguishable from one that no-oped.
            outcomes.append(
                LineOutcome(
                    index, FAILED_LINE, unit, None, None,
                    error=str(exc), ending=ending,
                )
            )
            continue
        validation = validate_conversion(unit, converted, lexicon)
        if validation.outcome == ACCEPTED:
            final = disposition            # `converted` or `converted_unjudged`
        elif validation.outcome == AMBIGUOUS:
            final = (
                AMBIGUOUS_LINE
                if document_legacy_ratio >= UNJUDGED_MIN_LEGACY_RATIO
                else AMBIGUOUS_HELD
            )
        else:
            final = REJECTED_LINE
        outcomes.append(
            LineOutcome(index, final, unit, converted, validation, ending=ending)
        )
    return tuple(outcomes)


def _counts(outcomes: Sequence[LineOutcome]) -> dict[str, int]:
    """Every disposition, always all of them, in `DISPOSITIONS` order.

    Zeros included deliberately: a report that omits empty buckets makes "no
    English lines were guarded" and "the guard never ran" look identical.
    """
    tally = {d: 0 for d in DISPOSITIONS}
    for outcome in outcomes:
        tally[outcome.disposition] += 1
    return tally


def convert_document(
    text: str,
    converter: LegacyFontConverter,
    lexicon: lexicon_mod.Lexicon,
    *,
    document_legacy_ratio: float = 1.0,
) -> DocumentConversion:
    """Convert one extracted TEXT document, line by line.

    The unit is a line WITHOUT its terminator; the terminator is carried
    alongside and re-attached on reconstruction. So line count and order are
    preserved exactly, a PDF's `[page N]` markers and blank lines survive as
    themselves, and a document in which nothing was converted comes back
    byte-identical — which is what the negative controls assert.
    """
    bodies: list[str] = []
    endings: list[str] = []
    for raw in text.splitlines(keepends=True):
        stripped = raw.rstrip("\r\n")
        bodies.append(stripped)
        endings.append(raw[len(stripped):])
    outcomes = _convert_units(
        bodies, converter, lexicon,
        document_legacy_ratio=document_legacy_ratio, endings=endings,
    )
    return DocumentConversion(
        converter_name=converter.name,
        mapping=converter.mapping,
        converter_version=converter.version,
        lexicon_fingerprint=lexicon.fingerprint,
        lines=outcomes,
        counts=_counts(outcomes),
    )


def convert_cells(
    rows: Sequence[Sequence[str]],
    converter: LegacyFontConverter,
    lexicon: lexicon_mod.Lexicon,
    *,
    document_legacy_ratio: float = 1.0,
) -> tuple[DocumentConversion, tuple[tuple[str, ...], ...]]:
    """Convert a SPREADSHEET cell by cell, and rebuild the grid.

    Not `convert_document` over rendered rows, and the difference is not
    cosmetic: `extraction.py` renders a row as `" | ".join(cells)`, and `|` is a
    Preeti codepoint that maps to `्र`. Feeding rendered rows to a converter turns
    every column separator into a Devanagari conjunct — measured on
    `8df7b02f8a13`, the benchmark's Preeti-encoded workbook.

    Returns the flat per-cell record AND the reconstructed grid, because the
    evidence needs the first and any future consumer needs the second.
    """
    flat = [cell for row in rows for cell in row]
    outcomes = _convert_units(
        flat, converter, lexicon, document_legacy_ratio=document_legacy_ratio
    )
    grid: list[tuple[str, ...]] = []
    cursor = 0
    for row in rows:
        grid.append(tuple(outcomes[cursor + i].text for i in range(len(row))))
        cursor += len(row)
    conversion = DocumentConversion(
        converter_name=converter.name,
        mapping=converter.mapping,
        converter_version=converter.version,
        lexicon_fingerprint=lexicon.fingerprint,
        lines=outcomes,
        counts=_counts(outcomes),
    )
    return conversion, tuple(grid)
