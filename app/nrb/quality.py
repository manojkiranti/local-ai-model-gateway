"""Deterministic quality metrics for extracted NRB text. Pure — no I/O, no model.

This module answers "can this text be trusted", and it exists because of one
measurement: `pypdf` extracts a text layer from 49/49 fetched NRB circulars, and
every one of them contains **zero Devanagari characters**. The files are Nepali
regulatory documents. So the failure mode this phase must catch is not missing
text — that is trivially detectable — it is text that parses cleanly and is wrong.

Every metric here is a function of the extracted string ALONE. Nothing in this
module may look at a source title, a URL, a document type or a database row: an
extraction row is keyed on the content hash, one blob is shared by several
sources, and a metric that depended on which source was processed first would
persist a different answer on every run. The metadata-assisted signal lives in
`profile.py`, where it is computed over *all* referencing sources at read time.

Ratios are over NON-WHITESPACE characters, so a document's indentation cannot
move its script profile.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from statistics import median
from typing import Sequence

__all__ = [
    "Evidence",
    "PageStats",
    "REASONS",
    "SheetStats",
    "STATUSES",
    "STATUS_EXTRACTED",
    "STATUS_FAILED",
    "STATUS_NEEDS_OCR",
    "STATUS_SUSPICIOUS",
    "STATUS_UNSUPPORTED",
    "STOPWORDS",
    "TextMetrics",
    "legacy_line_counts",
    "legacy_line_ratio",
    "line_looks_glyph_mapped",
    "UNSUPPORTED_FAMILIES",
    "Verdict",
    "classify",
    "looks_like_legacy_font",
    "measure_pages",
    "measure_text",
]

# Devanagari, including the Extended block. `।`/`॥` (danda) live in the main
# block, so Nepali punctuation counts as Devanagari — which is correct: it is
# script evidence, not letter evidence.
_DEVANAGARI = re.compile(r"[ऀ-ॿ꣠-ꣿ]")
_LATIN_LETTER = re.compile(r"[A-Za-z]")
# A token is a maximal run of non-whitespace. Deliberately NOT a word regex:
# legacy-font output is full of punctuation-inside-word, and splitting on
# punctuation would destroy the very signal we are measuring.
_TOKEN = re.compile(r"\S+")
_ALPHA_TOKEN = re.compile(r"^[A-Za-z]+$")
_VOWEL = re.compile(r"[aeiouAEIOU]")

# Control characters a text file legitimately contains (tab, LF, VT, FF, CR).
# Same set as `sniff._TEXT_CONTROLS`, for the same reason.
_TEXT_CONTROLS = frozenset({0x09, 0x0A, 0x0B, 0x0C, 0x0D})

REPLACEMENT_CHAR = "�"

# The most frequent English function words. A fixed list rather than a
# dictionary: this is a STRUCTURE probe, not a language identifier. Real English
# prose runs 0.15-0.25; a Preeti-as-ASCII extraction runs ~0.00, and so does
# Unicode Devanagari — which is exactly why the detector gates on latin letters
# before it reads this number (see `classify`).
STOPWORDS = frozenset(
    """the of and to in a for is on that by with as at from be this it are or
    an was shall which not have has been will may all such any""".split()
)


@dataclass(frozen=True)
class TextMetrics:
    """Content-intrinsic measurements of one extracted string."""

    char_count: int
    non_whitespace_chars: int
    token_count: int
    line_count: int
    non_empty_lines: int

    printable_ratio: float
    control_char_ratio: float
    replacement_char_count: int
    replacement_char_ratio: float

    devanagari_ratio: float
    latin_letter_ratio: float
    digit_ratio: float
    punctuation_ratio: float

    stopword_rate: float
    vowelless_token_ratio: float
    intraword_symbol_ratio: float
    intraword_case_switch_ratio: float
    # Share of substantive LINES that look glyph-mapped. The primary legacy-font
    # signal — see `legacy_line_ratio` for why the document-level numbers above
    # could not do this job.
    legacy_line_ratio: float
    # The ratio's numerator and denominator, kept because the ratio alone cannot
    # be audited. 0.5 means something very different over 4 judged lines than over
    # 900, and `judged_lines` is also the only way to see how much of a document
    # was too short to assess at all.
    legacy_lines: int
    judged_lines: int

    def as_dict(self) -> dict[str, float | int]:
        """JSON-safe, for the `metrics` JSONB column."""
        return asdict(self)


def _ratio(part: int, whole: int) -> float:
    """Rounded to 4 places so two runs over the same bytes produce byte-identical
    JSON — the same reason `report.py` orders its counters."""
    return round(part / whole, 4) if whole else 0.0


def _is_intraword_case_switch(token: str) -> bool:
    """A lower->upper transition inside a token.

    Catches `ljQLo`, `k|fKt`, `aBcDe`; does NOT catch an acronym (`NRB`, `PDF` —
    no lowercase at all) or ordinary capitalisation (`Nepal` — the switch is
    upper->lower, at position 0). English almost never does this mid-token;
    Preeti-as-ASCII does it constantly.
    """
    return any(
        token[i].islower() and token[i + 1].isupper() for i in range(len(token) - 1)
    )


def _is_word_character(ch: str) -> bool:
    """Part of a word, for the purpose of "is there a symbol wedged in this token".

    `str.isalnum()` alone is wrong for Devanagari and it is wrong in the direction
    that matters here. Nepali vowel signs and viramas are COMBINING MARKS
    (categories Mn/Mc) — `'ा'.isalnum()` is False — so correctly extracted Nepali
    scores an `intraword_symbol_ratio` of **0.95**, higher than the legacy-font
    garbage this metric exists to detect (measured on the module's own fixtures).

    That never changes a verdict, because `looks_like_legacy_font` gates on
    `devanagari_ratio` before it reads this. But the metric is persisted in
    `nrb_extractions.metrics` and printed in the report, so leaving it inverted
    would mislead the first person who compares a Nepali document with an English
    one. Marks count as word characters.
    """
    return ch.isalnum() or unicodedata.category(ch) in ("Mn", "Mc")


def _is_intraword_symbol(token: str) -> bool:
    """A non-word character strictly INSIDE a token.

    Edges are stripped first so ordinary prose punctuation (`bank,` `(a)` `"the"`)
    does not fire. What fires is `q_fie(`, `4{i-4;f`, `ffi;` — symbols wedged
    between letters, which is the shape a glyph-mapped font produces.
    """
    core = token.strip(".,;:!?()[]{}\"'`—–-")
    if len(core) < 3:
        return False
    return any(not _is_word_character(ch) for ch in core[1:-1])


def line_looks_glyph_mapped(line: str) -> bool | None:
    """Does ONE line look like Devanagari glyphs mapped onto latin codepoints?

    Returns None when the line is too short to judge, so it can be excluded from
    the denominator rather than counted as innocent.

    Shape only — `stopword_rate` is deliberately NOT consulted here. On a
    six-token line the stopword rate is noise: it is 0.0 for most real English
    lines too, so it would flag everything.

    **Public because Phase 6B routes conversion off it** (`legacy_convert.py`),
    and a second, independently-written line detector would be free to disagree
    with the one that produced the committed native-1 measurements. Promoted
    verbatim: no threshold, no rule and no return value changed. The three-valued
    result is part of the contract — `None` is not `False`, and 6B treats an
    unjudged line inside a legacy document as a conversion candidate rather than
    as clean text (26,087 of the suspicious cohort's 99,328 non-empty lines are
    unjudged, and they are its headings, dates and table cells).
    """
    tokens = _TOKEN.findall(line)
    if len(tokens) < LEGACY_LINE_MIN_TOKENS:
        return None
    non_ws = [ch for ch in line if not ch.isspace()]
    if not non_ws:
        return None
    total = len(non_ws)
    if len(_DEVANAGARI.findall(line)) / total > LEGACY_MAX_DEVANAGARI:
        return False   # real Devanagari on this line
    if len(_LATIN_LETTER.findall(line)) / total < LEGACY_MIN_LATIN:
        return False   # a numeric row, not latin text
    alpha = [t for t in tokens if _ALPHA_TOKEN.match(t) and len(t) >= 3]
    vowelless = (
        sum(1 for t in alpha if not _VOWEL.search(t)) / len(alpha) if alpha else 0.0
    )
    symbols = sum(1 for t in tokens if _is_intraword_symbol(t)) / len(tokens)
    switches = sum(1 for t in tokens if _is_intraword_case_switch(t)) / len(tokens)
    return (
        vowelless > LEGACY_MIN_VOWELLESS
        or symbols > LEGACY_MIN_INTRAWORD_SYMBOL
        or switches > LEGACY_MIN_CASE_SWITCH
    )


def legacy_line_ratio(text: str) -> float:
    """Share of substantive lines that look glyph-mapped.

    THIS is the legacy-font signal, and it is measured per line because the
    document-level numbers demonstrably cannot do it. Measured against the 49
    fetched NRB circulars:

      * A document-level rule gated on `stopword_rate < 0.02` missed **7 of 49**,
        one of them scoring 0.248 — a *higher* stopword rate than real English
        prose. Glyph-mapped text is full of one- and two-character ASCII tokens
        (`a`, `t`, `is`, `on`), so short stopwords match by chance.
      * Worse, those 7 were not detector noise. They are genuinely MIXED
        documents: a real English annex (an audit scope, a Basel capital table)
        behind a Preeti-encoded Nepali covering note. The document average is
        honestly English — while the operative Nepali instruction is unreadable.
        A whole-document statistic cannot represent that, and averaging it is how
        the unusable half would have reached the index.

    Per line, the separation is decisive rather than marginal:

        English prose            0.000
        Unicode Devanagari       0.000
        all 49 NRB circulars     0.281 - 1.000

    Lines below `LEGACY_LINE_MIN_TOKENS` are excluded from BOTH sides of the
    ratio: a two-word heading carries no measurable structure, and counting it as
    clean would dilute the signal in exactly the documents that are worst.
    """
    legacy, judged = legacy_line_counts(text)
    return _ratio(legacy, judged)


def legacy_line_counts(text: str) -> tuple[int, int]:
    """`(glyph-mapped lines, lines judged)` — the ratio's two halves.

    Reported and persisted alongside the ratio, because a ratio on its own cannot
    be audited: 0.5 over 4 judged lines and 0.5 over 900 are not the same finding,
    and `judged_lines` against the document's own line count is the only way to
    see how much was too short to assess.
    """
    judged = [
        verdict
        for verdict in (line_looks_glyph_mapped(line) for line in text.splitlines())
        if verdict is not None
    ]
    return sum(judged), len(judged)


def measure_text(text: str) -> TextMetrics:
    """Every content-intrinsic metric for one extracted string.

    Never raises and never divides by zero: empty input returns all zeros, which
    is a legitimate measurement of a file that produced no text.
    """
    lines = text.splitlines()
    tokens = _TOKEN.findall(text)
    non_ws = [ch for ch in text if not ch.isspace()]
    total_ws = len(non_ws)

    controls = sum(
        1 for ch in text if ord(ch) < 0x20 and ord(ch) not in _TEXT_CONTROLS
    )
    # `unicodedata.category` starting with 'C' is Other (control, format,
    # surrogate, private use, unassigned) — everything a rendered document should
    # not contain. Whitespace is excluded because it is legitimately non-printing.
    unprintable = sum(
        1
        for ch in text
        if not ch.isspace() and unicodedata.category(ch).startswith("C")
    )
    replacements = text.count(REPLACEMENT_CHAR)

    alpha_tokens = [t for t in tokens if _ALPHA_TOKEN.match(t) and len(t) >= 3]
    lowered = [t.lower() for t in tokens]
    # Computed once; the ratio and both its halves are all reported.
    _legacy_counts = legacy_line_counts(text)

    return TextMetrics(
        char_count=len(text),
        non_whitespace_chars=total_ws,
        token_count=len(tokens),
        line_count=len(lines),
        non_empty_lines=sum(1 for line in lines if line.strip()),
        printable_ratio=_ratio(total_ws - unprintable, total_ws),
        control_char_ratio=_ratio(controls, total_ws),
        replacement_char_count=replacements,
        replacement_char_ratio=_ratio(replacements, total_ws),
        devanagari_ratio=_ratio(len(_DEVANAGARI.findall(text)), total_ws),
        latin_letter_ratio=_ratio(len(_LATIN_LETTER.findall(text)), total_ws),
        digit_ratio=_ratio(sum(1 for ch in non_ws if ch.isdigit()), total_ws),
        punctuation_ratio=_ratio(
            sum(1 for ch in non_ws if unicodedata.category(ch).startswith("P")),
            total_ws,
        ),
        stopword_rate=_ratio(sum(1 for t in lowered if t in STOPWORDS), len(tokens)),
        vowelless_token_ratio=_ratio(
            sum(1 for t in alpha_tokens if not _VOWEL.search(t)), len(alpha_tokens)
        ),
        intraword_symbol_ratio=_ratio(
            sum(1 for t in tokens if _is_intraword_symbol(t)), len(tokens)
        ),
        intraword_case_switch_ratio=_ratio(
            sum(1 for t in tokens if _is_intraword_case_switch(t)), len(tokens)
        ),
        legacy_line_ratio=_ratio(*_legacy_counts),
        legacy_lines=_legacy_counts[0],
        judged_lines=_legacy_counts[1],
    )


# --------------------------------------------------------------------------- #
# Closed vocabularies.
#
# Both are backed by a CHECK constraint on `nrb_extractions`. That is not
# hygiene: the same lesson as `ck_nrb_files_fetch_status` — a typo'd status
# ('needs_orc') would match no predicate and no query, so the row would read as
# evaluated to Phase 6B while meaning nothing. Adding a value means editing the
# CHECK.
# --------------------------------------------------------------------------- #
STATUS_EXTRACTED = "extracted"      # native text appears usable
STATUS_SUSPICIOUS = "suspicious"    # text exists; do not trust it unreviewed
STATUS_NEEDS_OCR = "needs_ocr"      # native extraction insufficient; needs pixels
STATUS_UNSUPPORTED = "unsupported"  # valid file, no native parser implemented
STATUS_FAILED = "failed"            # parser error, missing blob, corrupt file
STATUSES = (
    STATUS_EXTRACTED, STATUS_SUSPICIOUS, STATUS_NEEDS_OCR,
    STATUS_UNSUPPORTED, STATUS_FAILED,
)

# There is no `pending`: an extraction row is keyed on (content_sha256,
# extractor_version), so ABSENCE is pending. A status column that could also say
# "not done yet" would be a second, disagreeing answer to the same question.

REASONS = (
    "clean",                   # extracted
    "legacy_font_suspected",   # suspicious: latin codepoints carrying Devanagari
    "partial_text_coverage",   # suspicious: a partly-scanned PDF
    "replacement_characters",  # suspicious: mojibake
    "control_characters",      # suspicious: binary leakage
    "low_printable_ratio",     # suspicious: not renderable text
    "empty_spreadsheet",       # suspicious: parsed, but nothing in it
    "no_text_layer",           # needs_ocr: pages exist, text does not
    "sparse_text_layer",       # needs_ocr: a stamped page number per page
    "image_file",              # needs_ocr: the text is pixels
    "no_native_parser",        # unsupported
    "parser_error",            # failed
)

# --- thresholds ------------------------------------------------------------ #
COVERAGE_NEEDS_OCR = 0.10       # below this, a PDF has effectively no text layer
COVERAGE_SUSPICIOUS = 0.60      # below this, too much of the document is missing
COVERAGE_WARN = 0.90            # below this, say so without changing the status
MIN_CHARS_PER_PAGE = 50         # a stamped page number is not a text layer
MAX_REPLACEMENT_RATIO = 0.005
MAX_CONTROL_RATIO = 0.01
MIN_PRINTABLE_RATIO = 0.95

# The legacy-font gate. All four must hold before the shape signals are read.
# Measured margins on the fixtures and on 6 real fetched circulars:
#   English prose      stopword 0.353, vowelless 0.000, symbol 0.000
#   Unicode Nepali     stopword 0.000  (exits at the devanagari gate)
#   real NRB circulars stopword 0.000-0.002, vowelless 0.43-0.54, symbol 0.40-0.60
LEGACY_MAX_DEVANAGARI = 0.01    # a line with real Devanagari is not glyph-mapped
LEGACY_MIN_LATIN = 0.35         # it IS latin text, not a numeric row
LEGACY_MIN_TOKENS = 50          # document floor: enough text to judge at all
LEGACY_LINE_MIN_TOKENS = 4      # line floor: below this a line has no structure
# At least one of these shape signals must fire on a line.
LEGACY_MIN_VOWELLESS = 0.30
LEGACY_MIN_INTRAWORD_SYMBOL = 0.15
LEGACY_MIN_CASE_SWITCH = 0.10
# Share of substantive lines that must look glyph-mapped. Measured margin:
# English and Unicode Devanagari score 0.000; the WORST of 49 real NRB circulars
# scores 0.281. Set at 0.20 — comfortably between, and deliberately low, because
# a document whose Nepali half is unreadable is unsafe even when its English half
# is perfect.
LEGACY_LINE_RATIO = 0.20

# Families with no native parser in this dependency set. `.xls` and `.doc` are
# 324 files (1.8% of the corpus); adding xlrd/antiword is a Phase 6B decision, and
# reporting them honestly is what makes that decision possible.
UNSUPPORTED_FAMILIES = frozenset({"office_legacy", "archive", "web", "unknown"})


@dataclass(frozen=True)
class PageStats:
    page_count: int
    pages_with_text: int
    text_page_coverage: float
    # Over EVERY page — reporting only. Informative, but it falls with coverage,
    # so it cannot distinguish the two faults below.
    median_chars_per_page: float
    # Over pages that produced text — what `sparse_text_layer` keys on. See
    # `measure_pages` for why these must be two different numbers.
    median_chars_per_text_page: float


@dataclass(frozen=True)
class SheetStats:
    sheet_count: int
    row_count: int
    non_empty_cells: int
    populated_ratio: float


@dataclass(frozen=True)
class Evidence:
    """Everything `classify` may look at — and nothing else.

    Deliberately a neutral carrier rather than `extraction.ExtractionResult`:
    `extraction.py` imports pypdf, and the dependency must run one way so this
    module stays trivially testable with hand-authored strings.

    Note what is ABSENT: no title, no URL, no document type, no owner, no file id.
    See the module docstring.
    """

    family: str                       # sniff.FAMILIES
    parsed: bool                      # a parser ran to completion
    error: str | None                 # exception type + short message
    text_metrics: TextMetrics | None
    pages: PageStats | None           # PDFs only
    sheets: SheetStats | None         # spreadsheets only


@dataclass(frozen=True)
class Verdict:
    status: str
    reason: str
    warnings: tuple[str, ...] = ()


def measure_pages(page_texts: Sequence[str]) -> PageStats:
    """Per-page structure for a PDF.

    MEDIAN rather than mean: one 40-page scanned appendix behind a text-rich
    cover page averages out to "text-rich" and would be classified `extracted`.
    The median says what most of the document is like.

    TWO medians, because there are two distinct faults and one number cannot tell
    them apart:

      * **How many pages have any text at all** is `text_page_coverage`. One
        readable page in six is a partly-scanned document.
      * **How much text the readable pages carry** is
        `median_chars_per_text_page`. A stamped page number on every page is a
        scan with a useless text layer — coverage 1.0, but nothing there.

    A median over ALL pages collapses into the first: as soon as more than half
    the pages are blank it reads 0 whatever the readable pages contain, so a
    partly-scanned document would be reported as having a sparse text layer,
    which is a different — and wrong — diagnosis.
    """
    lengths = [len(t.strip()) for t in page_texts]
    with_text = [n for n in lengths if n > 0]
    return PageStats(
        page_count=len(lengths),
        pages_with_text=len(with_text),
        text_page_coverage=_ratio(len(with_text), len(lengths)),
        median_chars_per_page=float(median(lengths)) if lengths else 0.0,
        median_chars_per_text_page=float(median(with_text)) if with_text else 0.0,
    )


def looks_like_legacy_font(metrics: TextMetrics) -> bool:
    """Latin codepoints carrying Devanagari glyphs (Preeti/Kantipur), or an
    equally unusable embedded OCR layer.

    The two are indistinguishable from the bytes and share one remedy, so this
    does not try to separate them.

    The judgement is `legacy_line_ratio` — a per-LINE measurement — for the
    reasons documented there: a document-level rule missed 7 of 49 real NRB
    circulars, including mixed documents whose English annex masked an unreadable
    Nepali directive. `stopword_rate` survives as a reported metric but is no
    longer a gate; it was the specific gate those 7 escaped through.

    The only document-level condition left is having enough text to judge at all.
    Correct Nepali needs no special exemption: every one of its lines carries real
    Devanagari, so no line is ever counted as glyph-mapped.
    """
    if metrics.token_count < LEGACY_MIN_TOKENS:
        return False
    return metrics.legacy_line_ratio > LEGACY_LINE_RATIO


def classify(evidence: Evidence) -> Verdict:
    """The status of one extraction. Deterministic, ordered, first match wins.

    Ties break toward `suspicious`, never toward `extracted`: a wrong document
    that parses cleanly is the failure this whole phase exists to prevent, and it
    is strictly worse than a recorded doubt.

    There is no numeric quality score. A score invites threshold-tuning without
    labels; the rules below are individually arguable and individually testable.
    """
    warnings: list[str] = []

    # 1. failed — the parser could not produce anything.
    if evidence.error is not None:
        return Verdict(STATUS_FAILED, "parser_error")

    # 2. unsupported — a valid file we have no parser for. Checked before the
    #    text rules because there IS no text to reason about.
    if evidence.family in UNSUPPORTED_FAMILIES:
        return Verdict(STATUS_UNSUPPORTED, "no_native_parser")

    # 3. needs_ocr — an image is a valid file whose text is pixels. NOT `failed`.
    if evidence.family == "image":
        return Verdict(STATUS_NEEDS_OCR, "image_file")

    if not evidence.parsed or evidence.text_metrics is None:
        return Verdict(STATUS_FAILED, "parser_error")

    metrics = evidence.text_metrics

    # 4. spreadsheets are judged STRUCTURALLY. A statistical table has no prose,
    #    so every linguistic rule below would misfire on one.
    if evidence.sheets is not None:
        if evidence.sheets.non_empty_cells == 0:
            return Verdict(STATUS_SUSPICIOUS, "empty_spreadsheet")
        return Verdict(STATUS_EXTRACTED, "clean", tuple(warnings))

    # Warnings are collected BEFORE any branch can return, so a verdict never
    # silently loses one just because an earlier rule matched.
    if metrics.token_count < LEGACY_MIN_TOKENS:
        warnings.append("insufficient_text")

    # 5. PDF structure. Two distinct faults, each with its own measurement:
    #    almost no page has text (a scan), or the pages that do have text carry
    #    almost none (a scan with a stamped page number). See `measure_pages`.
    if evidence.pages is not None and evidence.pages.page_count > 0:
        if evidence.pages.text_page_coverage < COVERAGE_NEEDS_OCR:
            return Verdict(STATUS_NEEDS_OCR, "no_text_layer", tuple(warnings))
        if evidence.pages.median_chars_per_text_page < MIN_CHARS_PER_PAGE:
            return Verdict(STATUS_NEEDS_OCR, "sparse_text_layer", tuple(warnings))

    # 6. suspicion, in severity order.
    if looks_like_legacy_font(metrics):
        return Verdict(STATUS_SUSPICIOUS, "legacy_font_suspected", tuple(warnings))
    if metrics.replacement_char_ratio > MAX_REPLACEMENT_RATIO:
        return Verdict(STATUS_SUSPICIOUS, "replacement_characters", tuple(warnings))
    if metrics.control_char_ratio > MAX_CONTROL_RATIO:
        return Verdict(STATUS_SUSPICIOUS, "control_characters", tuple(warnings))
    if metrics.printable_ratio < MIN_PRINTABLE_RATIO:
        return Verdict(STATUS_SUSPICIOUS, "low_printable_ratio", tuple(warnings))
    if evidence.pages is not None and evidence.pages.page_count > 0:
        coverage = evidence.pages.text_page_coverage
        if coverage < COVERAGE_SUSPICIOUS:
            return Verdict(STATUS_SUSPICIOUS, "partial_text_coverage", tuple(warnings))
        if coverage < COVERAGE_WARN:
            warnings.append("partial_text_coverage")

    if metrics.non_whitespace_chars == 0:
        return Verdict(STATUS_NEEDS_OCR, "no_text_layer", tuple(warnings))

    return Verdict(STATUS_EXTRACTED, "clean", tuple(warnings))
