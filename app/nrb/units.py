"""Judgment units: what native-2 actually scores for legacy-font evidence. Pure.

Native-1 measures a document's LINES. That is right for prose and wrong twice,
and the frozen benchmark measured both failures:

  * **A spreadsheet has no lines.** `extraction.py` renders a row as
    `" | ".join(cells)` purely so there is something to measure, and `quality.
    classify` then skips the linguistic rules for workbooks entirely — so
    `8df7b02f8a13`, whose cells are Preeti Nepali, is `extracted`/`clean`. The
    separator is not a neutral join either: `|` is a Preeti codepoint that maps to
    `्र`, so a rendered row is not safe to score OR to convert. **Cells are the
    unit.**
  * **A line is not always a judgeable thing.** Native-1's detector answers
    `True` / `False` / `None`, but `legacy_line_ratio` folds `None` out of both
    halves and treats everything else as a verdict. A numeric table row and a
    paragraph of English are both "not legacy", and lumping them together is what
    lets 29 Preeti lines hide inside `84862ab6866a`'s Unicode majority.

So this module separates **raw extracted text** from **units used for legacy
judgment**, and gives every unit one of three states rather than two:

    legacy_candidate    glyph-mapped shape; this needs recovery
    trusted_nonlegacy   positively identified as fine (English, Unicode Nepali)
    unjudged            carries no linguistic evidence either way

`unjudged` is the state native-1 lacked a name for. A numeric cell, a row of
dashes, a page number and a two-word heading are not evidence of cleanliness, and
counting them as such is exactly how a legacy minority gets diluted below a
threshold.

WHAT IS *NOT* CHANGED HERE
--------------------------
The shape signals are native-1's own — vowel-less tokens, intra-word symbols,
intra-word case switches, at the same thresholds. This is not a second,
independently-invented heuristic; §11 of `docs/nrb-integration.md` is explicit
that a competing detector would be free to disagree with the committed native-1
measurements. **No threshold is moved.** Three corrections are applied to the
SIGNALS, each traced to a specific false flag on the seven known English tables:

  1. **Symbols count only in tokens that contain letters.** `2,123,180.00` and
     `3500.00` are formatted numbers, not glyph-mapped words, and the symbol rule
     was 89.3% of the false flags on those seven documents (against 2.5% for the
     vowel-less rule and 10.4% for case switches — measured over 355 flagged
     lines).
  2. **A well-formed compound is not a glyph-mapped token.** `FIU-Nepal`,
     `AML/CFT`, `F/Y` split on their symbol into letter runs that are each
     pronounceable, an acronym, or a single capital. `q_fie(` and `4{i-4;f` do
     not — their fragments are lower-case single letters.
  3. **Acronyms are not judged on vowels.** `NRB`, `SLF`, `IRC` have no vowels
     because that is what an acronym is; `iv. NRB Bond - - -` scored 0.50 and
     `ii. SLF -` scored 1.00 against a 0.30 threshold. All-caps tokens leave the
     vowel test's numerator AND its denominator.

None of the three can shelter Preeti, because glyph-mapped text is relentlessly
MIXED case and vowel-poor across whole lines, not in isolated acronyms.

Measured effect: all **seven** known English tables fall from 0.2121-0.5787 to
**0.0000-0.1242** — every one below the 0.20 flag — while **nine** genuine legacy
documents hold at 0.9556-1.0000.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Sequence

from . import quality

__all__ = [
    "KINDS",
    "STATES",
    "STATE_LEGACY",
    "STATE_TRUSTED",
    "STATE_UNJUDGED",
    "UnitAssessment",
    "UnitProfile",
    "assess_unit",
    "cells_from_rows",
    "is_word_like_compound",
    "profile_units",
    "units_from_text",
]

_LETTER = re.compile(r"[A-Za-z]")
_LETTER_RUN = re.compile(r"[A-Za-z]+")

# --- the three states ------------------------------------------------------- #
STATE_LEGACY = "legacy_candidate"      # glyph-mapped shape; needs recovery
STATE_TRUSTED = "trusted_nonlegacy"    # positively identified as fine
STATE_UNJUDGED = "unjudged"            # no linguistic evidence either way
STATES = (STATE_LEGACY, STATE_TRUSTED, STATE_UNJUDGED)

# --- why, for each state. A closed vocabulary, like `quality.REASONS`: these are
# counted and reported, and a typo'd kind would vanish from every total while
# still looking like an assessed unit.
KIND_EMPTY = "empty"                     # unjudged
KIND_TOO_SHORT = "too_short"             # unjudged
KIND_NUMERIC = "numeric_table_like"      # unjudged
KIND_UNICODE = "unicode_devanagari"      # trusted
KIND_ENGLISH = "english_like"            # trusted
KIND_CLEAN_LATIN = "clean_latin"         # trusted
KIND_LEGACY_SHAPE = "legacy_shape"       # legacy
KINDS = (
    KIND_EMPTY, KIND_TOO_SHORT, KIND_NUMERIC, KIND_UNICODE,
    KIND_ENGLISH, KIND_CLEAN_LATIN, KIND_LEGACY_SHAPE,
)

# --- thresholds ------------------------------------------------------------- #
# Everything native-1 uses is imported from `quality` rather than restated, so
# the two versions cannot drift apart on a value one of them was calibrated with.

# `english_like` is a POSITIVE identification and needs convincing structure, not
# a lucky token. Phase 6A already proved the cheap version unsafe: a document-level
# `stopword_rate` gate missed 7 of 49 real circulars because glyph-mapped text is
# full of one- and two-character ASCII tokens that match short English stopwords by
# chance, one scoring 0.248 — higher than real English prose. So this reads
# ORTHOGRAPHY, not a word list: enough real words, essentially all of them
# containing a vowel, no mid-word case switching. Preeti runs 0.43-0.54 vowel-less
# and 0.40-0.60 case-switching; it cannot reach these numbers by accident.
ENGLISH_MIN_ALPHA_TOKENS = 3
ENGLISH_MAX_VOWELLESS = 0.10
ENGLISH_MAX_CASE_SWITCH = 0.0

# A unit that is mostly digits and punctuation carries no linguistic evidence.
# Marked `unjudged` rather than `trusted`, because "this row is numbers" is not a
# statement that the document's Nepali is fine.
NUMERIC_MIN_DIGIT_RATIO = 0.30
NUMERIC_MAX_ALPHA_TOKENS = 2


@dataclass(frozen=True)
class UnitAssessment:
    """One judgment unit — a line for text, a cell for a spreadsheet."""

    state: str
    kind: str
    tokens: int
    alpha_tokens: int
    vowelless_ratio: float
    symbol_ratio: float
    case_switch_ratio: float
    devanagari_ratio: float
    latin_ratio: float
    digit_ratio: float

    @property
    def is_legacy(self) -> bool:
        return self.state == STATE_LEGACY

    @property
    def is_judged(self) -> bool:
        return self.state != STATE_UNJUDGED


def is_word_like_compound(token: str) -> bool:
    """`FIU-Nepal`, `AML/CFT`, `F/Y`, `well-known` — a symbol joining real words.

    Two or more letter runs, each of them a pronounceable word (vowel-bearing), an
    acronym (all-caps, two or more letters), or a single CAPITAL — the last covers
    initialisms like `F/Y`, `A/C`, `M/S`, which are ordinary in NRB's English
    tables and were the third and last cause of a false flag on them.

    A single capital counts only inside a multi-run token, and only when it is
    upper case. That is what separates `F/Y` from glyph-mapped `q_fie(` and
    `4{i-4;f`, whose fragments are lower-case single letters.
    """
    runs = _LETTER_RUN.findall(token)
    if len(runs) < 2:
        return False
    return all(
        quality._VOWEL.search(r) or (r.isupper() and len(r) >= 2)
        or (len(r) == 1 and r.isupper())
        for r in runs
    )


def _symbol_hit(token: str) -> bool:
    """Native-1's intra-word-symbol test, minus its two measured false positives.

    The token must contain a letter at all — a formatted number is not a
    glyph-mapped word — and must not be a well-formed compound.
    """
    if not _LETTER.search(token):
        return False
    if is_word_like_compound(token):
        return False
    return quality._is_intraword_symbol(token)


def assess_unit(text: str) -> UnitAssessment:
    """Score ONE unit. Never raises; ordered, first match wins.

    The order is the argument. Unicode is checked before anything else because
    real Devanagari settles the question outright. English is checked before the
    shape rules because a positive identification of readable English should not
    then be second-guessed by a heuristic built to recognise its opposite.
    """
    tokens = quality._TOKEN.findall(text)
    non_ws = [c for c in text if not c.isspace()]
    total = len(non_ws)

    def _ratio(part: int, whole: int) -> float:
        return round(part / whole, 4) if whole else 0.0

    dev = _ratio(len(quality._DEVANAGARI.findall(text)), total)
    latin = _ratio(len(quality._LATIN_LETTER.findall(text)), total)
    digits = _ratio(sum(1 for c in non_ws if c.isdigit()), total)

    alpha = [t for t in tokens if quality._ALPHA_TOKEN.match(t) and len(t) >= 3]
    # Acronyms are excluded from the vowel test entirely — not counted as
    # vowel-less, and not counted in its denominator either. `NRB`, `SLF`, `IRC`
    # and `TOTAL` have no vowels because that is what an acronym IS, and on the
    # benchmark's English tables `iv. NRB Bond - - -` scored 0.50 and `ii. SLF -`
    # scored 1.00 against a 0.30 threshold. Judging them on vowels measures
    # capitalisation, not encoding.
    #
    # This does not open a hole for Preeti: glyph-mapped text is relentlessly
    # MIXED case (`ljQLo`, `k|fKt`, `OGgf]e]l6e`) — mid-token case switching is
    # itself one of the three signals — so an all-caps run is not its shape, and
    # the symbol and case-switch rules are untouched either way.
    vowel_judged = [t for t in alpha if not t.isupper()]
    wordish = [t for t in tokens if _LETTER.search(t)]
    vowelless = _ratio(
        sum(1 for t in vowel_judged if not quality._VOWEL.search(t)),
        len(vowel_judged),
    )
    symbols = _ratio(sum(1 for t in wordish if _symbol_hit(t)), len(wordish))
    switches = _ratio(
        sum(1 for t in wordish if quality._is_intraword_case_switch(t)), len(wordish)
    )

    def _made(state: str, kind: str) -> UnitAssessment:
        return UnitAssessment(
            state=state, kind=kind, tokens=len(tokens), alpha_tokens=len(alpha),
            vowelless_ratio=vowelless, symbol_ratio=symbols,
            case_switch_ratio=switches, devanagari_ratio=dev,
            latin_ratio=latin, digit_ratio=digits,
        )

    if not total:
        return _made(STATE_UNJUDGED, KIND_EMPTY)

    # Real Devanagari settles it. Same threshold native-1 exempts a line with.
    if dev > quality.LEGACY_MAX_DEVANAGARI:
        return _made(STATE_TRUSTED, KIND_UNICODE)

    if len(tokens) < quality.LEGACY_LINE_MIN_TOKENS:
        return _made(STATE_UNJUDGED, KIND_TOO_SHORT)

    # Mostly digits with almost no words: a table row or a numeric cell. Native-1
    # called this `False` (clean) via its latin-share gate and kept it in the
    # denominator; native-2 calls it unjudged, because it is evidence of nothing.
    if digits >= NUMERIC_MIN_DIGIT_RATIO and len(alpha) <= NUMERIC_MAX_ALPHA_TOKENS:
        return _made(STATE_UNJUDGED, KIND_NUMERIC)

    if latin < quality.LEGACY_MIN_LATIN:
        return _made(STATE_UNJUDGED, KIND_NUMERIC)

    # Positively English: enough real words, essentially all vowel-bearing, no
    # mid-word case switching. Orthography, never a stopword list — see the
    # threshold comments.
    if (
        len(alpha) >= ENGLISH_MIN_ALPHA_TOKENS
        and vowelless <= ENGLISH_MAX_VOWELLESS
        and switches <= ENGLISH_MAX_CASE_SWITCH
        and symbols <= quality.LEGACY_MIN_INTRAWORD_SYMBOL
    ):
        return _made(STATE_TRUSTED, KIND_ENGLISH)

    if (
        vowelless > quality.LEGACY_MIN_VOWELLESS
        or symbols > quality.LEGACY_MIN_INTRAWORD_SYMBOL
        or switches > quality.LEGACY_MIN_CASE_SWITCH
    ):
        return _made(STATE_LEGACY, KIND_LEGACY_SHAPE)

    return _made(STATE_TRUSTED, KIND_CLEAN_LATIN)


def units_from_text(text: str) -> tuple[str, ...]:
    """Judgment units for prose and PDFs: the lines, as they are."""
    return tuple(text.splitlines())


def cells_from_rows(rows: Sequence[Sequence[object]]) -> tuple[str, ...]:
    """Judgment units for a spreadsheet: individual cells.

    **Never the rendered row.** `extraction.py` joins cells with `" | "` to have
    something to store as text, and `|` is a Preeti codepoint that maps to `्र` —
    so a rendered row is unsafe to score and unsafe to convert. Cell identity is
    preserved here so the future converter can work per cell too.
    """
    return tuple(
        str(cell) for row in rows for cell in row if str(cell).strip()
    )


@dataclass(frozen=True)
class UnitProfile:
    """A document's units, aggregated. Every field is a routing explanation."""

    units: int
    judged: int
    legacy: int
    trusted: int
    unjudged: int

    english_units: int
    unicode_units: int
    numeric_units: int

    # Share of JUDGED units that are legacy. Comparable in spirit to native-1's
    # `legacy_line_ratio`, but over the three-state denominator.
    legacy_unit_ratio: float
    # Share among units that are neither Unicode nor English — "of the text that
    # could plausibly be glyph-mapped, how much is". This is the signal that sees
    # a Preeti minority inside a Unicode-majority document, where the global ratio
    # cannot.
    contested_legacy_ratio: float
    # The longest run of consecutive legacy units. A real legacy REGION is
    # contiguous; scattered singletons are usually noise.
    max_legacy_run: int

    def as_dict(self) -> dict[str, float | int]:
        return asdict(self)


