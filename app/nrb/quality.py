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

__all__ = [
    "STOPWORDS",
    "TextMetrics",
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
    )