def profile_units(assessments: Sequence[UnitAssessment]) -> UnitProfile:
    """Aggregate assessed units into the routing signals."""
    legacy = sum(1 for a in assessments if a.state == STATE_LEGACY)
    trusted = sum(1 for a in assessments if a.state == STATE_TRUSTED)
    unjudged = sum(1 for a in assessments if a.state == STATE_UNJUDGED)
    english = sum(1 for a in assessments if a.kind == KIND_ENGLISH)
    unicode_units = sum(1 for a in assessments if a.kind == KIND_UNICODE)
    numeric = sum(1 for a in assessments if a.kind == KIND_NUMERIC)
    judged = legacy + trusted

    run = best = 0
    for a in assessments:
        if a.state == STATE_LEGACY:
            run += 1
            best = max(best, run)
        elif a.state == STATE_TRUSTED:
            # Only a POSITIVE non-legacy unit breaks a run. An unjudged unit — a
            # blank line, a page number, a numeric row — sits inside a legacy
            # region all the time and must not chop it into fragments.
            run = 0
    contested = legacy + sum(
        1 for a in assessments if a.kind == KIND_CLEAN_LATIN
    )

    def _ratio(part: int, whole: int) -> float:
        return round(part / whole, 4) if whole else 0.0

    return UnitProfile(
        units=len(assessments),
        judged=judged,
        legacy=legacy,
        trusted=trusted,
        unjudged=unjudged,
        english_units=english,
        unicode_units=unicode_units,
        numeric_units=numeric,
        legacy_unit_ratio=_ratio(legacy, judged),
        contested_legacy_ratio=_ratio(legacy, contested),
        max_legacy_run=best,
    )
