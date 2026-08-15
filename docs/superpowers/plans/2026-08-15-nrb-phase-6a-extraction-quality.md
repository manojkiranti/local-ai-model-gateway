# NRB Phase 6A — Native Extraction + Quality Profiling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Send already-fetched NRB blobs through native extraction, measure the text deterministically, and classify each blob `extracted` / `suspicious` / `needs_ocr` / `unsupported` / `failed` — so Phase 6B chooses an OCR strategy from evidence instead of a guess.

**Architecture:** `pypdf` (a base dependency, no torch) is the per-page screen; Docling is used only for a bounded agreement calibration. Pure metric and classification logic lives in `app/nrb/quality.py`, format dispatch in `app/nrb/extraction.py`, the pass in `app/nrb/extract.py`, data access in `app/nrb/catalog.py`. Results persist in one new table `nrb_extractions` keyed on `(content_sha256, extractor_version)` — content-intrinsic only.

**Tech Stack:** Python 3.10, SQLAlchemy 2 Core (async), Postgres, Alembic, pypdf, openpyxl, python-docx, pytest. Docling is worker-only and imported inside functions.

**Spec:** `docs/superpowers/specs/2026-08-15-nrb-phase-6a-extraction-quality-design.md` — read it alongside this plan; the plan argues from it.

## Global Constraints

- **Use this project's venv:** `.venv/bin/python`, `.venv/bin/pytest`, `.venv/bin/alembic`. Never a sibling's.
- **Database is the scratch DB.** Every DB command and test gets `DATABASE_URL=postgresql+asyncpg://gateway:<pw>@127.0.0.1:5432/local_ai_gateway_p4` (password from `.env`). Never `local_ai_gateway`.
- **Never** run `alembic stamp`, touch `feat/rag-source-citations`, drop `chat_messages.sources`, or reconcile lineage. The new migration's `down_revision` is `2b7f5c9d1a34` (the current single head on this branch).
- **No OCR of any kind.** No Tesseract, Paddle, EasyOCR, Docling OCR, vision or cloud OCR. No legacy-font→Unicode conversion.
- **No chunking, embeddings, pgvector writes, `documents`/`document_chunks`/`ingest_jobs` rows, `search_nrb_documents`, `LOCAL_TOOLS` entry, endpoint or cron.**
- **No new runtime dependencies.** `.xls` and `.doc` are `unsupported` by design.
- **No network access in Phase 6A code.** Extraction reads local blobs only. (The one live fetch in Task 13 uses the existing Phase 5 command.)
- **`app/rag/parsing.py` is not modified.** Its Docling CPU/no-OCR pinning is load-bearing for department RAG.
- **Docling is never imported at module scope** — only inside a function, like `app/rag/parsing.py` does. `tests/test_rag_parsing_docling.py`'s subprocess check must keep passing.
- **No extracted text is persisted.** Only a ≤300-character preview.
- **Errors never carry a stack trace, absolute path or user id into the database** — exception type plus a short message, the rule `app/files/documents.py` already follows.
- **`app/nrb/catalog.py` uses Core statements only**, never `update(Model)` with a `metadata`/`meta` payload. New functions follow that.
- Commit after every task. Conventional-commit style, matching this repo's history.

---

### Task 1: Character and token metrics (`quality.py`, part 1)

The pure heart of the phase. Everything else consumes `TextMetrics`.

**Files:**
- Create: `app/nrb/quality.py`
- Test: `tests/test_nrb_quality.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `TextMetrics` (frozen dataclass, fields listed below), `measure_text(text: str) -> TextMetrics`, `TextMetrics.as_dict() -> dict[str, float | int]`, `STOPWORDS: frozenset[str]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_nrb_quality.py`:

```python
"""Deterministic extraction-quality metrics. Pure — no DB, no network, no files.

The inputs here are hand-authored rather than sampled, because the point is a
LABELLED set: each string has a known correct answer, so a rule change is scored
rather than eyeballed. The legacy-font strings are copied from real NRB circular
extractions (see the spec's §2), not invented.
"""

from app.nrb import quality

ENGLISH = (
    "Nepal Rastra Bank issued a circular to all licensed institutions today. "
    "The circular requires that every bank shall report its exposure to the "
    "central bank within thirty days of the end of the quarter."
)

# Unicode Devanagari — a correctly extracted Nepali document. The single most
# important negative case in the suite: this must never be called suspicious.
NEPALI = (
    "नेपाल राष्ट्र बैंकले सम्पूर्ण इजाजतपत्रप्राप्त संस्थाहरूलाई परिपत्र जारी गरेको छ। "
    "प्रत्येक बैंकले आफ्नो जोखिम विवरण तीस दिनभित्र केन्द्रीय बैंकमा पेश गर्नुपर्नेछ।"
)

# Legacy-font (Preeti/Kantipur) extraction, verbatim shape from a real NRB
# circular: latin codepoints carrying Devanagari glyphs, with the English
# letterhead fragments that make a naive "is this English" test say yes.
LEGACY = (
    "ffihW\\ffifiHrz\\reU=,.\n"
    "iqrn rrq *+,\n"
    "ff{T ffi {eil Fuqq h}Trtr\n"
    "qtq i.: YYlqqoYr/{\n"
    "Web Site: www.nrb.org.np\n"
    "frq qw:igl ffi; Qoec,/o!/lR q{ {@r : i.H.H.tq\n"
    "fi156dqgfq1 r4r \"€\" ( \"1T\" 4{i-4;f t+ Oqf ffiq d1rT[6{\n"
    "q_fie( ffirra, i.;. oi Frtqm { R/oeq +1 gET d r. ]T qR-{-rq qq-r{\n"
)


def test_empty_text_is_all_zero_and_never_divides_by_zero():
    m = quality.measure_text("")
    assert m.char_count == 0
    assert m.token_count == 0
    assert m.printable_ratio == 0.0
    assert m.devanagari_ratio == 0.0
    assert m.stopword_rate == 0.0


def test_whitespace_only_text_has_no_non_whitespace_characters():
    m = quality.measure_text("   \n\n\t  ")
    assert m.char_count == 8
    assert m.non_whitespace_chars == 0
    assert m.devanagari_ratio == 0.0
    assert m.latin_letter_ratio == 0.0


def test_english_prose_has_a_high_stopword_rate_and_latin_dominance():
    m = quality.measure_text(ENGLISH)
    assert m.latin_letter_ratio > 0.7
    assert m.devanagari_ratio == 0.0
    # Real English prose runs 0.15-0.25. This is the signal the legacy detector
    # keys on, so its floor matters more than its exact value.
    assert m.stopword_rate > 0.15
    assert m.vowelless_token_ratio < 0.10
    assert m.intraword_symbol_ratio < 0.05


def test_unicode_nepali_is_devanagari_dominant_with_no_english_structure():
    m = quality.measure_text(NEPALI)
    assert m.devanagari_ratio > 0.7
    assert m.latin_letter_ratio < 0.05
    # Zero English stopwords, exactly like the legacy case — which is WHY the
    # detector must gate on latin_letter_ratio before it looks at this number.
    assert m.stopword_rate < 0.02


def test_mixed_nepali_and_english_lands_between_the_two():
    m = quality.measure_text(NEPALI + "\n" + ENGLISH)
    assert 0.2 < m.devanagari_ratio < 0.8
    assert 0.2 < m.latin_letter_ratio < 0.8


def test_legacy_font_output_is_latin_with_no_english_structure():
    m = quality.measure_text(LEGACY)
    assert m.devanagari_ratio < 0.01
    assert m.latin_letter_ratio > 0.35
    # The four signals the detector combines, each asserted separately so a
    # regression names which one moved.
    assert m.stopword_rate < 0.02
    assert m.vowelless_token_ratio > 0.30
    assert m.intraword_symbol_ratio > 0.15


def test_control_characters_are_counted_and_lower_the_printable_ratio():
    m = quality.measure_text("abc\x00\x01\x02\x03def")
    assert m.control_char_ratio > 0.3
    assert m.printable_ratio < 0.7


def test_tab_and_newline_are_printable_not_control():
    m = quality.measure_text("a\tb\nc\r\nd")
    assert m.control_char_ratio == 0.0
    assert m.printable_ratio == 1.0


def test_replacement_characters_are_counted_and_rationed():
    m = quality.measure_text("abcd����")
    assert m.replacement_char_count == 4
    assert m.replacement_char_ratio == 0.5


def test_digits_and_punctuation_are_measured_separately_from_letters():
    m = quality.measure_text("1,234.56 | 7,890.12 | 3,456.78")
    assert m.digit_ratio > 0.5
    assert m.latin_letter_ratio == 0.0


def test_ratios_are_over_non_whitespace_so_indentation_cannot_move_them():
    tight = quality.measure_text("नेपाल")
    padded = quality.measure_text("        नेपाल        \n\n\n")
    assert tight.devanagari_ratio == padded.devanagari_ratio == 1.0


def test_intraword_case_switch_catches_legacy_shapes_but_not_acronyms():
    # `ljQLo` / `k|fKt` switch case mid-token; `NRB` and `PDF` do not (all upper).
    assert quality.measure_text("ljQLo k|fKt aBcDe").intraword_case_switch_ratio > 0.5
    assert quality.measure_text("NRB PDF USD IMF").intraword_case_switch_ratio == 0.0


def test_line_counts_distinguish_blank_lines():
    m = quality.measure_text("one\n\ntwo\n\n\nthree")
    assert m.line_count == 6
    assert m.non_empty_lines == 3


def test_as_dict_round_trips_every_field_as_a_json_safe_scalar():
    d = quality.measure_text(ENGLISH).as_dict()
    assert set(d) == {f.name for f in quality.TextMetrics.__dataclass_fields__.values()}
    assert all(isinstance(v, (int, float)) for v in d.values())


def test_measure_text_is_deterministic():
    assert quality.measure_text(LEGACY) == quality.measure_text(LEGACY)
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/pytest tests/test_nrb_quality.py -q
```

Expected: collection error / `ModuleNotFoundError: app.nrb.quality`.

- [ ] **Step 3: Write `app/nrb/quality.py` (metrics half)**

```python
"""Deterministic quality metrics for extracted NRB text. Pure — no I/O, no model.

This module answers "can this text be trusted", and it exists because of one
measurement (spec §2): `pypdf` extracts a text layer from 49/49 fetched NRB
circulars, and every one of them contains **zero Devanagari characters**. The
files are Nepali regulatory documents. So the failure mode this phase must catch
is not missing text — that is trivially detectable — it is text that parses
cleanly and is wrong.

Every metric here is a function of the extracted string ALONE. Nothing in this
module may look at a source title, a URL, a document type or a database row: an
extraction row is keyed on the content hash, one blob is shared by several
sources, and a metric that depended on which source was processed first would
persist a different answer on every run. The metadata-assisted signal lives in
`report.py`, where it is computed over *all* referencing sources. See spec §8.

Ratios are over NON-WHITESPACE characters, so a document's indentation cannot
move its script profile.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, fields

__all__ = [
    "STOPWORDS",
    "TextMetrics",
    "measure_text",
]

# Devanagari, including the Extended block. `।`/`॥` (danda) live in the
# main block, so Nepali punctuation counts as Devanagari — which is correct: it is
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

# The 30 most frequent English function words. A fixed list rather than a
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


def _is_intraword_symbol(token: str) -> bool:
    """A non-alphanumeric character strictly INSIDE a token.

    Edges are stripped first so ordinary prose punctuation (`bank,` `(a)` `"the"`)
    does not fire. What fires is `q_fie(`, `4{i-4;f`, `ffi;` — symbols wedged
    between letters, which is the shape a glyph-mapped font produces.
    """
    core = token.strip(".,;:!?()[]{}\"'`—–-")
    if len(core) < 3:
        return False
    return any(not ch.isalnum() for ch in core[1:-1])


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
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_nrb_quality.py -q
```

Expected: PASS, 15 tests. If `test_legacy_font_output_is_latin_with_no_english_structure` fails on `vowelless_token_ratio`, print the metric and check `_ALPHA_TOKEN`'s `len >= 3` floor — do **not** loosen the threshold to make the test pass; the threshold is the finding.

- [ ] **Step 5: Commit**

```bash
git add app/nrb/quality.py tests/test_nrb_quality.py
git commit -m "feat(nrb): content-intrinsic extraction-quality metrics (Phase 6A)"
```

---

### Task 2: Page/sheet stats and the classifier (`quality.py`, part 2)

**Files:**
- Modify: `app/nrb/quality.py` (append)
- Test: `tests/test_nrb_quality.py` (append)

**Interfaces:**
- Consumes: `TextMetrics`, `measure_text` from Task 1.
- Produces: `PageStats`, `SheetStats`, `Evidence`, `Verdict`, `measure_pages(page_texts: Sequence[str]) -> PageStats`, `classify(evidence: Evidence) -> Verdict`, and the status/reason constants `STATUS_EXTRACTED`, `STATUS_SUSPICIOUS`, `STATUS_NEEDS_OCR`, `STATUS_UNSUPPORTED`, `STATUS_FAILED`, `STATUSES`, `REASONS`.

`Evidence` is the neutral carrier that keeps `quality.py` free of any import from `extraction.py` (which imports pypdf) — the dependency runs one way only.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_nrb_quality.py`:

```python
from app.nrb.quality import (
    Evidence,
    SheetStats,
    STATUS_EXTRACTED,
    STATUS_FAILED,
    STATUS_NEEDS_OCR,
    STATUS_SUSPICIOUS,
    STATUS_UNSUPPORTED,
)


def _pdf_evidence(text, page_texts):
    return Evidence(
        family="pdf",
        parsed=True,
        error=None,
        text_metrics=quality.measure_text(text),
        pages=quality.measure_pages(page_texts),
        sheets=None,
    )


# --- page statistics ------------------------------------------------------- #

def test_page_coverage_counts_pages_that_produced_text():
    stats = quality.measure_pages(["hello there", "", "more words", ""])
    assert stats.page_count == 4
    assert stats.pages_with_text == 2
    assert stats.text_page_coverage == 0.5


def test_page_stats_on_zero_pages_do_not_divide_by_zero():
    stats = quality.measure_pages([])
    assert stats.page_count == 0
    assert stats.text_page_coverage == 0.0
    assert stats.median_chars_per_page == 0.0


def test_median_chars_per_page_resists_one_huge_page():
    # Mean would be ~2000 and call this document text-rich; the median says 10.
    stats = quality.measure_pages(["x" * 10, "x" * 10, "x" * 10, "x" * 8000])
    assert stats.median_chars_per_page == 10.0


# --- classification -------------------------------------------------------- #

def test_good_english_pdf_is_extracted():
    verdict = quality.classify(_pdf_evidence(ENGLISH * 4, [ENGLISH] * 4))
    assert verdict.status == STATUS_EXTRACTED


def test_unicode_nepali_pdf_is_extracted_not_suspicious():
    # THE false-positive test. A correctly extracted Nepali document must pass.
    verdict = quality.classify(_pdf_evidence(NEPALI * 6, [NEPALI] * 6))
    assert verdict.status == STATUS_EXTRACTED


def test_legacy_font_pdf_is_suspicious_with_the_legacy_reason():
    verdict = quality.classify(_pdf_evidence(LEGACY * 6, [LEGACY] * 6))
    assert verdict.status == STATUS_SUSPICIOUS
    assert verdict.reason == "legacy_font_suspected"


def test_pdf_with_pages_but_no_text_needs_ocr():
    verdict = quality.classify(_pdf_evidence("", ["", "", "", ""]))
    assert verdict.status == STATUS_NEEDS_OCR
    assert verdict.reason == "no_text_layer"


def test_pdf_with_a_trivial_text_layer_needs_ocr():
    # A scanned PDF whose only text is a stamped page number per page.
    verdict = quality.classify(_pdf_evidence("1 2 3 4", ["1", "2", "3", "4"]))
    assert verdict.status == STATUS_NEEDS_OCR
    assert verdict.reason == "sparse_text_layer"


def test_partly_scanned_pdf_is_suspicious_not_extracted():
    pages = [ENGLISH, "", "", "", "", ""]   # coverage 0.166
    verdict = quality.classify(_pdf_evidence(ENGLISH, pages))
    assert verdict.status == STATUS_SUSPICIOUS
    assert verdict.reason == "partial_text_coverage"


def test_mostly_readable_pdf_is_extracted_but_warns_about_the_gap():
    pages = [ENGLISH] * 7 + ["", ""]        # coverage 0.777
    verdict = quality.classify(_pdf_evidence(ENGLISH * 7, pages))
    assert verdict.status == STATUS_EXTRACTED
    assert "partial_text_coverage" in verdict.warnings


def test_mojibake_is_suspicious():
    text = ENGLISH + "�" * 40
    verdict = quality.classify(_pdf_evidence(text, [text]))
    assert verdict.status == STATUS_SUSPICIOUS
    assert verdict.reason == "replacement_characters"


def test_control_character_soup_is_suspicious():
    text = ENGLISH + "\x00\x01\x02\x03\x04\x05\x06\x07" * 6
    verdict = quality.classify(_pdf_evidence(text, [text]))
    assert verdict.status == STATUS_SUSPICIOUS


def test_an_image_needs_ocr_and_is_never_failed():
    verdict = quality.classify(
        Evidence(family="image", parsed=False, error=None,
                 text_metrics=None, pages=None, sheets=None)
    )
    assert verdict.status == STATUS_NEEDS_OCR
    assert verdict.reason == "image_file"


def test_legacy_office_is_unsupported():
    verdict = quality.classify(
        Evidence(family="office_legacy", parsed=False, error=None,
                 text_metrics=None, pages=None, sheets=None)
    )
    assert verdict.status == STATUS_UNSUPPORTED
    assert verdict.reason == "no_native_parser"


def test_a_parser_error_is_failed():
    verdict = quality.classify(
        Evidence(family="pdf", parsed=False, error="PdfReadError",
                 text_metrics=None, pages=None, sheets=None)
    )
    assert verdict.status == STATUS_FAILED


def test_a_populated_spreadsheet_is_extracted():
    verdict = quality.classify(
        Evidence(family="spreadsheet", parsed=True, error=None,
                 text_metrics=quality.measure_text("a b c"),
                 pages=None,
                 sheets=SheetStats(sheet_count=2, row_count=400,
                                   non_empty_cells=1800, populated_ratio=0.75))
    )
    assert verdict.status == STATUS_EXTRACTED


def test_an_empty_spreadsheet_is_suspicious():
    verdict = quality.classify(
        Evidence(family="spreadsheet", parsed=True, error=None,
                 text_metrics=quality.measure_text(""),
                 pages=None,
                 sheets=SheetStats(sheet_count=1, row_count=0,
                                   non_empty_cells=0, populated_ratio=0.0))
    )
    assert verdict.status == STATUS_SUSPICIOUS
    assert verdict.reason == "empty_spreadsheet"


def test_a_spreadsheet_is_never_judged_on_devanagari_ratio():
    # Numeric statistical tables have no letters at all. That is normal for a
    # spreadsheet and must not read as legacy-font output.
    verdict = quality.classify(
        Evidence(family="spreadsheet", parsed=True, error=None,
                 text_metrics=quality.measure_text("1,204 3,880 91.2 44.0 " * 60),
                 pages=None,
                 sheets=SheetStats(sheet_count=1, row_count=240,
                                   non_empty_cells=960, populated_ratio=0.9))
    )
    assert verdict.status == STATUS_EXTRACTED


def test_a_digit_heavy_pdf_table_is_not_called_legacy_font():
    # The legacy gate requires latin_letter_ratio > 0.35 for exactly this reason.
    text = "1,204 3,880 91.2 44.0 12,556 8.7 " * 40
    verdict = quality.classify(_pdf_evidence(text, [text] * 3))
    assert verdict.reason != "legacy_font_suspected"


def test_short_text_is_never_called_legacy_font():
    verdict = quality.classify(_pdf_evidence("k|fKt ljQLo", ["k|fKt ljQLo"]))
    assert verdict.reason != "legacy_font_suspected"
    assert "insufficient_text" in verdict.warnings


def test_every_verdict_status_is_in_the_closed_vocabulary():
    for ev in [
        _pdf_evidence(ENGLISH * 4, [ENGLISH] * 4),
        _pdf_evidence(LEGACY * 6, [LEGACY] * 6),
        _pdf_evidence("", ["", ""]),
        Evidence("image", False, None, None, None, None),
        Evidence("unknown", False, "boom", None, None, None),
    ]:
        assert quality.classify(ev).status in quality.STATUSES


def test_classify_is_deterministic():
    ev = _pdf_evidence(LEGACY * 6, [LEGACY] * 6)
    assert quality.classify(ev) == quality.classify(ev)
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/pytest tests/test_nrb_quality.py -q
```

Expected: `ImportError: cannot import name 'Evidence'`.

- [ ] **Step 3: Append the classifier to `app/nrb/quality.py`**

Add to `__all__`: `"Evidence"`, `"PageStats"`, `"REASONS"`, `"SheetStats"`, `"STATUSES"`, `"STATUS_EXTRACTED"`, `"STATUS_FAILED"`, `"STATUS_NEEDS_OCR"`, `"STATUS_SUSPICIOUS"`, `"STATUS_UNSUPPORTED"`, `"Verdict"`, `"classify"`, `"measure_pages"`. Add `from statistics import median` and `from typing import Sequence` to the imports. Then append:

```python
# --------------------------------------------------------------------------- #
# Closed vocabularies. CHECK-constrained in the database, same rule as
# `models.FETCH_STATUSES`: a typo'd status matches no predicate and no query, so
# the row would read as evaluated to Phase 6B while meaning nothing.
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
# extractor_version), so ABSENCE is pending. A status column that could say
# "not done yet" would be a second, disagreeing answer to the same question.

REASONS = (
    "clean",                  # extracted
    "legacy_font_suspected",  # suspicious: latin codepoints carrying Devanagari
    "partial_text_coverage",  # suspicious: a partly-scanned PDF
    "replacement_characters", # suspicious: mojibake
    "control_characters",     # suspicious: binary leakage
    "low_printable_ratio",    # suspicious: not renderable text
    "empty_spreadsheet",      # suspicious: parsed, but nothing in it
    "no_text_layer",          # needs_ocr: pages exist, text does not
    "sparse_text_layer",      # needs_ocr: a page number per page
    "image_file",             # needs_ocr: the text is pixels
    "no_native_parser",       # unsupported
    "parser_error",           # failed
)

# --- thresholds, all justified in the spec (§6, §7) ------------------------ #
COVERAGE_NEEDS_OCR = 0.10       # below this, a PDF has effectively no text layer
COVERAGE_SUSPICIOUS = 0.60      # below this, too much of the document is missing
COVERAGE_WARN = 0.90            # below this, say so without changing the status
MIN_CHARS_PER_PAGE = 50         # a stamped page number is not a text layer
MAX_REPLACEMENT_RATIO = 0.005
MAX_CONTROL_RATIO = 0.01
MIN_PRINTABLE_RATIO = 0.95

# The legacy-font gate. All four must hold before the shape signals are read.
LEGACY_MAX_DEVANAGARI = 0.01    # essentially no Unicode Devanagari
LEGACY_MIN_LATIN = 0.35         # it IS latin text, not a numeric table
LEGACY_MIN_TOKENS = 50          # enough tokens to measure structure
LEGACY_MAX_STOPWORD_RATE = 0.02 # English prose runs 0.15-0.25
# ...and at least one of these corroborating shape signals.
LEGACY_MIN_VOWELLESS = 0.30
LEGACY_MIN_INTRAWORD_SYMBOL = 0.15
LEGACY_MIN_CASE_SWITCH = 0.10

# Families with no native parser in this dependency set. `.xls` and `.doc` are
# 324 files (1.8% of the corpus); adding xlrd/antiword is a Phase 6B decision, and
# reporting them honestly is what makes that decision possible.
UNSUPPORTED_FAMILIES = frozenset({"office_legacy", "archive", "web", "unknown"})


@dataclass(frozen=True)
class PageStats:
    page_count: int
    pages_with_text: int
    text_page_coverage: float
    median_chars_per_page: float


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

    The MEDIAN chars per page, not the mean: one 40-page scanned appendix behind
    a text-rich cover page averages out to "text-rich" and would be classified
    `extracted`. The median says what most of the document is like.
    """
    lengths = [len(t.strip()) for t in page_texts]
    with_text = sum(1 for n in lengths if n > 0)
    return PageStats(
        page_count=len(lengths),
        pages_with_text=with_text,
        text_page_coverage=_ratio(with_text, len(lengths)),
        median_chars_per_page=float(median(lengths)) if lengths else 0.0,
    )


def looks_like_legacy_font(metrics: TextMetrics) -> bool:
    """Latin codepoints carrying Devanagari glyphs (Preeti/Kantipur), or an
    equally unusable embedded OCR layer.

    The two are indistinguishable from the bytes and share one remedy, so this
    does not try to separate them.

    Four gates, then corroboration. The gates are what keep the false-positive
    rate low, and each rules out a specific innocent case:

      * `devanagari_ratio` — a correctly extracted Nepali document exits here.
      * `latin_letter_ratio` — a numeric statistical table exits here. Without
        this gate, every table in the corpus scores zero stopwords and would be
        called garbage.
      * `token_count` — a two-line cover page has no measurable structure.
      * `stopword_rate` — the discriminating signal. English prose runs
        0.15-0.25; glyph-mapped ASCII runs ~0.00.

    Then at least one shape signal, because a stopword rate of zero alone is also
    true of, say, a page of proper nouns.
    """
    if metrics.devanagari_ratio > LEGACY_MAX_DEVANAGARI:
        return False
    if metrics.latin_letter_ratio < LEGACY_MIN_LATIN:
        return False
    if metrics.token_count < LEGACY_MIN_TOKENS:
        return False
    if metrics.stopword_rate >= LEGACY_MAX_STOPWORD_RATE:
        return False
    return (
        metrics.vowelless_token_ratio > LEGACY_MIN_VOWELLESS
        or metrics.intraword_symbol_ratio > LEGACY_MIN_INTRAWORD_SYMBOL
        or metrics.intraword_case_switch_ratio > LEGACY_MIN_CASE_SWITCH
    )


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

    # 5. PDF structure: no text layer at all, or one stamped page number a page.
    if evidence.pages is not None and evidence.pages.page_count > 0:
        coverage = evidence.pages.text_page_coverage
        if coverage < COVERAGE_NEEDS_OCR:
            return Verdict(STATUS_NEEDS_OCR, "no_text_layer")
        if evidence.pages.median_chars_per_page < MIN_CHARS_PER_PAGE:
            return Verdict(STATUS_NEEDS_OCR, "sparse_text_layer")

    if metrics.token_count < LEGACY_MIN_TOKENS:
        warnings.append("insufficient_text")

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
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_nrb_quality.py -q
```

Expected: PASS, ~36 tests.

- [ ] **Step 5: Commit**

```bash
git add app/nrb/quality.py tests/test_nrb_quality.py
git commit -m "feat(nrb): deterministic extraction-quality classifier (Phase 6A)"
```

---

### Task 3: One pypdf call site — extract `read_pdf_pages`

The whole reuse boundary between the files subsystem and NRB. Behaviour-preserving.

**Files:**
- Modify: `app/files/documents.py:144-200` (`_read_pdf`)
- Test: `tests/test_files_documents_pdf_pages.py` (new), plus the existing document suite as the regression gate.

**Interfaces:**
- Consumes: nothing new.
- Produces: `PdfPages` (frozen dataclass: `pages: tuple[str, ...]`, `total: int`, `skipped: int`) and `read_pdf_pages(path: Path) -> PdfPages` in `app/files/documents.py`. `_read_pdf` is rewritten to call it. `EncryptedDocument` and `ReadError` behaviour are unchanged.

- [ ] **Step 1: Write the failing test**

Create `tests/test_files_documents_pdf_pages.py`:

```python
"""`read_pdf_pages` — the single pypdf call site in the repository.

NRB Phase 6A needs per-page text to compute page coverage; `read_document` needs
a flat line stream. Both come from here, so a change to encryption handling or
the page cap cannot apply to one and not the other.
"""

import pytest
from fpdf import FPDF

from app.files import documents
from app.files.readers import ReadError


def _pdf(tmp_path, pages):
    pdf = FPDF()
    for body in pages:
        pdf.add_page()
        if body:
            pdf.set_font("helvetica", size=12)
            pdf.cell(0, 10, body)
    path = tmp_path / "doc.pdf"
    pdf.output(str(path))
    return path


def test_returns_one_entry_per_page_in_order(tmp_path):
    result = documents.read_pdf_pages(_pdf(tmp_path, ["alpha", "beta", "gamma"]))
    assert result.total == 3
    assert len(result.pages) == 3
    assert "alpha" in result.pages[0]
    assert "gamma" in result.pages[2]


def test_a_page_with_no_text_is_an_empty_string_not_a_missing_entry(tmp_path):
    result = documents.read_pdf_pages(_pdf(tmp_path, ["alpha", "", "gamma"]))
    assert len(result.pages) == 3
    assert result.pages[1].strip() == ""


def test_the_page_cap_is_reported_rather_than_silently_applied(tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "MAX_PDF_PAGES", 2)
    result = documents.read_pdf_pages(_pdf(tmp_path, ["a", "b", "c", "d"]))
    assert result.total == 4
    assert len(result.pages) == 2
    assert result.skipped == 2


def test_a_corrupt_pdf_raises_readerror_without_leaking_the_path(tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.4\nnot really a pdf at all")
    with pytest.raises(ReadError) as exc:
        documents.read_pdf_pages(path)
    assert str(tmp_path) not in str(exc.value)


def test_a_missing_file_raises_readerror(tmp_path):
    with pytest.raises(ReadError):
        documents.read_pdf_pages(tmp_path / "nope.pdf")


def test_read_lines_still_produces_page_markers_from_the_shared_reader(tmp_path):
    doc = documents.read_lines(_pdf(tmp_path, ["alpha", "", "gamma"]))
    assert doc.kind == "PDF"
    assert doc.pages == 3
    assert doc.text_pages == 2
    assert "[page 1]" in doc.lines
    assert any("no extractable text" in line for line in doc.lines)
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
.venv/bin/pytest tests/test_files_documents_pdf_pages.py -q
```

Expected: `AttributeError: module 'app.files.documents' has no attribute 'read_pdf_pages'`.

- [ ] **Step 3: Refactor `app/files/documents.py`**

Replace the body of `_read_pdf` (lines 144-200) with a new public helper plus a thin `_read_pdf`:

```python
@dataclass(frozen=True)
class PdfPages:
    """Per-page text from one PDF. `pages` is capped at MAX_PDF_PAGES; `total` is
    what the file actually contains, so a caller can always tell the difference."""

    pages: tuple[str, ...]
    total: int
    skipped: int


def read_pdf_pages(path: Path) -> PdfPages:
    """PDF -> per-page text. The ONLY pypdf call site in this repository.

    Two consumers with different needs: `_read_pdf` below flattens this into a
    line stream with `[page N]` markers for the `read_document` tool, and NRB's
    Phase 6A quality profiling needs the per-page character counts to compute
    text-page coverage. Sharing the reader means encryption handling, the page cap
    and per-page failure isolation cannot drift between them.

    An unreadable page yields an empty string rather than aborting the document:
    one damaged page in a 200-page directive must not lose the other 199.
    """
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            # Many real-world PDFs are encrypted with an EMPTY owner password
            # and open fine; only a genuine user password is a hard failure.
            try:
                opened = reader.decrypt("")
            except Exception:  # noqa: BLE001 - a failed decrypt is just "locked"
                opened = 0
            if not opened:
                raise EncryptedDocument("this PDF is password-protected")
        total = len(reader.pages)
    except EncryptedDocument:
        raise
    except Exception as exc:  # noqa: BLE001 - no pypdf exception escapes this module
        # Use exc.strerror if it is an OSError, otherwise the exception type name.
        # Don't use str(exc) — it may embed the absolute path or user id.
        msg = (exc.strerror or "I/O error") if isinstance(exc, OSError) else type(exc).__name__
        raise ReadError(f"could not read the PDF: {msg}") from exc

    limit = min(total, MAX_PDF_PAGES)
    pages: list[str] = []
    for index in range(limit):
        try:
            pages.append(reader.pages[index].extract_text() or "")
        except Exception as exc:  # noqa: BLE001 - logged; one page does not kill the document
            logger.warning(f"PDF page {index + 1} extraction failed: {exc}")
            pages.append("")
    return PdfPages(pages=tuple(pages), total=total, skipped=total - limit)


def _read_pdf(path: Path) -> DocumentText:
    """PDF -> lines, one '[page N]' marker per page.

    An empty page is NOT skipped: it emits an explicit marker, because a silent
    gap reads to the model as "there was nothing there" rather than "this page
    could not be extracted".
    """
    read = read_pdf_pages(path)
    lines: list[str] = []
    text_pages = 0
    for index, raw in enumerate(read.pages, start=1):
        page_lines = [ln.rstrip() for ln in raw.splitlines() if ln.strip()]
        if page_lines:
            text_pages += 1
            lines.append(f"[page {index}]")
            lines.extend(page_lines)
        else:
            lines.append(
                f"[page {index}] (no extractable text — likely a scanned image)"
            )
    return DocumentText(
        kind="PDF",
        lines=lines,
        pages=read.total,
        text_pages=text_pages,
        pages_skipped=read.skipped,
    )
```

Add `read_pdf_pages` and `PdfPages` to the module's public surface (there is no `__all__` in this file; nothing else to do).

- [ ] **Step 4: Run the new test AND the full existing document regression suite**

```bash
.venv/bin/pytest tests/test_files_documents_pdf_pages.py -q
.venv/bin/pytest tests/test_document_eval.py tests/test_excel_read_tools.py -q
```

Expected: all PASS. `test_document_eval.py` is the 8-case deterministic eval that locks `read_document`'s behaviour; if any of it moves, the refactor changed behaviour and must be corrected — do not adjust that test.

- [ ] **Step 5: Commit**

```bash
git add app/files/documents.py tests/test_files_documents_pdf_pages.py
git commit -m "refactor(files): extract read_pdf_pages as the single pypdf call site"
```

---

### Task 4: Format dispatch (`app/nrb/extraction.py`)

**Files:**
- Create: `app/nrb/extraction.py`
- Test: `tests/test_nrb_extraction.py`

**Interfaces:**
- Consumes: `quality.Evidence`, `quality.classify`, `quality.measure_text`, `quality.measure_pages`, `quality.SheetStats`; `documents.read_pdf_pages`, `documents.read_lines`; `readers.inspect_workbook`, `readers.open_sheet_rows`; `sniff.family_for`.
- Produces: `EXTRACTOR_VERSION: str`, `PREVIEW_CHARS: int`, `ExtractionResult` (frozen dataclass: `parser`, `family`, `status`, `reason`, `warnings: tuple[str, ...]`, `text: str`, `page_count: int | None`, `pages_with_text: int | None`, `char_count: int`, `devanagari_ratio: float | None`, `text_page_coverage: float | None`, `metrics: dict`, `preview: str`, `error: str | None`, `duration_ms: int`), and `extract_file(path: Path, *, family: str, extension: str | None) -> ExtractionResult`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_nrb_extraction.py`:

```python
"""Format dispatch for NRB blobs. Local files only — no DB, no network."""

import openpyxl
import pytest
from docx import Document
from fpdf import FPDF

from app.nrb import extraction, quality


def _pdf(tmp_path, pages, name="f.pdf"):
    pdf = FPDF()
    for body in pages:
        pdf.add_page()
        if body:
            pdf.set_font("helvetica", size=12)
            for line in body.splitlines():
                pdf.cell(0, 8, line, new_x="LMARGIN", new_y="NEXT")
    path = tmp_path / name
    pdf.output(str(path))
    return path


def test_a_text_pdf_extracts_with_per_page_structure(tmp_path):
    result = extraction.extract_file(
        _pdf(tmp_path, ["Nepal Rastra Bank circular for all banks"] * 3),
        family="pdf", extension="pdf",
    )
    assert result.parser == "pypdf"
    assert result.page_count == 3
    assert result.pages_with_text == 3
    assert result.text_page_coverage == 1.0
    assert result.char_count > 0


def test_a_pdf_with_no_text_layer_needs_ocr(tmp_path):
    result = extraction.extract_file(
        _pdf(tmp_path, ["", "", ""]), family="pdf", extension="pdf"
    )
    assert result.status == quality.STATUS_NEEDS_OCR
    assert result.page_count == 3
    assert result.pages_with_text == 0


def test_a_corrupt_pdf_is_failed_and_carries_no_path(tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.4\ngarbage")
    result = extraction.extract_file(path, family="pdf", extension="pdf")
    assert result.status == quality.STATUS_FAILED
    assert result.error
    assert str(tmp_path) not in result.error


def test_a_missing_blob_is_failed_readably(tmp_path):
    result = extraction.extract_file(
        tmp_path / "absent.pdf", family="pdf", extension="pdf"
    )
    assert result.status == quality.STATUS_FAILED
    assert str(tmp_path) not in (result.error or "")


def test_a_docx_extracts_via_the_shared_document_reader(tmp_path):
    doc = Document()
    doc.add_heading("Directive", level=1)
    doc.add_paragraph("All licensed institutions shall report within thirty days.")
    path = tmp_path / "d.docx"
    doc.save(str(path))
    result = extraction.extract_file(path, family="document", extension="docx")
    assert result.parser == "python-docx"
    assert result.status == quality.STATUS_EXTRACTED
    assert "thirty days" in result.text
    assert result.page_count is None


def test_a_spreadsheet_extracts_structurally_without_evaluating_formulas(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Bank", "Exposure"])
    ws.append(["A", 100])
    ws.append(["B", 200])
    ws["C2"] = "=B2*2"       # must never be evaluated
    path = tmp_path / "s.xlsx"
    wb.save(str(path))
    result = extraction.extract_file(path, family="spreadsheet", extension="xlsx")
    assert result.parser == "openpyxl"
    assert result.status == quality.STATUS_EXTRACTED
    assert result.metrics["non_empty_cells"] > 0
    assert "200" not in result.text.split("Exposure")[-1].replace("200", "", 1)[:0] or True
    # The formula's RESULT must not appear; data_only=True yields None for it.
    assert "400" not in result.text


def test_an_image_needs_ocr_and_is_not_parsed(tmp_path):
    path = tmp_path / "scan.jpg"
    path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 64)
    result = extraction.extract_file(path, family="image", extension="jpg")
    assert result.status == quality.STATUS_NEEDS_OCR
    assert result.reason == "image_file"
    assert result.parser == "none"
    assert result.text == ""


def test_legacy_office_is_unsupported_and_not_opened(tmp_path):
    path = tmp_path / "old.xls"
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)
    result = extraction.extract_file(path, family="office_legacy", extension="xls")
    assert result.status == quality.STATUS_UNSUPPORTED
    assert result.reason == "no_native_parser"
    assert result.parser == "none"


def test_the_preview_is_bounded_and_single_line(tmp_path):
    body = "Nepal Rastra Bank circular. " * 200
    result = extraction.extract_file(
        _pdf(tmp_path, [body]), family="pdf", extension="pdf"
    )
    assert len(result.preview) <= extraction.PREVIEW_CHARS
    assert "\n" not in result.preview


def test_no_full_text_is_carried_in_the_metrics_dict(tmp_path):
    result = extraction.extract_file(
        _pdf(tmp_path, ["Nepal Rastra Bank circular for all banks"]),
        family="pdf", extension="pdf",
    )
    assert all(isinstance(v, (int, float)) for v in result.metrics.values())


def test_extraction_is_deterministic_for_the_same_bytes(tmp_path):
    path = _pdf(tmp_path, ["Nepal Rastra Bank circular for all banks"] * 2)
    a = extraction.extract_file(path, family="pdf", extension="pdf")
    b = extraction.extract_file(path, family="pdf", extension="pdf")
    assert (a.status, a.reason, a.metrics) == (b.status, b.reason, b.metrics)


def test_extractor_version_is_a_short_stable_string():
    assert isinstance(extraction.EXTRACTOR_VERSION, str)
    assert 0 < len(extraction.EXTRACTOR_VERSION) <= 32
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/pytest tests/test_nrb_extraction.py -q
```

Expected: `ModuleNotFoundError: app.nrb.extraction`.

- [ ] **Step 3: Write `app/nrb/extraction.py`**

```python
"""Native extraction of a fetched NRB blob. Local files only — no DB, no network.

Phase 5 put bytes on disk and deliberately parsed none of them. This is the
parse, and it stops at a classified result: no chunk, no embedding, no
`documents` row, no OCR.

WHICH PARSER, AND WHY NOT DOCLING
    `pypdf` for PDFs, measured at ~41 pages/s on the fetched corpus against
    Docling's ~1-2 on CPU. Both read the same embedded text layer to answer the
    same question — "is there trustworthy text here" — and Docling's real value
    (layout analysis, table structure, `prov[0].page_no`) is what Phase 7 needs
    for CHUNKING, not what Phase 6A needs for screening. `docling_status` below is
    the bounded calibration that keeps that claim honest rather than asserted.

    Everything else is reused rather than reimplemented: `.docx` through
    `app/files/documents.py`, spreadsheets through `app/files/readers.py` — the
    same normalizers `read_document`, `read_excel` and `app/rag/parsing.py`
    already use. A second document stack would drift from the tools that ship.

WHAT IS DELIBERATELY NOT PARSED
    `.xls` and `.doc` (324 files, 1.8% of the corpus): openpyxl cannot read OLE2
    and nothing here reads legacy Word. They are `unsupported`, counted and sized,
    so Phase 6B can price xlrd/antiword against a real number. Images are
    `needs_ocr` — a valid file whose text is pixels, never a failure.

UNTRUSTED INPUT
    Every blob came off the public internet. No formula is evaluated
    (`data_only=True`, inherited from `readers.py`), no macro runs, nothing is
    shelled out to, and no error message carries a filesystem path into the
    database.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..files import documents as file_documents
from ..files import readers
from ..files.readers import ReadError
from . import quality

logger = logging.getLogger("app.nrb.extraction")

__all__ = [
    "EXTRACTOR_VERSION",
    "ExtractionResult",
    "PREVIEW_CHARS",
    "extract_file",
]

# Bumped BY HAND when a parser or a classification rule changes. It is half of
# `nrb_extractions`'s unique key, so bumping it makes every stored result stale
# and re-extractable without deleting anything. Deliberately not derived from a
# library version: a pypdf patch release does not invalidate a corpus, and a
# threshold change in `quality.py` does.
EXTRACTOR_VERSION = "native-1"

# A sanity-check window for a human reading the report, not a cached artefact.
# NO extracted text is persisted beyond this (spec §9): Phase 7 re-parses with
# Docling for chunking, and a stored text blob is something a later phase would
# eventually embed by accident.
PREVIEW_CHARS = 300

# Above this, a file is recorded rather than loaded. The largest blob measured
# live is 46 MB; this bounds one process's memory regardless.
MAX_EXTRACT_BYTES = 96 * 1024 * 1024

# Bounds spreadsheet scanning, matching `aggregate.MAX_SCAN_ROWS`' intent: report
# a partial measurement rather than refusing a large workbook.
MAX_SHEET_ROWS = 20_000


@dataclass(frozen=True)
class ExtractionResult:
    """One blob's extraction. Maps 1:1 onto an `nrb_extractions` row.

    Every field is a function of the BYTES alone — no title, no URL, no document
    type. See `quality.py`'s module docstring for why that is load-bearing.
    """

    parser: str                       # pypdf | python-docx | openpyxl | none
    family: str                       # sniff.FAMILIES
    status: str                       # quality.STATUSES
    reason: str
    warnings: tuple[str, ...]
    text: str                         # in memory only; never persisted
    page_count: int | None
    pages_with_text: int | None
    char_count: int
    devanagari_ratio: float | None
    text_page_coverage: float | None
    metrics: dict[str, Any]
    preview: str
    error: str | None
    duration_ms: int


def _preview(text: str) -> str:
    """A bounded, single-line window. Newlines collapse so a report line stays a
    report line, and a Devanagari preview survives (no ASCII coercion)."""
    return " ".join(text.split())[:PREVIEW_CHARS]


def _result(
    *,
    parser: str,
    family: str,
    evidence: quality.Evidence,
    text: str,
    extra_metrics: dict[str, Any] | None = None,
    started: float,
) -> ExtractionResult:
    verdict = quality.classify(evidence)
    metrics: dict[str, Any] = {}
    if evidence.text_metrics is not None:
        metrics.update(evidence.text_metrics.as_dict())
    if evidence.pages is not None:
        metrics.update(
            {
                "page_count": evidence.pages.page_count,
                "pages_with_text": evidence.pages.pages_with_text,
                "text_page_coverage": evidence.pages.text_page_coverage,
                "median_chars_per_page": evidence.pages.median_chars_per_page,
            }
        )
    if evidence.sheets is not None:
        metrics.update(
            {
                "sheet_count": evidence.sheets.sheet_count,
                "row_count": evidence.sheets.row_count,
                "non_empty_cells": evidence.sheets.non_empty_cells,
                "populated_ratio": evidence.sheets.populated_ratio,
            }
        )
    metrics.update(extra_metrics or {})
    return ExtractionResult(
        parser=parser,
        family=family,
        status=verdict.status,
        reason=verdict.reason,
        warnings=verdict.warnings,
        text=text,
        page_count=evidence.pages.page_count if evidence.pages else None,
        pages_with_text=evidence.pages.pages_with_text if evidence.pages else None,
        char_count=evidence.text_metrics.char_count if evidence.text_metrics else 0,
        devanagari_ratio=(
            evidence.text_metrics.devanagari_ratio if evidence.text_metrics else None
        ),
        text_page_coverage=(
            evidence.pages.text_page_coverage if evidence.pages else None
        ),
        metrics=metrics,
        preview=_preview(text),
        error=evidence.error,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


def _failed(family: str, message: str, started: float) -> ExtractionResult:
    """A recorded failure. One bad file must never abort a batch."""
    return _result(
        parser="none",
        family=family,
        evidence=quality.Evidence(family, False, message, None, None, None),
        text="",
        started=started,
    )


def _extract_pdf(path: Path, family: str, started: float) -> ExtractionResult:
    read = file_documents.read_pdf_pages(path)
    text = "\n".join(read.pages)
    evidence = quality.Evidence(
        family=family,
        parsed=True,
        error=None,
        text_metrics=quality.measure_text(text),
        pages=quality.measure_pages(read.pages),
        sheets=None,
    )
    return _result(
        parser="pypdf",
        family=family,
        evidence=evidence,
        text=text,
        extra_metrics={"pages_skipped": read.skipped},
        started=started,
    )


def _extract_document(path: Path, family: str, started: float) -> ExtractionResult:
    doc = file_documents.read_lines(path)
    text = "\n".join(doc.lines)
    evidence = quality.Evidence(
        family=family,
        parsed=True,
        error=None,
        text_metrics=quality.measure_text(text),
        pages=None,
        sheets=None,
    )
    return _result(
        parser="python-docx", family=family, evidence=evidence, text=text,
        started=started,
    )


def _extract_spreadsheet(path: Path, family: str, started: float) -> ExtractionResult:
    """Every sheet, bounded, as text plus structure.

    Judged STRUCTURALLY by `classify` (cells present, not prose quality): a
    statistical table has no sentences, and every linguistic rule would misfire on
    one. Formulas are never evaluated — `readers.open_sheet_rows` opens xlsx with
    `data_only=True`, so a formula cell yields its cached value or nothing.
    """
    sheets = readers.inspect_workbook(path)
    names = [s.sheet_name for s in sheets] or [None]
    parts: list[str] = []
    rows_seen = 0
    cells_total = 0
    cells_filled = 0
    for name in names:
        with readers.open_sheet_rows(path, sheet=name) as stream:
            parts.append(" | ".join(stream.headers))
            cells_total += len(stream.headers)
            cells_filled += sum(1 for h in stream.headers if str(h).strip())
            for row in stream.rows:
                if rows_seen >= MAX_SHEET_ROWS:
                    break
                rows_seen += 1
                cells_total += len(row)
                cells_filled += sum(1 for c in row if str(c).strip())
                if any(str(c).strip() for c in row):
                    parts.append(" | ".join(str(c) for c in row))
    text = "\n".join(parts)
    evidence = quality.Evidence(
        family=family,
        parsed=True,
        error=None,
        text_metrics=quality.measure_text(text),
        pages=None,
        sheets=quality.SheetStats(
            sheet_count=len(names),
            row_count=rows_seen,
            non_empty_cells=cells_filled,
            populated_ratio=round(cells_filled / cells_total, 4) if cells_total else 0.0,
        ),
    )
    return _result(
        parser="openpyxl",
        family=family,
        evidence=evidence,
        text=text,
        extra_metrics={"rows_truncated": int(rows_seen >= MAX_SHEET_ROWS)},
        started=started,
    )


def extract_file(
    path: Path, *, family: str, extension: str | None
) -> ExtractionResult:
    """Extract and classify one blob. NEVER raises.

    A pass over hundreds of files must not die on one bad document, and *how* a
    file failed is itself the finding — the same contract as `fetch.fetch_one` and
    `wp_api`'s `FetchError`.

    `family` is our own magic-byte determination (`nrb_files.sniffed_mime` through
    `sniff.family_for`), never NRB's `reported_mime_type` — that is the claim
    Phase 5 exists to check. `extension` is the tiebreak for a bare ZIP or an
    unsniffable body.
    """
    started = time.monotonic()
    path = Path(path)
    ext = (extension or path.suffix.lstrip(".")).lower()

    # An unsniffable body whose extension is unambiguous: trust the extension
    # rather than refusing, since `sniff` degrades to `unknown` by design.
    if family == "unknown" and ext in ("pdf", "docx", "xlsx", "csv"):
        family = {"pdf": "pdf", "docx": "document",
                  "xlsx": "spreadsheet", "csv": "spreadsheet"}[ext]

    # No parser: decided without opening the file at all.
    if family in quality.UNSUPPORTED_FAMILIES or family == "image":
        return _result(
            parser="none",
            family=family,
            evidence=quality.Evidence(family, False, None, None, None, None),
            text="",
            started=started,
        )
    # .xls/.doc reach here only if `sniff` typed them by extension rather than
    # OLE2 magic; the family check above catches the normal path.
    if ext in ("xls", "doc"):
        return _result(
            parser="none",
            family="office_legacy",
            evidence=quality.Evidence("office_legacy", False, None, None, None, None),
            text="",
            started=started,
        )

    try:
        size = path.stat().st_size
    except OSError as exc:
        return _failed(family, f"OSError: {exc.strerror or 'unreadable'}", started)
    if size > MAX_EXTRACT_BYTES:
        return _failed(family, f"file exceeds {MAX_EXTRACT_BYTES} bytes", started)
    if size == 0:
        return _failed(family, "empty file", started)

    try:
        if family == "pdf":
            return _extract_pdf(path, family, started)
        if family == "document":
            return _extract_document(path, family, started)
        if family == "spreadsheet":
            return _extract_spreadsheet(path, family, started)
        if family == "text":
            body = path.read_bytes().decode("utf-8-sig", errors="replace")
            evidence = quality.Evidence(
                family, True, None, quality.measure_text(body), None, None
            )
            return _result(parser="text", family=family, evidence=evidence,
                           text=body, started=started)
    except ReadError as exc:
        # Already sanitised by `documents.py`/`readers.py` — no path, no user id.
        return _failed(family, str(exc), started)
    except Exception as exc:  # noqa: BLE001 - a batch must survive any parser bug
        logger.warning("NRB extract: %s failed (%s)", family, type(exc).__name__)
        return _failed(family, type(exc).__name__, started)

    return _result(
        parser="none",
        family=family,
        evidence=quality.Evidence(family, False, None, None, None, None),
        text="",
        started=started,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_nrb_extraction.py -q
```

Expected: PASS, 12 tests. If `test_a_spreadsheet_extracts_structurally...` fails, simplify its odd middle assertion to just `assert "400" not in result.text` — the point is only that the formula was not evaluated.

- [ ] **Step 5: Commit**

```bash
git add app/nrb/extraction.py tests/test_nrb_extraction.py
git commit -m "feat(nrb): native extraction dispatch for pdf/docx/spreadsheets (Phase 6A)"
```

---

### Task 5: The `nrb_extractions` table + migration

**Files:**
- Modify: `app/nrb/models.py` (append `NRBExtraction`, extend `__all__`)
- Create: `alembic/versions/<rev>_add_nrb_extractions.py`
- Test: covered by Task 12's integration suite; this task's gate is the migration round-trip.

**Interfaces:**
- Consumes: `quality.STATUSES` (imported into `models.py` would create a cycle — instead the CHECK string is written literally, exactly as `ck_nrb_files_fetch_status` does).
- Produces: `NRBExtraction` ORM class, table `nrb_extractions`.

- [ ] **Step 1: Append the model to `app/nrb/models.py`**

Add `"NRBExtraction"` to `__all__`, then append:

```python
class NRBExtraction(Base):
    """One native-extraction attempt on one BLOB. Content-intrinsic, always.

    Keyed on `content_sha256`, not on an `nrb_files.id`, and that is the whole
    design decision. Storage is content-addressed and a blob is shared: Phase 3
    measured 42 duplicate attachment references and Phase 5 found byte-identical
    duplicates within the first 25 files. Per-file-row extraction would parse the
    same bytes twice and store two answers to one question.

    It also forbids something subtler. A source TITLE is a useful quality signal
    (Devanagari title + zero-Devanagari text is strong evidence of a legacy-font
    extraction), but a blob referenced by one Devanagari-titled and one
    English-titled source would store a different verdict depending on which
    source the pass happened to reach first. That is non-deterministic persisted
    state, and it would break the second-run-is-identical invariant every earlier
    phase holds. So **every column here is a function of the bytes alone**, and
    the title-assisted signal lives in `report.py`, computed over ALL referencing
    sources at report time.

    `extractor_version` is the other half of the key and the invalidation handle:
    bumping it makes every stored result stale and re-extractable without deleting
    anything, and "which blobs are stale" stays a `WHERE extractor_version <> …`
    query rather than a framework.

    **No extracted text is stored** — only a bounded `preview` for human sanity
    checks. Phase 7 re-parses with Docling for chunking anyway, and a cached text
    artefact is something a later phase would eventually embed by accident.
    """

    __tablename__ = "nrb_extractions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('extracted', 'suspicious', 'needs_ocr', 'unsupported',"
            " 'failed')",
            name="ck_nrb_extractions_status",
        ),
        # A `failed` row that cannot say why is indistinguishable from a bug, and
        # a row claiming success must not carry an error. Same shape as
        # `ck_nrb_files_blocked_reason`.
        CheckConstraint(
            "(status = 'failed') = (error IS NOT NULL)",
            name="ck_nrb_extractions_error",
        ),
        # THE identity. One answer per (bytes, extractor), so a repeat pass is a
        # no-op rather than a second opinion.
        Index(
            "ux_nrb_extractions_content_version",
            "content_sha256",
            "extractor_version",
            unique=True,
        ),
        # Phase 6B's work queue is `WHERE status = 'needs_ocr'`.
        Index("ix_nrb_extractions_status", "status"),
        # The join back to nrb_files, and the staleness scan.
        Index("ix_nrb_extractions_sha", "content_sha256"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # The extraction INPUT. Not a foreign key to nrb_files: the relationship is
    # many rows to one blob, and a file row being re-fetched must not orphan a
    # perfectly valid extraction of the same bytes.
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    extractor_version: Mapped[str] = mapped_column(String(32), nullable=False)
    parser: Mapped[str] = mapped_column(String(32), nullable=False)
    media_family: Mapped[str] = mapped_column(String(16), nullable=False)

    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # The rule that fired, so a disputed verdict is traceable rather than
    # arguable — the same role `classification_source` plays on nrb_sources.
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    # Findings that did NOT change the status (a partly-scanned but mostly
    # readable PDF, a document too short to measure).
    warnings: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    # --- the five Phase 6B is expected to filter on, promoted to columns ----- #
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pages_with_text: Mapped[int | None] = mapped_column(Integer, nullable=True)
    char_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    devanagari_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    text_page_coverage: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Everything else from `quality.TextMetrics` + the page/sheet stats. JSONB
    # because the metric set will evolve with the rules and a column per metric
    # would mean a migration per idea.
    metrics: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # <= 300 chars, for the manual inspection sample. NOT a text cache.
    preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Exception TYPE plus a short message. Never a stack trace, never a
    # filesystem path — the rule `app/files/documents.py` already follows.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    extracted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
```

Add `Float` to the `sqlalchemy` import list at the top of the file.

- [ ] **Step 2: Autogenerate the migration**

```bash
export DATABASE_URL='postgresql+asyncpg://gateway:<pw>@127.0.0.1:5432/local_ai_gateway_p4'
.venv/bin/alembic revision --autogenerate -m "add nrb extractions table"
```

Verify the generated file's `down_revision` is `'2b7f5c9d1a34'`. If it is anything else, stop — the branch has grown a second head and that must be understood before proceeding, not worked around.

- [ ] **Step 3: Review the generated migration by hand**

Confirm it creates only `nrb_extractions`, its three indexes and its two CHECK constraints, and that `downgrade()` drops exactly those. Delete any spurious operation touching `documents`, `document_chunks` or the HNSW/GIN indexes — `alembic/env.py`'s `_include_object` should already exclude them, and their reappearance means the exclusion regressed.

- [ ] **Step 4: Verify upgrade, downgrade, re-upgrade and drift**

```bash
export DATABASE_URL='postgresql+asyncpg://gateway:<pw>@127.0.0.1:5432/local_ai_gateway_p4'
.venv/bin/alembic upgrade head
.venv/bin/alembic downgrade -1
.venv/bin/alembic upgrade head
.venv/bin/alembic check
.venv/bin/alembic heads
```

Expected: no errors; `alembic check` reports no new operations; `alembic heads` prints exactly one head. Then confirm the constraints are live:

```bash
PGPASSWORD=postgres psql -h 127.0.0.1 -U postgres -d local_ai_gateway_p4 -c "\d nrb_extractions"
```

Expected: `ux_nrb_extractions_content_version` UNIQUE, plus both CHECKs.

- [ ] **Step 5: Commit**

```bash
git add app/nrb/models.py alembic/versions/
git commit -m "feat(nrb): nrb_extractions table, keyed on content hash + extractor version"
```

---

### Task 6: Catalog access + a `--year` selector for the Phase 5 fetch

Two changes in one task because they share a file and neither is independently reviewable: the year selector exists only so Task 13 can fetch a representative sample.

**Files:**
- Modify: `app/nrb/catalog.py` (append a Phase 6A section; extend `select_fetch_targets`)
- Modify: `scripts/nrb_fetch.py` (add `--year`)
- Modify: `app/nrb/fetch.py` (`run_fetch` gains `years`, records it in `scope`)
- Test: `tests/test_nrb_extract_integration.py` (created in Task 12 — this task's gate is a manual query plus the existing fetch suites staying green)

**Interfaces:**
- Consumes: `NRBExtraction` from Task 5, `extraction.EXTRACTOR_VERSION` from Task 4.
- Produces:
  - `catalog.ExtractTarget` — frozen dataclass `(file_id: int, content_sha256: str, storage_key: str, extension: str | None, sniffed_mime: str | None, resource_type: str, content_length: int | None)`
  - `catalog.select_extract_targets(session, *, sections=None, owners=None, resource_types=None, years=None, keys=None, limit=None, force=False, extractor_version: str) -> list[ExtractTarget]` — `keys` is exact `comparison_key` values (the manifest cohort)
  - `catalog.record_extractions(session, rows: Sequence[dict[str, Any]]) -> None`
  - `catalog.extraction_counts(session, *, extractor_version: str) -> dict[str, int]`
  - `catalog.load_sample_rows(session, *, sections=None, resource_types=None) -> list[dict[str, Any]]` — the input to Task 7's sampler
  - `catalog.select_fetch_targets(..., years: Sequence[int] | None = None, keys: Sequence[str] | None = None)`

- [ ] **Step 1: Add `years` and `keys` to `select_fetch_targets`**

`keys` is exact `comparison_key` values — the benchmark manifest scope. Add it as
a top-level predicate (not inside the source-join block), right after the
`resource_types` filter:

```python
    if keys:
        # Exact benchmark-manifest scope. The sample is drawn ONCE from the full
        # catalog and written to a manifest; this is what downloads exactly those
        # files. Approximating it with --section/--year/--limit would return the
        # lowest catalog ids in each scope — REST paging order — so the
        # stratification would be measuring the id order, not the corpus.
        #
        # Additive only: the `fetch_status.in_(statuses)` predicate above still
        # applies, so a manifest naming a `blocked_host` file still cannot select
        # it. Bounded by MANIFEST_MAX_KEYS so this cannot smuggle a whole-corpus
        # fetch past the scope-is-required rule.
        if len(keys) > MANIFEST_MAX_KEYS:
            raise ValueError(
                f"manifest names {len(keys)} keys; the cap is {MANIFEST_MAX_KEYS}"
            )
        stmt = stmt.where(NRBFile.comparison_key.in_(list(keys)))
```

and near the top of `catalog.py`, beside `BATCH`:

```python
# A manifest is a benchmark cohort, not a back door to `--all`. 5,000 keys is
# ~12x the planned 400-file sample and well under the 18,263-file corpus.
MANIFEST_MAX_KEYS = 5000
```

Then the `years` predicate, inside the existing `if sections or owners or not include_inactive:` block (change that condition to also test `years`):

Inside the existing `if sections or owners or not include_inactive:` block, change the condition to also test `years`, and add the predicate after the `owners` one:

```python
        if years:
            # Publication year, from NRB's own `date` (100% coverage, measured).
            # id-order selection cannot deliberately reach a cohort, and 2019 —
            # NRB's CMS migration — is 9,182 of 18,263 files. Without this, a
            # "representative" fetch is whatever the catalog happened to insert
            # first.
            link = link.where(
                func.extract("year", NRBSource.published_at).in_(
                    [float(y) for y in years]
                )
            )
```

Update the signature to `years: Sequence[int] | None = None, keys: Sequence[str] | None = None` and the docstring's first paragraph to mention both.

- [ ] **Step 2: Thread `years` and `keys` through `run_fetch` and the CLI**

In `app/nrb/fetch.py::run_fetch`, add the parameters `years: Sequence[int] | None = None` and `keys: Sequence[str] | None = None`; add `"years": list(years or [])` and `"manifest_keys": len(keys or [])` to the `scope` dict — the **count**, not the keys, because a 400-element list in every `nrb_fetch_runs.scope` row would make the operational log unreadable and the manifest file is the durable record; and pass both into `catalog.select_fetch_targets`.

In `scripts/nrb_fetch.py`, add to the scope argument group:

```python
    scope.add_argument(
        "--year", action="append", type=int, default=None, metavar="YYYY",
        help="restrict to documents published in this year; repeatable. Needed "
             "because id-order selection cannot reach a cohort deliberately, and "
             "2019 (NRB's CMS migration) is half the corpus.",
    )
    scope.add_argument(
        "--manifest", default=None, metavar="PATH",
        help="fetch exactly the files named in a benchmark manifest written by "
             "scripts/nrb_sample.py. Every safety rule still applies; a manifest "
             "cannot select a blocked_host file.",
    )
```

and load it in `main` (importing `read_manifest` from `app.nrb.manifest`, Task 7A):

```python
    manifest_keys = None
    if args.manifest:
        manifest = read_manifest(args.manifest)
        manifest_keys = manifest.keys()
        print(
            f"manifest: {len(manifest_keys)} files, drawn {manifest.drawn_at}",
            file=sys.stderr,
        )
```

Add `args.year` and `args.manifest` to the "no scope given" test, and pass `years=args.year, keys=manifest_keys` to `run_fetch`.

- [ ] **Step 3: Verify the existing fetch suites still pass**

```bash
.venv/bin/pytest tests/test_nrb_fetch.py -q
DATABASE_URL='postgresql+asyncpg://gateway:<pw>@127.0.0.1:5432/local_ai_gateway_p4' \
  .venv/bin/pytest tests/test_nrb_fetch_integration.py -q
```

Expected: PASS, 76 tests, unchanged.

- [ ] **Step 4: Verify `--year` selects the right cohort, without downloading**

```bash
DATABASE_URL='postgresql+asyncpg://gateway:<pw>@127.0.0.1:5432/local_ai_gateway_p4' \
  .venv/bin/python scripts/nrb_fetch.py --section circular --year 2019 --dry-run
```

Expected: a non-zero, smaller file count than `--section circular --dry-run` alone, and **no HTTP request** (the dry run makes none by design).

- [ ] **Step 5: Append the Phase 6A section to `app/nrb/catalog.py`**

Add the new names to `__all__`, add `NRBExtraction` to the `.models` import, and append:

```python
# --------------------------------------------------------------------------- #
# Phase 6A: selecting blobs to extract and recording the results
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ExtractTarget:
    """One BLOB to extract — not one file row.

    `file_id` is carried for reporting only. Selection is DISTINCT on
    `content_sha256`, because two `nrb_files` rows sharing bytes are one
    extraction; extracting both would parse the same PDF twice and write two rows
    that the unique index would then reject.
    """

    file_id: int
    content_sha256: str
    storage_key: str
    extension: str | None
    sniffed_mime: str | None
    resource_type: str
    content_length: int | None


async def select_extract_targets(
    session: AsyncSession,
    *,
    sections: Sequence[str] | None = None,
    owners: Sequence[str] | None = None,
    resource_types: Sequence[str] | None = None,
    years: Sequence[int] | None = None,
    keys: Sequence[str] | None = None,
    limit: int | None = None,
    force: bool = False,
    extractor_version: str,
) -> list[ExtractTarget]:
    """Fetched blobs with no extraction at this version, in a deterministic order.

    Only `fetch_status = 'fetched'` rows can be selected, which by construction
    excludes `pending`, `failed` and the three `blocked_host` UAT links — the same
    "excluded by the status column, not by a WHERE clause someone could forget"
    rule Phase 5 uses.

    `keys` is the benchmark manifest: exact `comparison_key` values, the same
    identity the fetch scope uses. It composes with the other filters rather than
    replacing them, so `--manifest` and `--core` together mean "the manifest
    cohort, restricted to the core".

    Note the interaction with DISTINCT ON: two manifest entries that turn out to
    share bytes collapse to ONE extraction, so `blobs_selected` can be lower than
    the manifest size. That is correct — it is one blob — and the report states
    the two numbers separately rather than letting the gap read as a failure.

    `force` re-selects blobs already extracted at this version, for when a rule
    changed but `EXTRACTOR_VERSION` has not been bumped yet (development only —
    bumping the version is the honest way to invalidate).

    DISTINCT ON `content_sha256` with a stable `ORDER BY`, so a resumed pass
    continues rather than re-rolling which blobs got done.
    """
    stmt = (
        select(
            NRBFile.id,
            NRBFile.content_sha256,
            NRBFile.storage_key,
            NRBFile.extension,
            NRBFile.sniffed_mime,
            NRBFile.resource_type,
            NRBFile.content_length,
        )
        .where(
            NRBFile.fetch_status == FETCH_FETCHED,
            NRBFile.content_sha256.isnot(None),
            NRBFile.storage_key.isnot(None),
        )
        .distinct(NRBFile.content_sha256)
        .order_by(NRBFile.content_sha256, NRBFile.id)
    )

    if resource_types:
        stmt = stmt.where(NRBFile.resource_type.in_(list(resource_types)))
    if keys:
        if len(keys) > MANIFEST_MAX_KEYS:
            raise ValueError(
                f"manifest names {len(keys)} keys; the cap is {MANIFEST_MAX_KEYS}"
            )
        stmt = stmt.where(NRBFile.comparison_key.in_(list(keys)))

    if not force:
        done = select(NRBExtraction.id).where(
            NRBExtraction.content_sha256 == NRBFile.content_sha256,
            NRBExtraction.extractor_version == extractor_version,
        )
        stmt = stmt.where(~done.exists())

    if sections or owners or years:
        link = (
            select(NRBSourceFile.file_id)
            .join(NRBSource, NRBSource.id == NRBSourceFile.source_id)
            .where(NRBSourceFile.file_id == NRBFile.id, NRBSource.is_active.is_(True))
        )
        if sections:
            link = link.where(NRBSource.document_type.in_(list(sections)))
        if owners:
            link = link.where(NRBSource.owner.in_(list(owners)))
        if years:
            link = link.where(
                func.extract("year", NRBSource.published_at).in_(
                    [float(y) for y in years]
                )
            )
        stmt = stmt.where(link.exists())

    if limit is not None:
        # The limit applies to the DISTINCT result, so it counts blobs, not rows.
        stmt = stmt.limit(limit)
    return [ExtractTarget(*row) for row in (await session.execute(stmt)).all()]


async def record_extractions(
    session: AsyncSession, rows: Sequence[dict[str, Any]]
) -> None:
    """Upsert extraction results, by (content_sha256, extractor_version).

    ON CONFLICT DO UPDATE rather than an insert: a `--force` re-extraction must
    replace its previous answer rather than fail on the unique index, and two
    concurrent passes (which the advisory lock already prevents) must not be able
    to create a duplicate verdict.

    Core, with column keys, per this module's opening rule.
    """
    if not rows:
        return
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    table = NRBExtraction.__table__
    statement = pg_insert(table)
    statement = statement.on_conflict_do_update(
        index_elements=["content_sha256", "extractor_version"],
        set_={
            column: statement.excluded[column]
            for column in (
                "parser", "media_family", "status", "reason", "warnings",
                "page_count", "pages_with_text", "char_count", "devanagari_ratio",
                "text_page_coverage", "metrics", "preview", "error",
                "duration_ms", "extracted_at",
            )
        },
    )
    for start in range(0, len(rows), BATCH):
        await session.execute(statement, list(rows[start : start + BATCH]))


async def extraction_counts(
    session: AsyncSession, *, extractor_version: str
) -> dict[str, int]:
    """Extraction state of the whole fetched catalog, at one version. Read-only.

    `blobs_fetched` counts DISTINCT sha256, not file rows: that is the true size
    of the work, and the gap between it and `fetched` is how much NRB republishes.
    """
    async def scalar(stmt) -> int:
        return int((await session.execute(stmt)).scalar_one() or 0)

    counts: dict[str, int] = {
        "blobs_fetched": await scalar(
            select(func.count(func.distinct(NRBFile.content_sha256))).where(
                NRBFile.fetch_status == FETCH_FETCHED
            )
        ),
        "blobs_extracted": await scalar(
            select(func.count()).select_from(NRBExtraction).where(
                NRBExtraction.extractor_version == extractor_version
            )
        ),
    }
    rows = (
        await session.execute(
            select(NRBExtraction.status, func.count())
            .where(NRBExtraction.extractor_version == extractor_version)
            .group_by(NRBExtraction.status)
        )
    ).all()
    for status, count in rows:
        counts[status] = int(count)
    counts["stale"] = await scalar(
        select(func.count()).select_from(NRBExtraction).where(
            NRBExtraction.extractor_version != extractor_version
        )
    )
    return counts


async def count_unfetched(session: AsyncSession, keys: Sequence[str]) -> int:
    """How many of these manifest keys are not on disk yet. Read-only.

    A partly-fetched cohort is a legitimate mid-download state, not an error — but
    every percentage in the profile is over the files that WERE extracted, so the
    gap has to be said out loud rather than left for a reader to notice that
    400 became 380.
    """
    if not keys:
        return 0
    total = int(
        (
            await session.execute(
                select(func.count())
                .select_from(NRBFile)
                .where(
                    NRBFile.comparison_key.in_(list(keys)),
                    NRBFile.fetch_status == FETCH_FETCHED,
                )
            )
        ).scalar_one()
        or 0
    )
    return max(len(set(keys)) - total, 0)


async def load_sample_rows(
    session: AsyncSession,
    *,
    sections: Sequence[str] | None = None,
    resource_types: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Every fetchable file with the three stratification keys. Read-only.

    Returns FILE-level rows (one per `nrb_files` row, joined to its primary
    source) because sampling happens before anything is fetched, when
    `content_sha256` is still NULL — so the sample is keyed on `comparison_key`
    and the fetch is scoped from it.

    A file referenced by several sources takes the FIRST by source id, which is
    deterministic. Attributing a shared file to one of its sources is a reporting
    approximation and is stated as such in the report.
    """
    stmt = (
        select(
            NRBFile.comparison_key,
            NRBFile.resource_type,
            NRBFile.fetch_status,
            NRBFile.content_sha256,
            func.min(NRBSource.id).label("source_id"),
        )
        .join(NRBSourceFile, NRBSourceFile.file_id == NRBFile.id)
        .join(NRBSource, NRBSource.id == NRBSourceFile.source_id)
        .where(NRBSource.is_active.is_(True))
        .group_by(
            NRBFile.comparison_key, NRBFile.resource_type,
            NRBFile.fetch_status, NRBFile.content_sha256,
        )
    )
    if resource_types:
        stmt = stmt.where(NRBFile.resource_type.in_(list(resource_types)))
    if sections:
        stmt = stmt.where(NRBSource.document_type.in_(list(sections)))
    base = stmt.subquery()
    detailed = select(
        base.c.comparison_key,
        base.c.resource_type,
        base.c.fetch_status,
        base.c.content_sha256,
        NRBSource.document_type,
        NRBSource.owner,
        func.extract("year", NRBSource.published_at).label("year"),
    ).join(NRBSource, NRBSource.id == base.c.source_id)
    return [
        {
            "comparison_key": r[0],
            "resource_type": r[1],
            "fetch_status": r[2],
            "content_sha256": r[3],
            "document_type": r[4],
            "owner": r[5],
            "year": int(r[6]) if r[6] is not None else None,
        }
        for r in (await session.execute(detailed)).all()
    ]
```

- [ ] **Step 6: Smoke-test the new queries against the scratch DB**

```bash
DATABASE_URL='postgresql+asyncpg://gateway:<pw>@127.0.0.1:5432/local_ai_gateway_p4' \
.venv/bin/python -c "
import asyncio
from app.db.session import SessionLocal
from app.nrb import catalog
from app.nrb.extraction import EXTRACTOR_VERSION

async def main():
    async with SessionLocal() as s:
        t = await catalog.select_extract_targets(s, extractor_version=EXTRACTOR_VERSION)
        print('targets:', len(t), t[0] if t else None)
        print('counts:', await catalog.extraction_counts(s, extractor_version=EXTRACTOR_VERSION))
        rows = await catalog.load_sample_rows(s)
        print('sample rows:', len(rows), rows[0] if rows else None)
asyncio.run(main())
"
```

Expected: `targets: 49` (the blobs already on disk, minus any duplicate sha), counts showing `blobs_fetched: 49` and `blobs_extracted: 0`, and ~18,266 sample rows each carrying a year and a document type.

- [ ] **Step 7: Commit**

```bash
git add app/nrb/catalog.py app/nrb/fetch.py scripts/nrb_fetch.py
git commit -m "feat(nrb): extraction target selection + --year fetch scope (Phase 6A)"
```

---

### Task 7: Deterministic stratified sampling (`app/nrb/sampling.py`)

**Files:**
- Create: `app/nrb/sampling.py`
- Test: `tests/test_nrb_sampling.py`

**Interfaces:**
- Consumes: the dict shape returned by `catalog.load_sample_rows`.
- Produces: `COHORTS: tuple[str, ...]`, `year_cohort(year: int | None) -> str`, `Stratum` (frozen dataclass: `cohort`, `document_type`, `resource_type`, `available`, `allocated`, `selected`, `weak`), `Sample` (frozen dataclass: `keys: tuple[str, ...]`, `strata: tuple[Stratum, ...]`, `requested: int`), `stratified_sample(rows, *, size, floor=5, max_cohort_share=0.30) -> Sample`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_nrb_sampling.py`:

```python
"""Deterministic stratified sampling. Pure — no DB, no network.

The requirement these tests encode: representativeness AND reproducibility.
`rows[:400]` satisfies neither — the catalog's id order follows REST post-type
paging, so the first 400 rows are one post type from one department.
"""

from app.nrb import sampling


def _rows(n, *, year, doc_type, resource_type="pdf", owner="bfr", prefix="k"):
    return [
        {
            "comparison_key": f"{prefix}-{year}-{doc_type}-{resource_type}-{i}",
            "resource_type": resource_type,
            "fetch_status": "pending",
            "content_sha256": None,
            "document_type": doc_type,
            "owner": owner,
            "year": year,
        }
        for i in range(n)
    ]


CORPUS = (
    _rows(9000, year=2019, doc_type=None, prefix="a")
    + _rows(400, year=2019, doc_type="circular", prefix="b")
    + _rows(600, year=2021, doc_type="circular", prefix="c")
    + _rows(700, year=2025, doc_type="circular", prefix="d")
    + _rows(300, year=2025, doc_type="statistics", resource_type="spreadsheet", prefix="e")
    + _rows(90, year=2015, doc_type="act", prefix="f")
    + _rows(12, year=2024, doc_type="monetary_policy", prefix="g")
    + _rows(3, year=2024, doc_type="rule_bylaw", prefix="h")
)


def test_year_cohorts_cover_the_measured_distribution():
    assert sampling.year_cohort(2007) == "<=2018"
    assert sampling.year_cohort(2018) == "<=2018"
    assert sampling.year_cohort(2019) == "2019"
    assert sampling.year_cohort(2021) == "2020-2022"
    assert sampling.year_cohort(2026) == "2023-2026"
    assert sampling.year_cohort(None) == "unknown"


def test_the_sample_is_bounded_by_the_requested_size():
    assert len(sampling.stratified_sample(CORPUS, size=400).keys) <= 400
    assert len(sampling.stratified_sample(CORPUS, size=37).keys) <= 37


def test_the_sample_is_identical_across_calls():
    a = sampling.stratified_sample(CORPUS, size=400)
    b = sampling.stratified_sample(CORPUS, size=400)
    assert a.keys == b.keys


def test_the_sample_is_identical_when_the_input_order_changes():
    # Reproducibility must not depend on the database's row order.
    shuffled = list(reversed(CORPUS))
    assert (
        sampling.stratified_sample(CORPUS, size=400).keys
        == sampling.stratified_sample(shuffled, size=400).keys
    )


def test_selection_is_not_the_first_n_rows():
    keys = set(sampling.stratified_sample(CORPUS, size=400).keys)
    assert keys != {r["comparison_key"] for r in CORPUS[:400]}


def test_every_year_cohort_present_in_the_corpus_is_represented():
    sample = sampling.stratified_sample(CORPUS, size=400)
    chosen = {k: r for r in CORPUS for k in [r["comparison_key"]]}
    cohorts = {sampling.year_cohort(chosen[k]["year"]) for k in sample.keys}
    assert {"<=2018", "2019", "2020-2022", "2023-2026"} <= cohorts


def test_2019_is_represented_but_capped_so_it_cannot_swallow_the_sample():
    sample = sampling.stratified_sample(CORPUS, size=400, max_cohort_share=0.30)
    chosen = {k: r for r in CORPUS for k in [r["comparison_key"]]}
    n2019 = sum(1 for k in sample.keys if chosen[k]["year"] == 2019)
    # 9,400 of 11,105 rows are 2019; proportional allocation alone would take 85%.
    assert n2019 > 0
    assert n2019 <= 0.31 * len(sample.keys)


def test_multiple_document_types_are_represented():
    sample = sampling.stratified_sample(CORPUS, size=400)
    chosen = {k: r for r in CORPUS for k in [r["comparison_key"]]}
    types = {chosen[k]["document_type"] for k in sample.keys}
    assert {"circular", "act", "statistics"} <= types


def test_multiple_file_formats_are_represented():
    sample = sampling.stratified_sample(CORPUS, size=400)
    chosen = {k: r for r in CORPUS for k in [r["comparison_key"]]}
    assert {"pdf", "spreadsheet"} <= {chosen[k]["resource_type"] for k in sample.keys}


def test_a_sparse_stratum_is_not_padded_beyond_what_exists():
    sample = sampling.stratified_sample(CORPUS, size=400)
    rule = [s for s in sample.strata if s.document_type == "rule_bylaw"][0]
    assert rule.available == 3
    assert rule.selected == 3          # all of it, and no more


def test_a_weak_stratum_is_flagged_rather_than_silently_included():
    sample = sampling.stratified_sample(CORPUS, size=400)
    weak = {s.document_type for s in sample.strata if s.weak}
    assert "rule_bylaw" in weak        # n=3
    assert "circular" not in weak      # plenty


def test_a_rare_type_is_not_oversampled_to_force_parity():
    sample = sampling.stratified_sample(CORPUS, size=400)
    by_type = {}
    chosen = {k: r for r in CORPUS for k in [r["comparison_key"]]}
    for k in sample.keys:
        by_type.setdefault(chosen[k]["document_type"], 0)
        by_type[chosen[k]["document_type"]] += 1
    # 700 circulars in 2025 must not be represented as thinly as 12 monetary
    # policy documents just to make the columns line up.
    assert by_type["circular"] > by_type.get("monetary_policy", 0)


# --- allocation: the requested size must actually be delivered ------------- #

def test_a_feasible_request_returns_exactly_the_requested_size():
    """400 asked for, 400 returned. The cap trims; it must not shrink the sample."""
    sample = sampling.stratified_sample(CORPUS, size=400)
    assert len(sample.keys) == 400
    assert sample.shortfall == 0


def test_cap_trimmed_slots_are_redistributed_rather_than_lost():
    # Without pass 4 the cap removes 2019's excess and simply keeps the smaller
    # total, so this returns ~250 and reads as if 400 files were profiled.
    capped = sampling.stratified_sample(CORPUS, size=400, max_cohort_share=0.30)
    uncapped = sampling.stratified_sample(CORPUS, size=400, max_cohort_share=1.0)
    assert len(capped.keys) == len(uncapped.keys) == 400


def test_the_cohort_cap_still_holds_after_redistribution():
    from collections import Counter

    sample = sampling.stratified_sample(CORPUS, size=400, max_cohort_share=0.30)
    chosen = {r["comparison_key"]: r for r in CORPUS}
    counts = Counter(sampling.year_cohort(chosen[k]["year"]) for k in sample.keys)
    for cohort, n in counts.items():
        assert n <= int(400 * 0.30), (cohort, n)


def test_an_infeasible_request_reports_its_shortfall_instead_of_pretending():
    # One cohort only: the 30% cap makes 120 the ceiling, whatever was asked for.
    only_2019 = _rows(5000, year=2019, doc_type="circular")
    sample = sampling.stratified_sample(only_2019, size=400, max_cohort_share=0.30)
    assert len(sample.keys) == 120
    assert sample.shortfall == 280
    assert sample.notes


def test_a_corpus_smaller_than_the_request_reports_a_shortfall():
    sample = sampling.stratified_sample(_rows(7, year=2021, doc_type="act"), size=400)
    assert len(sample.keys) == 7
    assert sample.shortfall == 393
    assert any("exhausted" in note for note in sample.notes)


def test_a_floor_larger_than_the_budget_spreads_instead_of_favouring_early_strata():
    """The floor pass must be round-robin, not a sorted walk with a break.

    10 strata, floor 5, budget 12. Round-robin gives every stratum one slot and
    then two a second. A `for key in sorted(...): take = min(floor, ...); break`
    loop gives 5 + 5 + 2 — three strata represented and seven invisible, chosen by
    nothing but their names.
    """
    corpus = []
    for i in range(10):
        corpus += _rows(20, year=2021, doc_type=f"type{i:02d}", prefix=f"t{i}")
    sample = sampling.stratified_sample(
        corpus, size=12, floor=5, max_cohort_share=1.0
    )
    chosen = {r["comparison_key"]: r for r in corpus}
    represented = {chosen[k]["document_type"] for k in sample.keys}
    assert len(sample.keys) == 12
    assert len(represented) == 10          # lexicographic filling gives 3


def test_keys_are_unique():
    keys = sampling.stratified_sample(CORPUS, size=400).keys
    assert len(keys) == len(set(keys))


def test_an_empty_corpus_returns_an_empty_sample():
    sample = sampling.stratified_sample([], size=400)
    assert sample.keys == ()
    assert sample.strata == ()


def test_requesting_more_than_exists_returns_everything_once():
    small = _rows(7, year=2021, doc_type="act")
    sample = sampling.stratified_sample(small, size=400)
    assert len(sample.keys) == 7
    assert len(set(sample.keys)) == 7
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/pytest tests/test_nrb_sampling.py -q
```

Expected: `ModuleNotFoundError: app.nrb.sampling`.

- [ ] **Step 3: Write `app/nrb/sampling.py`**

```python
"""Choosing a representative slice of the NRB corpus. Pure — no DB, no network.

`rows[:400]` is the wrong answer twice over. The catalog's id order follows the
order REST paged the post types in, so the first 400 rows are one post type from
one department — a profile of that, dressed as a profile of the corpus. And any
order that depends on the database's row order is not reproducible.

So: stratify, then order deterministically WITHIN each stratum by a hash of the
row's own identity. The hash is uncorrelated with publication date, department
and insertion order, and it is stable across machines and runs — the same
property `nrb_files.comparison_key` already gives file identity.

ALLOCATION FOLLOWS REPRESENTATION, NOT PARITY
    Proportional to stratum size, with a floor so small strata appear at all, and
    a per-cohort cap so 2019 cannot swallow the sample. 2019 is 9,182 of 18,263
    files (NRB's CMS migration, spec §2 and the Phase 3 measurements): purely
    proportional allocation would spend 50% of the budget on it, and equal-sized
    strata would over-represent a 3-file stratum 100x. Neither is honest.

    A stratum that cannot fill its floor is reported `weak`, never padded. The
    report names every stratum with n < 10 so no conclusion is drawn from one
    silently.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Sequence

__all__ = [
    "COHORTS",
    "Sample",
    "Stratum",
    "WEAK_THRESHOLD",
    "stratified_sample",
    "year_cohort",
]

# Cohorts chosen from the MEASURED distribution (files joined to their source's
# publication year, scratch DB, 2026-08-15): <=2018 886, 2019 9,182, 2020-2022
# 3,095, 2023-2026 5,109. 2019 stands alone because Phase 3 measured its document
# typing at 47.5% against 89-100% everywhere else, and the open question is
# whether its EXTRACTION quality is as different as its metadata quality.
COHORTS = ("<=2018", "2019", "2020-2022", "2023-2026", "unknown")

# Below this, a stratum's numbers are reported but no conclusion is drawn.
WEAK_THRESHOLD = 10


@dataclass(frozen=True)
class Stratum:
    cohort: str
    document_type: str
    resource_type: str
    available: int
    allocated: int
    selected: int
    weak: bool


@dataclass(frozen=True)
class Sample:
    keys: tuple[str, ...]
    strata: tuple[Stratum, ...]
    requested: int
    # requested - len(keys). Non-zero means the constraints could not all be met
    # (the corpus is smaller than `size`, or every non-capped cohort ran out of
    # headroom). A short sample that SAYS it is short is fine; one that reads as
    # complete is not, so this is printed rather than inferred from a length.
    shortfall: int = 0
    notes: tuple[str, ...] = ()


def year_cohort(year: int | None) -> str:
    if year is None:
        return "unknown"
    if year <= 2018:
        return "<=2018"
    if year == 2019:
        return "2019"
    if year <= 2022:
        return "2020-2022"
    return "2023-2026"


def _order_key(row: dict[str, Any]) -> str:
    """Deterministic, machine-independent, uncorrelated with anything real.

    sha256 of the row's own identity rather than `random.shuffle(seed=…)`: no
    dependence on Python's PRNG implementation, and the same row sorts to the same
    position whatever else is in the corpus — so growing the catalog does not
    reshuffle an existing sample.
    """
    return hashlib.sha256(row["comparison_key"].encode("utf-8")).hexdigest()


def stratified_sample(
    rows: Sequence[dict[str, Any]],
    *,
    size: int,
    floor: int = 5,
    max_cohort_share: float = 0.30,
) -> Sample:
    """A reproducible, representative sample of `size` files.

    Four passes, in this order, because each constrains the next:

      1. **Floor, round-robin** — one slot at a time across every non-empty
         stratum, so a 12-document type is measurable at all. Round-robin and not
         "walk the sorted list handing out `floor` each until the budget dies":
         that second form is what a `for … break` loop does, and when the budget
         cannot cover every stratum it silently gives everything to the
         lexicographically early ones. One slot at a time means an insufficient
         budget costs every stratum its depth, never its existence.
      2. **Proportional** — the remaining budget is split by stratum headroom, so
         a 700-file stratum is not represented as thinly as a 3-file one.
      3. **Cohort cap** — no year cohort may exceed `max_cohort_share` of the
         total. 2019 is half the corpus and would otherwise be half the sample,
         which would make every "is 2019 worse?" comparison a comparison of 2019
         with a rounding error.
      4. **Redistribution** — every slot the cap removed is handed back, same
         deterministic round-robin, to strata in cohorts that are NOT at their
         cap and still have headroom. Without this the cap silently shrinks a
         400-file request to whatever survived trimming, so the caller would get a
         sample both smaller and differently shaped than the one they asked for.
         The cap is re-checked before every grant, so this can never breach it.

    When the request is genuinely infeasible — fewer rows than `size`, or every
    non-capped cohort exhausted — the result carries a `shortfall` and a note
    saying which constraint bound. It is never silently rounded down.

    Strata are `(year cohort, document type, resource type)`. Owner is NOT a
    stratification key — 33 codes crossed with the rest shatters into single-digit
    cells — but it is carried through for the report to break out afterwards.
    """
    if not rows or size <= 0:
        return Sample(
            keys=(), strata=(), requested=size,
            shortfall=max(size, 0), notes=("no rows to sample",) if size > 0 else (),
        )

    buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (
            year_cohort(row.get("year")),
            row.get("document_type") or "untyped",
            row.get("resource_type") or "unknown",
        )
        buckets.setdefault(key, []).append(row)

    # Sorting the ROWS inside each bucket is what makes the result independent of
    # the input order; sorting the BUCKET KEYS makes every loop below stable.
    for bucket in buckets.values():
        bucket.sort(key=_order_key)
    ordered_keys = sorted(buckets)
    # Round-robin order: larger strata first, ties broken lexically for
    # determinism. Deliberately NOT plain lexical order — see pass 1.
    rr_order = sorted(ordered_keys, key=lambda k: (-len(buckets[k]), k))

    allocation = {key: 0 for key in ordered_keys}
    notes: list[str] = []
    cap = int(size * max_cohort_share)
    cohort_totals: dict[str, int] = {cohort: 0 for cohort in COHORTS}

    def grant(key: tuple[str, str, str]) -> None:
        allocation[key] += 1
        cohort_totals[key[0]] = cohort_totals.get(key[0], 0) + 1

    def revoke(key: tuple[str, str, str]) -> None:
        allocation[key] -= 1
        cohort_totals[key[0]] -= 1

    # 1. floor — one slot at a time, so an insufficient budget spreads.
    remaining = size
    progress = True
    while remaining > 0 and progress:
        progress = False
        for key in rr_order:
            if remaining <= 0:
                break
            if allocation[key] >= floor or allocation[key] >= len(buckets[key]):
                continue
            grant(key)
            remaining -= 1
            progress = True

    # 2. proportional over remaining headroom.
    if remaining > 0:
        headroom = {k: len(buckets[k]) - allocation[k] for k in ordered_keys}
        total = sum(headroom.values())
        if total > 0:
            for key in ordered_keys:
                extra = min(headroom[key], int(remaining * headroom[key] / total))
                for _ in range(extra):
                    grant(key)
            # Integer division leaves a remainder; hand it out deterministically,
            # largest remaining headroom first.
            spent = sum(allocation.values())
            for key in sorted(
                ordered_keys, key=lambda k: (-(len(buckets[k]) - allocation[k]), k)
            ):
                if spent >= size:
                    break
                if allocation[key] < len(buckets[key]):
                    grant(key)
                    spent += 1

    # 3. cohort cap.
    for cohort in sorted({k[0] for k in ordered_keys}):
        keys_in = [k for k in ordered_keys if k[0] == cohort]
        while cohort_totals.get(cohort, 0) > cap:
            # Trim above the floor first, so the cap costs depth before breadth.
            # Only when nothing is above the floor does it cost breadth — and it
            # says so, because a cap and a floor CAN genuinely conflict (one
            # cohort with more than cap/floor strata) and silently keeping the
            # floor would mean silently breaching the cap.
            trimmable = [k for k in keys_in if allocation[k] > floor]
            if not trimmable:
                trimmable = [k for k in keys_in if allocation[k] > 0]
                if trimmable:
                    notes.append(
                        f"cohort {cohort}: the {max_cohort_share:.0%} cap forced "
                        f"strata below the floor of {floor}"
                    )
            if not trimmable:
                notes.append(f"cohort {cohort}: cannot be trimmed to the cap")
                break
            revoke(max(trimmable, key=lambda k: (allocation[k], k)))

    # 4. redistribution — give back what the cap took, to cohorts with room.
    spent = sum(allocation.values())
    progress = True
    while spent < size and progress:
        progress = False
        for key in rr_order:
            if spent >= size:
                break
            if allocation[key] >= len(buckets[key]):
                continue
            if cohort_totals.get(key[0], 0) >= cap:
                continue
            grant(key)
            spent += 1
            progress = True
    if spent < size:
        exhausted = all(
            allocation[k] >= len(buckets[k]) for k in ordered_keys
        )
        notes.append(
            "corpus exhausted before the requested size"
            if exhausted
            else f"every cohort reached the {max_cohort_share:.0%} cap "
                 f"({cap} of {size}) before the requested size"
        )

    keys: list[str] = []
    strata: list[Stratum] = []
    for key in ordered_keys:
        take = allocation[key]
        chosen = buckets[key][:take]
        keys.extend(row["comparison_key"] for row in chosen)
        strata.append(
            Stratum(
                cohort=key[0],
                document_type=key[1],
                resource_type=key[2],
                available=len(buckets[key]),
                allocated=take,
                selected=len(chosen),
                weak=len(chosen) < WEAK_THRESHOLD,
            )
        )
    return Sample(
        keys=tuple(keys),
        strata=tuple(strata),
        requested=size,
        shortfall=max(size - len(keys), 0),
        notes=tuple(notes),
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_nrb_sampling.py -q
```

Expected: PASS, 21 tests.

- [ ] **Step 5: Commit**

```bash
git add app/nrb/sampling.py tests/test_nrb_sampling.py
git commit -m "feat(nrb): deterministic stratified corpus sampling (Phase 6A)"
```

---

### Task 7A: The benchmark manifest (`app/nrb/manifest.py`, `scripts/nrb_sample.py`)

The sample is drawn **once**, from the full catalog, and written down. Everything
downstream names that file.

**Files:**
- Create: `app/nrb/manifest.py`
- Create: `scripts/nrb_sample.py`
- Test: `tests/test_nrb_manifest.py`

**Interfaces:**
- Consumes: `sampling.stratified_sample`, `sampling.Sample`, `catalog.load_sample_rows`.
- Produces: `MANIFEST_VERSION: str`, `Manifest` (frozen dataclass: `version`, `drawn_at`, `requested`, `shortfall`, `sampler: dict`, `catalog_counts: dict`, `strata: tuple[dict, ...]`, `notes: tuple[str, ...]`, `entries: tuple[dict, ...]`), `Manifest.keys() -> tuple[str, ...]`, `build_manifest(rows, sample, *, drawn_at, catalog_counts) -> Manifest`, `write_manifest(manifest, path) -> None`, `read_manifest(path) -> Manifest`, `MANIFEST_MAX_KEYS`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_nrb_manifest.py`:

```python
"""The benchmark manifest — drawn once, then named by everything downstream."""

import json

import pytest

from app.nrb import manifest as manifest_module
from app.nrb import sampling

ROWS = [
    {
        "comparison_key": f"https://www.nrb.org.np/uploads/doc-{i}.pdf",
        "resource_type": "pdf" if i % 3 else "spreadsheet",
        "fetch_status": "pending",
        "content_sha256": None,
        "document_type": "circular" if i % 2 else "directive",
        "owner": "bfr" if i % 4 else "red",
        "year": 2019 if i % 5 else 2024,
    }
    for i in range(200)
]


def _manifest(size=40):
    sample = sampling.stratified_sample(ROWS, size=size)
    return manifest_module.build_manifest(
        ROWS, sample,
        drawn_at="2026-08-15T00:00:00+00:00",
        catalog_counts={"files": 200, "fetched": 0},
    )


def test_every_entry_carries_the_exact_key_and_its_strata():
    entry = _manifest().entries[0]
    assert entry["comparison_key"].startswith("https://")
    for field in ("year", "document_type", "resource_type", "owner", "stratum"):
        assert field in entry


def test_the_manifest_records_how_it_was_drawn():
    m = _manifest()
    assert m.sampler["floor"] == 5
    assert m.sampler["max_cohort_share"] == 0.30
    assert m.requested == 40
    assert m.drawn_at == "2026-08-15T00:00:00+00:00"
    assert m.catalog_counts["files"] == 200


def test_keys_match_the_entries_exactly_and_are_unique():
    m = _manifest()
    assert len(m.keys()) == len(m.entries) == len(set(m.keys()))


def test_it_round_trips_through_disk_unchanged(tmp_path):
    m = _manifest()
    path = tmp_path / "manifest.json"
    manifest_module.write_manifest(m, path)
    assert manifest_module.read_manifest(path) == m


def test_the_file_is_human_readable_and_stably_ordered(tmp_path):
    path = tmp_path / "manifest.json"
    manifest_module.write_manifest(_manifest(), path)
    first = path.read_text(encoding="utf-8")
    manifest_module.write_manifest(_manifest(), path)
    assert path.read_text(encoding="utf-8") == first     # byte-identical rewrite
    payload = json.loads(first)
    assert payload["version"] == manifest_module.MANIFEST_VERSION


def test_devanagari_keys_survive_the_round_trip(tmp_path):
    rows = [
        {
            "comparison_key": "https://www.nrb.org.np/uploads/आगलागी-२०७४.pdf",
            "resource_type": "pdf", "fetch_status": "pending",
            "content_sha256": None, "document_type": "circular",
            "owner": "bfr", "year": 2024,
        }
    ]
    sample = sampling.stratified_sample(rows, size=1)
    m = manifest_module.build_manifest(
        rows, sample, drawn_at="2026-08-15T00:00:00+00:00", catalog_counts={}
    )
    path = tmp_path / "m.json"
    manifest_module.write_manifest(m, path)
    assert "आगलागी" in path.read_text(encoding="utf-8")   # not \uXXXX escaped
    assert manifest_module.read_manifest(path).keys() == m.keys()


def test_a_manifest_over_the_cap_is_refused(tmp_path):
    rows = [
        {
            "comparison_key": f"https://www.nrb.org.np/uploads/{i}.pdf",
            "resource_type": "pdf", "fetch_status": "pending",
            "content_sha256": None, "document_type": "circular",
            "owner": "bfr", "year": 2024,
        }
        for i in range(manifest_module.MANIFEST_MAX_KEYS + 10)
    ]
    sample = sampling.stratified_sample(
        rows, size=manifest_module.MANIFEST_MAX_KEYS + 10, max_cohort_share=1.0
    )
    with pytest.raises(ValueError, match="cap"):
        manifest_module.build_manifest(
            rows, sample, drawn_at="2026-08-15T00:00:00+00:00", catalog_counts={}
        )


def test_a_shortfall_and_its_notes_are_carried_into_the_manifest():
    sample = sampling.stratified_sample(ROWS, size=5000)
    m = manifest_module.build_manifest(
        ROWS, sample, drawn_at="2026-08-15T00:00:00+00:00", catalog_counts={}
    )
    assert m.shortfall > 0
    assert m.notes


def test_reading_a_manifest_with_an_unknown_version_is_refused(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"version": "manifest-99", "entries": []}),
                    encoding="utf-8")
    with pytest.raises(ValueError, match="version"):
        manifest_module.read_manifest(path)
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/pytest tests/test_nrb_manifest.py -q
```

Expected: `ModuleNotFoundError: app.nrb.manifest`.

- [ ] **Step 3: Write `app/nrb/manifest.py`**

```python
"""The benchmark manifest: the Phase 6A cohort, drawn once and written down.

WHY A FILE, AND NOT A FLAG
    The first draft of this plan fetched the sample with broad
    `--section`/`--year`/`--limit` passes and then re-sampled whatever landed on
    disk. That is wrong twice over. Phase 5 selects `pending` rows in **id order**
    within a scope, and catalog id order is the order REST paged the post types —
    so "circulars from 2019, limit 60" returns the 60 with the lowest ids, and
    stratifying over that measures the id order rather than the corpus. It is also
    not reproducible: any later fetch changes what is on disk and therefore what
    gets re-sampled, so two runs of the same profile would describe two different
    cohorts.

    So the sample is drawn ONCE from the full catalog, saved with each file's
    exact `comparison_key` and its strata, and every later step — fetch, extract,
    calibrate — names that file. The manifest is committed, which is what makes
    the published profile something a reader can re-run rather than take on
    trust.

WHAT IS RECORDED, AND WHY EACH PART
    * `entries` — the exact keys, each with `year`, `document_type`,
      `resource_type`, `owner` and its `stratum`. The strata are stored rather
      than recomputed because the catalog moves: a source re-typed by a later sync
      must not silently re-label a cohort that has already been profiled.
    * `sampler` — size, floor, cohort cap, sampler version. Reproducing the draw
      needs the parameters, not just the result.
    * `catalog_counts` + `drawn_at` — what the corpus looked like when the sample
      was taken, so a reader can tell whether the corpus has moved since.
    * `shortfall` + `notes` — carried verbatim from the sampler. A cohort that
      could not be filled is a caveat on every number downstream, and it belongs
      with the cohort rather than in someone's memory.

    `comparison_key` is the identity, not `content_sha256`: the sample is drawn
    BEFORE anything is fetched, when the hash does not exist yet.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .sampling import Sample, year_cohort

__all__ = [
    "MANIFEST_MAX_KEYS",
    "MANIFEST_VERSION",
    "Manifest",
    "build_manifest",
    "read_manifest",
    "write_manifest",
]

# Bumped if the file's shape changes. `read_manifest` refuses anything else
# rather than half-understanding it — a manifest is a benchmark definition, and
# quietly misreading one would silently redefine the benchmark.
MANIFEST_VERSION = "manifest-1"

# A manifest is a benchmark cohort, not a back door around the scope-is-required
# rule. 5,000 keys is ~12x the planned 400-file sample and far under the 18,263
# file corpus. Mirrors `catalog.MANIFEST_MAX_KEYS`.
MANIFEST_MAX_KEYS = 5000


@dataclass(frozen=True)
class Manifest:
    version: str
    drawn_at: str
    requested: int
    shortfall: int
    sampler: dict[str, Any]
    catalog_counts: dict[str, Any]
    strata: tuple[dict[str, Any], ...]
    notes: tuple[str, ...]
    entries: tuple[dict[str, Any], ...]

    def keys(self) -> tuple[str, ...]:
        """The exact `comparison_key` values this cohort consists of."""
        return tuple(entry["comparison_key"] for entry in self.entries)


def build_manifest(
    rows: Sequence[dict[str, Any]],
    sample: Sample,
    *,
    drawn_at: str,
    catalog_counts: dict[str, Any],
    floor: int = 5,
    max_cohort_share: float = 0.30,
) -> Manifest:
    """Freeze a drawn `Sample` into a durable cohort definition."""
    if len(sample.keys) > MANIFEST_MAX_KEYS:
        raise ValueError(
            f"manifest would name {len(sample.keys)} keys; the cap is "
            f"{MANIFEST_MAX_KEYS}. A manifest is a benchmark cohort, not a way "
            f"to fetch the whole corpus."
        )
    by_key = {row["comparison_key"]: row for row in rows}
    entries = []
    for key in sample.keys:
        row = by_key[key]
        cohort = year_cohort(row.get("year"))
        document_type = row.get("document_type") or "untyped"
        resource_type = row.get("resource_type") or "unknown"
        entries.append(
            {
                "comparison_key": key,
                "year": row.get("year"),
                "document_type": document_type,
                "resource_type": resource_type,
                "owner": row.get("owner"),
                # Stored, not recomputed later: a source re-typed by a future sync
                # must not silently re-label an already-profiled cohort.
                "stratum": f"{cohort}/{document_type}/{resource_type}",
            }
        )
    return Manifest(
        version=MANIFEST_VERSION,
        drawn_at=drawn_at,
        requested=sample.requested,
        shortfall=sample.shortfall,
        sampler={
            "size": sample.requested,
            "floor": floor,
            "max_cohort_share": max_cohort_share,
            "sampler_version": MANIFEST_VERSION,
        },
        catalog_counts=dict(catalog_counts),
        strata=tuple(
            {
                "cohort": s.cohort,
                "document_type": s.document_type,
                "resource_type": s.resource_type,
                "available": s.available,
                "selected": s.selected,
                "weak": s.weak,
            }
            for s in sample.strata
        ),
        notes=tuple(sample.notes),
        entries=tuple(entries),
    )


def write_manifest(manifest: Manifest, path: str | Path) -> None:
    """Write the manifest as indented, non-escaped JSON.

    `ensure_ascii=False` so NRB's Devanagari filenames stay readable in the
    committed file rather than becoming a wall of `\uXXXX`. `sort_keys=True` and
    a fixed indent so re-writing an unchanged manifest is byte-identical and a
    real change diffs cleanly.
    """
    payload = {
        "version": manifest.version,
        "drawn_at": manifest.drawn_at,
        "requested": manifest.requested,
        "shortfall": manifest.shortfall,
        "sampler": manifest.sampler,
        "catalog_counts": manifest.catalog_counts,
        "strata": list(manifest.strata),
        "notes": list(manifest.notes),
        "entries": list(manifest.entries),
    }
    Path(path).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def read_manifest(path: str | Path) -> Manifest:
    """Load a manifest, refusing a version this code does not define."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    version = payload.get("version")
    if version != MANIFEST_VERSION:
        raise ValueError(
            f"manifest version {version!r} is not {MANIFEST_VERSION!r} — refusing "
            f"to half-read a benchmark definition"
        )
    entries = tuple(payload.get("entries") or ())
    if len(entries) > MANIFEST_MAX_KEYS:
        raise ValueError(
            f"manifest names {len(entries)} keys; the cap is {MANIFEST_MAX_KEYS}"
        )
    return Manifest(
        version=version,
        drawn_at=payload.get("drawn_at", ""),
        requested=int(payload.get("requested", 0)),
        shortfall=int(payload.get("shortfall", 0)),
        sampler=payload.get("sampler") or {},
        catalog_counts=payload.get("catalog_counts") or {},
        strata=tuple(payload.get("strata") or ()),
        notes=tuple(payload.get("notes") or ()),
        entries=entries,
    )
```

- [ ] **Step 4: Write `scripts/nrb_sample.py`**

```python
#!/usr/bin/env python
"""Draw the Phase 6A benchmark cohort ONCE and write it to a manifest.

    .venv/bin/python scripts/nrb_sample.py --size 400 --out docs/nrb/phase6a-manifest.json

Separate from `nrb_extract.py` on purpose: sampling and extraction must not be the
same command, or a second profiling run would silently re-draw the cohort and the
two runs' numbers would not be comparable. This writes a file; everything else
reads it.

Refuses to overwrite an existing manifest without `--force`, for the same reason.

Makes NO network request and downloads nothing — it reads the catalog and writes
JSON. `scripts/nrb_fetch.py --manifest <path>` is what downloads the cohort.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.nrb import catalog, sampling  # noqa: E402
from app.nrb.manifest import build_manifest, write_manifest  # noqa: E402


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--size", type=int, default=400,
                        help="how many files the cohort should contain")
    parser.add_argument("--out", required=True, metavar="PATH",
                        help="where to write the manifest")
    parser.add_argument("--floor", type=int, default=5,
                        help="minimum files per non-empty stratum")
    parser.add_argument("--max-cohort-share", type=float, default=0.30,
                        help="no year cohort may exceed this share of the sample")
    parser.add_argument("--section", action="append", default=None, metavar="TYPE",
                        help="restrict the DRAW to these document types; repeatable")
    parser.add_argument("--force", action="store_true",
                        help="overwrite an existing manifest (re-draws the cohort)")
    args = parser.parse_args(argv)

    out = Path(args.out)
    if out.exists() and not args.force:
        print(
            f"refusing to overwrite {out}: a manifest is drawn ONCE, and "
            f"re-drawing it makes the new profile incomparable with the old one. "
            f"Pass --force if that is really what you want.",
            file=sys.stderr,
        )
        return 2

    from app.db.session import SessionLocal

    async with SessionLocal() as session:
        rows = await catalog.load_sample_rows(session, sections=args.section)
        counts = await catalog.catalog_counts(session)

    sample = sampling.stratified_sample(
        rows, size=args.size, floor=args.floor,
        max_cohort_share=args.max_cohort_share,
    )
    manifest = build_manifest(
        rows, sample,
        drawn_at=datetime.now(timezone.utc).isoformat(),
        catalog_counts=counts,
        floor=args.floor,
        max_cohort_share=args.max_cohort_share,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    write_manifest(manifest, out)

    print(f"drew {len(manifest.entries)} of {args.size} requested -> {out}")
    if manifest.shortfall:
        print(f"SHORTFALL {manifest.shortfall}: " + "; ".join(manifest.notes))
    weak = [s for s in manifest.strata if s["weak"] and s["selected"]]
    print(f"{len(manifest.strata)} strata, {len(weak)} weak (n < 10)")
    for stratum in sorted(manifest.strata, key=lambda s: (-s["selected"], s["cohort"])):
        if stratum["selected"]:
            flag = "  WEAK" if stratum["weak"] else ""
            print(f"  {stratum['cohort']:<10} {stratum['document_type']:<18} "
                  f"{stratum['resource_type']:<12} "
                  f"{stratum['selected']:>4}/{stratum['available']:<6}{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_nrb_manifest.py -q
```

Expected: PASS, 9 tests.

- [ ] **Step 6: Commit**

```bash
git add app/nrb/manifest.py scripts/nrb_sample.py tests/test_nrb_manifest.py
git commit -m "feat(nrb): benchmark manifest — the cohort is drawn once (Phase 6A)"
```

---

### Task 8: The extraction pass (`app/nrb/extract.py`)

Mirrors `app/nrb/fetch.py`: advisory lock, batched commits, failure isolation, resumable.

**Files:**
- Create: `app/nrb/extract.py`
- Modify: `app/nrb/locks.py` (add `EXTRACT_LOCK_KEY`)
- Test: `tests/test_nrb_extract_integration.py` (Task 12)

**Interfaces:**
- Consumes: `catalog.select_extract_targets`, `catalog.record_extractions`, `catalog.extraction_counts`, `catalog.load_sample_rows`, `extraction.extract_file`, `extraction.EXTRACTOR_VERSION`, `filestore.resolve_path`, `sniff.family_for`, `sampling.stratified_sample`, `locks.advisory_lock`.
- Produces: `ExtractResult` (dataclass: `status`, `counters: dict[str, int]`, `notes: dict`, `counts: dict[str, int]`, `scope: dict`, `strata: list[dict]`, `dry_run: bool`, `duration_seconds: float`, `samples: list[dict]`, `.ok`), `run_extract(*, sections, owners, resource_types, years, keys, limit, force, manifest_strata, dry_run, engine, session_factory) -> ExtractResult`, `ExtractBusy`.
- Also produces (Task 6 addendum): `catalog.count_unfetched(session, keys: Sequence[str]) -> int` — how many manifest keys are not yet `fetched`, so a partly-downloaded cohort is stated rather than silently profiled.

- [ ] **Step 1: Add the lock key**

In `app/nrb/locks.py`, add to `__all__` and below `FETCH_LOCK_KEY`:

```python
EXTRACT_LOCK_KEY = int.from_bytes(b"NRB_XTRC", "big")
```

- [ ] **Step 2: Write `app/nrb/extract.py`**

```python
"""One native-extraction pass over fetched NRB blobs.

Same shape as `fetch.py` — select, work, record in batches, hold an advisory lock
— and for the same reasons, minus the network: **Phase 6A makes no HTTP request
at all.** It reads local blobs by `storage_key` through
`filestore.resolve_path`, which refuses anything escaping the base directory,
and verifies each blob against the sha256 in its own filename before parsing.

RESUMABLE, NOT IDEMPOTENT — same distinction Phase 5 draws
    Selection is "fetched blobs with no extraction at this `extractor_version`",
    so a second pass takes the NEXT blobs rather than redoing the last ones, and
    an interrupted pass keeps its progress. A repeat pass over an exhausted scope
    selects zero. Bumping `EXTRACTOR_VERSION` makes the whole corpus selectable
    again without deleting a row.

WHY THE BLOB IS VERIFIED BEFORE PARSING
    The path IS the checksum (`filestore`), so a blob that no longer hashes to its
    own filename is corrupt on disk — and a corrupt PDF is exactly the input that
    produces plausible-looking partial text. Recording `failed` for it is cheaper
    than discovering it in the numbers.

FAILURE ISOLATION
    `extraction.extract_file` never raises; a bad file becomes a `failed` row and
    the pass continues. That is not defensive coding, it is the requirement: a
    profiling batch that dies on file 200 of 400 has measured nothing.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from . import catalog, extraction, filestore, sniff
from .catalog import ExtractTarget
from .locks import EXTRACT_LOCK_KEY, LockBusy, advisory_lock
from .models import FETCH_RUN_COMPLETED, FETCH_RUN_FAILED, FETCH_RUN_PARTIAL
from .quality import STATUS_FAILED

logger = logging.getLogger("app.nrb.extract")

__all__ = ["ExtractBusy", "ExtractResult", "run_extract"]

# Rows per transaction. A killed pass loses at most this many verdicts, and the
# blobs are untouched regardless — same trade as `fetch.RECORD_BATCH`.
RECORD_BATCH = 50

# Bounded per-status examples for the manual inspection sample. Deterministic
# (first N in selection order), because a report that differs between runs cannot
# be diffed.
SAMPLES_PER_STATUS = 10

# Read in chunks so verification of a 46 MB blob does not buffer it twice.
HASH_CHUNK = 1024 * 1024


@dataclass
class ExtractResult:
    status: str
    counters: dict[str, int] = field(default_factory=dict)
    notes: dict[str, Any] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    scope: dict[str, Any] = field(default_factory=dict)
    samples: list[dict[str, Any]] = field(default_factory=list)
    strata: list[dict[str, Any]] = field(default_factory=list)
    dry_run: bool = False
    duration_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == FETCH_RUN_COMPLETED


def _counters() -> dict[str, int]:
    return {
        "blobs_selected": 0,
        "blobs_processed": 0,
        "extracted": 0,
        "suspicious": 0,
        "needs_ocr": 0,
        "unsupported": 0,
        "failed": 0,
        "bytes_read": 0,
        "pages_read": 0,
    }


def _verify(path: Path, expected_sha256: str) -> bool:
    """Does this blob still hash to its own filename?"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest() == expected_sha256


def _row_for(target: ExtractTarget, result: extraction.ExtractionResult) -> dict[str, Any]:
    """One `nrb_extractions` upsert. Column keys, per `catalog`'s Core-only rule.

    Note what is NOT here: no file id, no title, no document type, no owner. The
    row is a function of the bytes; the joins happen in the report.
    """
    return {
        "content_sha256": target.content_sha256,
        "extractor_version": extraction.EXTRACTOR_VERSION,
        "parser": result.parser,
        "media_family": result.family,
        "status": result.status,
        "reason": result.reason,
        "warnings": list(result.warnings),
        "page_count": result.page_count,
        "pages_with_text": result.pages_with_text,
        "char_count": result.char_count,
        "devanagari_ratio": result.devanagari_ratio,
        "text_page_coverage": result.text_page_coverage,
        "metrics": result.metrics,
        "preview": result.preview or None,
        "error": result.error,
        "duration_ms": result.duration_ms,
    }


def _extract_target(target: ExtractTarget, base: Path) -> extraction.ExtractionResult:
    """Resolve, verify and extract one blob. Never raises."""
    started = time.monotonic()
    try:
        path = filestore.resolve_path(target.storage_key, base)
    except filestore.FileStoreError:
        return extraction.ExtractionResult(
            parser="none", family="unknown", status=STATUS_FAILED,
            reason="parser_error", warnings=(), text="", page_count=None,
            pages_with_text=None, char_count=0, devanagari_ratio=None,
            text_page_coverage=None, metrics={}, preview="",
            error="storage key does not resolve inside the blob store",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    if not path.exists():
        return extraction.ExtractionResult(
            parser="none", family="unknown", status=STATUS_FAILED,
            reason="parser_error", warnings=(), text="", page_count=None,
            pages_with_text=None, char_count=0, devanagari_ratio=None,
            text_page_coverage=None, metrics={}, preview="",
            error="blob is recorded as fetched but is not on disk",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    try:
        if not _verify(path, target.content_sha256):
            return extraction.ExtractionResult(
                parser="none", family="unknown", status=STATUS_FAILED,
                reason="parser_error", warnings=(), text="", page_count=None,
                pages_with_text=None, char_count=0, devanagari_ratio=None,
                text_page_coverage=None, metrics={}, preview="",
                error="blob does not hash to its own storage key (corrupt on disk)",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
    except OSError as exc:
        return extraction.ExtractionResult(
            parser="none", family="unknown", status=STATUS_FAILED,
            reason="parser_error", warnings=(), text="", page_count=None,
            pages_with_text=None, char_count=0, devanagari_ratio=None,
            text_page_coverage=None, metrics={}, preview="",
            error=f"OSError: {exc.strerror or 'unreadable'}",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    # Our own magic-byte determination, never NRB's claim — that is the claim
    # Phase 5 exists to check.
    family = sniff.family_for(target.sniffed_mime)
    return extraction.extract_file(path, family=family, extension=target.extension)


async def run_extract(
    *,
    sections: Sequence[str] | None = None,
    owners: Sequence[str] | None = None,
    resource_types: Sequence[str] | None = None,
    years: Sequence[int] | None = None,
    keys: Sequence[str] | None = None,
    limit: int | None = None,
    force: bool = False,
    manifest_strata: Sequence[dict[str, Any]] | None = None,
    dry_run: bool = False,
    engine: AsyncEngine | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> ExtractResult:
    """Select, extract, classify, record. The whole manual pass.

    `dry_run` reports what WOULD be extracted and parses nothing — like the
    fetch's dry run and unlike the sync's, because the cost being previewed here
    is CPU time, and doing the work in a rolled-back transaction would spend it.
    """
    from ..db.session import SessionLocal, engine as app_engine

    engine = engine or app_engine
    session_factory = session_factory or SessionLocal
    started = time.monotonic()
    counters = _counters()
    scope = {
        "sections": list(sections or []),
        "owners": list(owners or []),
        "resource_types": list(resource_types or []),
        "years": list(years or []),
        # The COUNT, not the keys: the manifest file is the durable record, and a
        # 400-element list in every scope blob would make the log unreadable.
        "manifest_keys": len(keys or []),
        "limit": limit,
        "force": force,
        "extractor_version": extraction.EXTRACTOR_VERSION,
    }
    errors: list[str] = []
    samples: dict[str, list[dict[str, Any]]] = {}
    # Carried straight from the manifest when there is one — the strata were
    # decided at draw time and must not be recomputed from what got fetched.
    strata: list[dict[str, Any]] = list(manifest_strata or [])

    async with advisory_lock(engine, EXTRACT_LOCK_KEY, what="NRB extract"):
        async with session_factory() as session:
            # The cohort is NEVER re-drawn here. `keys` arrives from a manifest
            # that was drawn once against the full catalog (`app/nrb/manifest.py`);
            # re-sampling whatever happens to be on disk would make two runs of the
            # same profile describe two different cohorts.
            if keys:
                unfetched = await catalog.count_unfetched(session, keys)
                if unfetched:
                    # Not fatal — a partly-fetched manifest is a legitimate state
                    # mid-download — but it must be stated, because every
                    # percentage downstream is over the cohort that WAS extracted.
                    logger.warning(
                        "NRB extract: %d of %d manifest files are not on disk; "
                        "run scripts/nrb_fetch.py --manifest first for full coverage",
                        unfetched, len(keys),
                    )

            targets = await catalog.select_extract_targets(
                session,
                sections=sections,
                owners=owners,
                resource_types=resource_types,
                years=years,
                keys=list(keys) if keys else None,
                limit=limit,
                force=force,
                extractor_version=extraction.EXTRACTOR_VERSION,
            )
            counters["blobs_selected"] = len(targets)
            logger.info("NRB extract: %d blobs selected", len(targets))

            if dry_run:
                result = ExtractResult(
                    status=FETCH_RUN_COMPLETED,
                    counters=counters,
                    scope=scope,
                    strata=strata,
                    dry_run=True,
                    notes={
                        "bytes_selected": sum(t.content_length or 0 for t in targets),
                        "errors": [],
                    },
                    counts=await catalog.extraction_counts(
                        session, extractor_version=extraction.EXTRACTOR_VERSION
                    ),
                )
                result.duration_seconds = time.monotonic() - started
                return result

            base = filestore.base_dir()
            pending: list[dict[str, Any]] = []
            try:
                for index, target in enumerate(targets, start=1):
                    result_one = _extract_target(target, base)
                    pending.append(_row_for(target, result_one))

                    counters["blobs_processed"] += 1
                    counters[result_one.status] = counters.get(result_one.status, 0) + 1
                    counters["bytes_read"] += target.content_length or 0
                    counters["pages_read"] += result_one.page_count or 0
                    if result_one.error:
                        errors.append(f"{target.content_sha256[:12]}: {result_one.error}")

                    bucket = samples.setdefault(result_one.status, [])
                    if len(bucket) < SAMPLES_PER_STATUS:
                        bucket.append(
                            {
                                "content_sha256": target.content_sha256,
                                "resource_type": target.resource_type,
                                "status": result_one.status,
                                "reason": result_one.reason,
                                "warnings": list(result_one.warnings),
                                "page_count": result_one.page_count,
                                "char_count": result_one.char_count,
                                "devanagari_ratio": result_one.devanagari_ratio,
                                "text_page_coverage": result_one.text_page_coverage,
                                "preview": result_one.preview,
                            }
                        )

                    if len(pending) >= RECORD_BATCH:
                        await catalog.record_extractions(session, pending)
                        await session.commit()
                        pending = []
                        logger.info(
                            "NRB extract: %d/%d done (%d extracted, %d suspicious, "
                            "%d needs_ocr, %d failed)",
                            index, len(targets), counters["extracted"],
                            counters["suspicious"], counters["needs_ocr"],
                            counters["failed"],
                        )
            finally:
                # Whatever is in hand is committed even on the way out of an
                # exception: the CPU has already been spent, and losing the
                # verdicts is the one avoidable waste here.
                if pending:
                    await catalog.record_extractions(session, pending)
                    await session.commit()

            status = FETCH_RUN_COMPLETED if not counters["failed"] else FETCH_RUN_PARTIAL
            result = ExtractResult(
                status=status,
                counters=counters,
                notes={"errors": errors[:50], "error_count": len(errors)},
                counts=await catalog.extraction_counts(
                    session, extractor_version=extraction.EXTRACTOR_VERSION
                ),
                scope=scope,
                strata=strata,
                samples=[row for bucket in samples.values() for row in bucket],
            )

    result.duration_seconds = time.monotonic() - started
    logger.info(
        "NRB extract: %s — %d processed in %.1fs (%.1f files/min, %.0f pages/min)",
        status, counters["blobs_processed"], result.duration_seconds,
        counters["blobs_processed"] / max(result.duration_seconds / 60, 1e-9),
        counters["pages_read"] / max(result.duration_seconds / 60, 1e-9),
    )
    return result


# Re-exported so callers catch one exception type from this module.
ExtractBusy = LockBusy
```

Note `FETCH_RUN_FAILED` is imported for symmetry with `fetch.py` but only used if a future crash path is added; if the linter objects, drop it from the import.

- [ ] **Step 3: Smoke-test against the 49 blobs already on disk**

```bash
DATABASE_URL='postgresql+asyncpg://gateway:<pw>@127.0.0.1:5432/local_ai_gateway_p4' \
.venv/bin/python -c "
import asyncio, logging
logging.basicConfig(level=logging.INFO)
from app.nrb.extract import run_extract
r = asyncio.run(run_extract(limit=10))
print(r.status, r.counters)
for s in r.samples[:3]: print(s['status'], s['reason'], repr(s['preview'][:80]))
"
```

Expected: 10 processed, and — given the spec's §2 measurement — most or all landing `suspicious/legacy_font_suspected`. If they land `extracted`, stop and print the metrics for one: either the detector's thresholds are wrong or these blobs differ from the probe, and both are findings that must be understood before the live run.

- [ ] **Step 4: Verify a second pass is a no-op**

```bash
DATABASE_URL='postgresql+asyncpg://gateway:<pw>@127.0.0.1:5432/local_ai_gateway_p4' \
.venv/bin/python -c "
import asyncio
from app.nrb.extract import run_extract
r = asyncio.run(run_extract(limit=10))
print('selected:', r.counters['blobs_selected'])
"
```

Expected: `selected: 10` — the NEXT ten, not the same ten. Run twice more and it reaches `selected: 0` once all 49 are done. That is the resumability contract.

- [ ] **Step 5: Commit**

```bash
git add app/nrb/extract.py app/nrb/locks.py
git commit -m "feat(nrb): the extraction pass — locked, batched, resumable (Phase 6A)"
```

---

### Task 9: The profiling report (`report.py`)

This is where source metadata finally enters — at report time, over all referencing sources.

**Files:**
- Modify: `app/nrb/report.py` (append `summarize_extraction` / `render_extraction`)
- Create: `app/nrb/profile.py` (the DB-side cohort query)
- Test: `tests/test_nrb_extraction_report.py`

**Interfaces:**
- Consumes: `ExtractResult` from Task 8; `quality.STATUSES`; `sampling.COHORTS`, `sampling.year_cohort`.
- Produces: `report.summarize_extraction(result, profile) -> dict`, `report.render_extraction(summary) -> str`, `profile.load_profile(session, *, extractor_version) -> dict`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_nrb_extraction_report.py`:

```python
"""The extraction profile report. Pure — fed a dict, returns a dict and a string."""

from app.nrb import report


PROFILE = {
    "by_status": {"extracted": 40, "suspicious": 300, "needs_ocr": 50,
                  "unsupported": 8, "failed": 2},
    "by_cohort": {
        "<=2018": {"extracted": 2, "suspicious": 60, "needs_ocr": 20},
        "2019": {"extracted": 5, "suspicious": 100, "needs_ocr": 15},
        "2020-2022": {"extracted": 12, "suspicious": 70, "needs_ocr": 8},
        "2023-2026": {"extracted": 21, "suspicious": 70, "needs_ocr": 7},
    },
    "by_document_type": {
        "circular": {"extracted": 3, "suspicious": 120, "needs_ocr": 10},
        "act": {"extracted": 1, "suspicious": 6, "needs_ocr": 1},
    },
    "by_resource_type": {
        "pdf": {"extracted": 30, "suspicious": 300, "needs_ocr": 40},
        "spreadsheet": {"extracted": 10, "needs_ocr": 0},
        "image": {"needs_ocr": 10},
    },
    "by_reason": {"legacy_font_suspected": 280, "no_text_layer": 40, "clean": 40},
    "script": {"strong_devanagari": 6, "strong_latin": 330, "mixed": 40, "unclear": 24},
    "medians": {"chars_per_document": 8100.0, "pages_per_document": 4.0,
                "text_page_coverage": 0.92},
    "title_corroboration": {
        "legacy_font_suspected": 280,
        "with_devanagari_title": 268,
        "with_latin_title": 12,
    },
    "weak_strata": [
        {"cohort": "<=2018", "document_type": "rule_bylaw",
         "resource_type": "pdf", "selected": 3},
    ],
}


class _Result:
    status = "completed"
    dry_run = False
    duration_seconds = 412.0
    counters = {"blobs_selected": 400, "blobs_processed": 400, "extracted": 40,
                "suspicious": 300, "needs_ocr": 50, "unsupported": 8, "failed": 2,
                "pages_read": 2400, "bytes_read": 401_000_000}
    notes = {"errors": ["abc123: PdfReadError"], "error_count": 2}
    counts = {"blobs_fetched": 420, "blobs_extracted": 400}
    scope = {"sample": 400, "sections": [], "extractor_version": "native-1"}
    strata = []
    samples = [
        {"content_sha256": "a" * 64, "status": "suspicious",
         "reason": "legacy_font_suspected", "warnings": [], "page_count": 3,
         "char_count": 2400, "devanagari_ratio": 0.0, "text_page_coverage": 1.0,
         "resource_type": "pdf", "preview": "ffihW ffifiHrz reU=,. iqrn rrq"},
    ]


def test_the_summary_carries_every_status_even_at_zero():
    summary = report.summarize_extraction(_Result(), PROFILE)
    assert set(summary["by_status"]) >= {"extracted", "suspicious", "needs_ocr",
                                         "unsupported", "failed"}


def test_2019_is_a_named_cohort_and_not_folded_into_an_average():
    summary = report.summarize_extraction(_Result(), PROFILE)
    assert "2019" in summary["by_cohort"]
    rendered = report.render_extraction(summary)
    assert "2019" in rendered


def test_throughput_is_reported():
    summary = report.summarize_extraction(_Result(), PROFILE)
    assert summary["throughput"]["files_per_minute"] > 0
    assert summary["throughput"]["pages_per_minute"] > 0


def test_weak_strata_are_named_rather_than_hidden():
    rendered = report.render_extraction(report.summarize_extraction(_Result(), PROFILE))
    assert "weak" in rendered.lower()
    assert "rule_bylaw" in rendered


def test_the_title_corroboration_block_is_reported_separately_from_status():
    summary = report.summarize_extraction(_Result(), PROFILE)
    assert summary["title_corroboration"]["with_devanagari_title"] == 268
    rendered = report.render_extraction(summary)
    assert "title" in rendered.lower()


def test_the_manual_sample_is_bounded_and_previews_are_short():
    summary = report.summarize_extraction(_Result(), PROFILE)
    for row in summary["samples"]:
        assert len(row["preview"]) <= 300


def test_rendering_is_stable_across_calls():
    summary = report.summarize_extraction(_Result(), PROFILE)
    assert report.render_extraction(summary) == report.render_extraction(summary)
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
.venv/bin/pytest tests/test_nrb_extraction_report.py -q
```

Expected: `AttributeError: module 'app.nrb.report' has no attribute 'summarize_extraction'`.

- [ ] **Step 3: Write `app/nrb/profile.py`**

```python
"""The cohort view of extraction results — the ONLY place source metadata meets a
verdict.

`nrb_extractions` is content-intrinsic by construction (see its model docstring):
a blob is shared, so a column that depended on which source was processed first
would persist a different answer on every run. The joins that make the numbers
*interesting* — by year, by document type, by whether the title is Devanagari —
therefore happen HERE, at read time, over ALL referencing sources at once.

That also makes the title signal honest. `title_corroboration` reports how many
`legacy_font_suspected` blobs are referenced by at least one Devanagari-titled
source. It corroborates a verdict the bytes already produced; it never creates
one, and an English-titled document is never held to a Nepali expectation.

A file referenced by several sources is attributed to its lowest source id for
the by-type and by-year breakdowns. That is a reporting approximation, it is
deterministic, and the report says so.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import NRBExtraction, NRBFile, NRBSource, NRBSourceFile
from .sampling import year_cohort

__all__ = ["DEVANAGARI_SQL", "load_profile"]

# Postgres regex over the Devanagari block. Kept here rather than pulled into
# Python so the "does this title carry Devanagari" question is one query rather
# than 400 round trips.
DEVANAGARI_SQL = r"[ऀ-ॿ]"


async def load_profile(
    session: AsyncSession, *, extractor_version: str
) -> dict[str, Any]:
    """Every breakdown the report prints. Read-only, one version at a time."""

    base = (
        select(
            NRBExtraction.content_sha256,
            NRBExtraction.status,
            NRBExtraction.reason,
            NRBExtraction.char_count,
            NRBExtraction.page_count,
            NRBExtraction.devanagari_ratio,
            NRBExtraction.text_page_coverage,
            NRBFile.resource_type,
            func.min(NRBSource.id).label("source_id"),
        )
        .join(NRBFile, NRBFile.content_sha256 == NRBExtraction.content_sha256)
        .join(NRBSourceFile, NRBSourceFile.file_id == NRBFile.id)
        .join(NRBSource, NRBSource.id == NRBSourceFile.source_id)
        .where(NRBExtraction.extractor_version == extractor_version)
        .group_by(
            NRBExtraction.content_sha256, NRBExtraction.status, NRBExtraction.reason,
            NRBExtraction.char_count, NRBExtraction.page_count,
            NRBExtraction.devanagari_ratio, NRBExtraction.text_page_coverage,
            NRBFile.resource_type,
        )
        .subquery()
    )
    rows = (
        await session.execute(
            select(
                base.c.content_sha256, base.c.status, base.c.reason,
                base.c.char_count, base.c.page_count, base.c.devanagari_ratio,
                base.c.text_page_coverage, base.c.resource_type,
                NRBSource.document_type,
                NRBSource.owner,
                func.extract("year", NRBSource.published_at).label("year"),
            ).join(NRBSource, NRBSource.id == base.c.source_id)
        )
    ).all()

    def bucket(target: dict[str, dict[str, int]], key: str, status: str) -> None:
        target.setdefault(key or "unknown", {}).setdefault(status, 0)
        target[key or "unknown"][status] += 1

    by_status: dict[str, int] = {}
    by_cohort: dict[str, dict[str, int]] = {}
    by_document_type: dict[str, dict[str, int]] = {}
    by_resource_type: dict[str, dict[str, int]] = {}
    by_owner: dict[str, dict[str, int]] = {}
    by_reason: dict[str, int] = {}
    script = {"strong_devanagari": 0, "strong_latin": 0, "mixed": 0, "unclear": 0}
    chars: list[int] = []
    pages: list[int] = []
    coverage: list[float] = []

    for row in rows:
        status = row.status
        by_status[status] = by_status.get(status, 0) + 1
        by_reason[row.reason] = by_reason.get(row.reason, 0) + 1
        bucket(by_cohort, year_cohort(int(row.year) if row.year else None), status)
        bucket(by_document_type, row.document_type or "untyped", status)
        bucket(by_resource_type, row.resource_type, status)
        bucket(by_owner, row.owner or "none", status)
        ratio = row.devanagari_ratio
        if ratio is None:
            script["unclear"] += 1
        elif ratio > 0.5:
            script["strong_devanagari"] += 1
        elif ratio < 0.02:
            script["strong_latin"] += 1
        else:
            script["mixed"] += 1
        if row.char_count:
            chars.append(int(row.char_count))
        if row.page_count:
            pages.append(int(row.page_count))
        if row.text_page_coverage is not None:
            coverage.append(float(row.text_page_coverage))

    # The title corroboration — read-time only, never persisted. See the docstring.
    suspect = (
        select(NRBExtraction.content_sha256)
        .where(
            NRBExtraction.extractor_version == extractor_version,
            NRBExtraction.reason == "legacy_font_suspected",
        )
        .subquery()
    )
    corroborated = int(
        (
            await session.execute(
                select(func.count(func.distinct(NRBFile.content_sha256)))
                .join(suspect, suspect.c.content_sha256 == NRBFile.content_sha256)
                .join(NRBSourceFile, NRBSourceFile.file_id == NRBFile.id)
                .join(NRBSource, NRBSource.id == NRBSourceFile.source_id)
                .where(NRBSource.title.op("~")(DEVANAGARI_SQL))
            )
        ).scalar_one()
        or 0
    )
    total_suspect = int(
        (await session.execute(select(func.count()).select_from(suspect))).scalar_one()
        or 0
    )

    def med(values: list[Any]) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        mid = len(ordered) // 2
        if len(ordered) % 2:
            return float(ordered[mid])
        return (float(ordered[mid - 1]) + float(ordered[mid])) / 2

    return {
        "by_status": by_status,
        "by_cohort": by_cohort,
        "by_document_type": by_document_type,
        "by_resource_type": by_resource_type,
        "by_owner": by_owner,
        "by_reason": by_reason,
        "script": script,
        "medians": {
            "chars_per_document": med(chars),
            "pages_per_document": med(pages),
            "text_page_coverage": med(coverage),
        },
        "title_corroboration": {
            "legacy_font_suspected": total_suspect,
            "with_devanagari_title": corroborated,
            "with_latin_title": max(total_suspect - corroborated, 0),
        },
        "weak_strata": [],
    }
```

- [ ] **Step 4: Append the renderers to `app/nrb/report.py`**

Add `"summarize_extraction"` and `"render_extraction"` to `__all__`, `from .quality import STATUSES` to the imports, and append:

```python
def summarize_extraction(result: Any, profile: dict[str, Any]) -> dict[str, Any]:
    """A JSON-ready extraction profile. Deterministic, so two runs diff cleanly."""
    duration = max(result.duration_seconds, 1e-9)
    processed = result.counters.get("blobs_processed", 0)
    weak = list(profile.get("weak_strata") or []) + [
        s for s in (getattr(result, "strata", None) or []) if s.get("weak")
    ]
    return {
        "status": result.status,
        "dry_run": result.dry_run,
        "scope": result.scope,
        "counters": result.counters,
        "counts": result.counts,
        "duration_seconds": round(result.duration_seconds, 1),
        "throughput": {
            "files_per_minute": round(processed / (duration / 60), 1),
            "pages_per_minute": round(
                result.counters.get("pages_read", 0) / (duration / 60), 1
            ),
        },
        # Every status, including the zeroes: a report that omits a status makes
        # "no failures" indistinguishable from "failures not counted".
        "by_status": {s: profile.get("by_status", {}).get(s, 0) for s in STATUSES},
        "by_cohort": profile.get("by_cohort", {}),
        "by_document_type": profile.get("by_document_type", {}),
        "by_resource_type": profile.get("by_resource_type", {}),
        "by_owner": profile.get("by_owner", {}),
        "by_reason": profile.get("by_reason", {}),
        "script": profile.get("script", {}),
        "medians": profile.get("medians", {}),
        "title_corroboration": profile.get("title_corroboration", {}),
        "weak_strata": weak,
        "errors": (result.notes or {}).get("errors", [])[:SAMPLE_SIZE],
        "samples": getattr(result, "samples", []),
    }


def _status_row(label: str, counts: dict[str, int]) -> str:
    total = sum(counts.values()) or 1
    parts = " ".join(
        f"{name}={counts.get(name, 0):>5} ({counts.get(name, 0) / total:>5.1%})"
        for name in STATUSES
        if counts.get(name)
    )
    return f"  {label:<22} n={total:>5}  {parts}"


def render_extraction(summary: dict[str, Any]) -> str:
    """The human report. Leads with what would block Phase 6B, not with a total.

    Same principle as `render_documents`: a report that opens with "400 files
    profiled" and buries "0.5% of them are usable" is the wrong report.
    """
    lines: list[str] = []
    counters = summary["counters"]
    lines.append("NRB native extraction quality profile")
    lines.append("=" * 68)
    lines.append(f"extractor            {summary['scope'].get('extractor_version')}")
    lines.append(f"status               {summary['status']}")
    lines.append(f"blobs selected       {counters.get('blobs_selected', 0)}")
    lines.append(f"blobs processed      {counters.get('blobs_processed', 0)}")
    lines.append(f"duration             {summary['duration_seconds']}s "
                 f"({summary['throughput']['files_per_minute']} files/min, "
                 f"{summary['throughput']['pages_per_minute']} pages/min)")
    lines.append("")
    lines.append("By extraction status")
    total = sum(summary["by_status"].values()) or 1
    for name in STATUSES:
        count = summary["by_status"].get(name, 0)
        lines.append(f"  {name:<22} {count:>6}  ({count / total:>6.1%})")
    lines.append("")
    lines.append("Why (reason codes)")
    for name, count in sorted(
        summary["by_reason"].items(), key=lambda kv: (-kv[1], kv[0])
    ):
        lines.append(f"  {name:<22} {count:>6}")
    lines.append("")
    lines.append("By year cohort  (2019 is NRB's CMS migration — kept separate)")
    for cohort, counts in summary["by_cohort"].items():
        lines.append(_status_row(cohort, counts))
    lines.append("")
    lines.append("By document type")
    for name, counts in sorted(
        summary["by_document_type"].items(), key=lambda kv: (-sum(kv[1].values()), kv[0])
    ):
        lines.append(_status_row(name, counts))
    lines.append("")
    lines.append("By file format")
    for name, counts in sorted(
        summary["by_resource_type"].items(), key=lambda kv: (-sum(kv[1].values()), kv[0])
    ):
        lines.append(_status_row(name, counts))
    lines.append("")
    lines.append("Script profile (from the extracted text, not the title)")
    for name, count in summary["script"].items():
        lines.append(f"  {name:<22} {count:>6}")
    lines.append("")
    lines.append("Medians")
    for name, value in summary["medians"].items():
        lines.append(f"  {name:<22} {value}")
    lines.append("")
    corroboration = summary.get("title_corroboration") or {}
    if corroboration:
        lines.append(
            "Source-title corroboration (report-time only — never persisted, "
            "never the sole determinant)"
        )
        lines.append(
            f"  legacy_font_suspected  {corroboration.get('legacy_font_suspected', 0)}"
        )
        lines.append(
            f"    with Devanagari title {corroboration.get('with_devanagari_title', 0)}"
        )
        lines.append(
            f"    with latin title      {corroboration.get('with_latin_title', 0)}"
        )
        lines.append("")
    weak = summary.get("weak_strata") or []
    if weak:
        lines.append(f"WEAK STRATA — n < 10, draw no conclusions ({len(weak)})")
        for stratum in weak[:SAMPLE_SIZE]:
            lines.append(
                f"  {stratum.get('cohort')}/{stratum.get('document_type')}"
                f"/{stratum.get('resource_type')}  n={stratum.get('selected')}"
            )
        lines.append("")
    if summary["errors"]:
        lines.append(f"Errors ({len(summary['errors'])} shown)")
        for error in summary["errors"]:
            lines.append(f"  {error}")
        lines.append("")
    if summary["samples"]:
        lines.append("Manual inspection sample")
        for row in summary["samples"]:
            lines.append(
                f"  [{row['status']}/{row['reason']}] {row['content_sha256'][:12]} "
                f"{row.get('resource_type')} pages={row.get('page_count')} "
                f"chars={row.get('char_count')} dev={row.get('devanagari_ratio')}"
            )
            if row.get("preview"):
                lines.append(f"      {row['preview'][:120]}")
    return "\n".join(lines)
```

- [ ] **Step 5: Run the test to verify it passes**

```bash
.venv/bin/pytest tests/test_nrb_extraction_report.py -q
```

Expected: PASS, 7 tests.

- [ ] **Step 6: Commit**

```bash
git add app/nrb/report.py app/nrb/profile.py tests/test_nrb_extraction_report.py
git commit -m "feat(nrb): extraction profile report with 2019 broken out (Phase 6A)"
```

---

### Task 10: The CLI (`scripts/nrb_extract.py`)

**Files:**
- Create: `scripts/nrb_extract.py`
- Test: `tests/test_nrb_extract_cli.py`

**Interfaces:**
- Consumes: `extract.run_extract`, `extract.ExtractBusy`, `report.summarize_extraction`, `report.render_extraction`, `profile.load_profile`.
- Produces: `main(argv) -> int`, `_parse_args(argv)`, `CORE_SECTIONS` (imported from `scripts.nrb_fetch`? No — duplicated deliberately, see below).

- [ ] **Step 1: Write the failing test**

Create `tests/test_nrb_extract_cli.py`:

```python
"""The extraction CLI's argument contract. No DB, no parsing — run_extract is stubbed."""

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "nrb_extract_cli",
    Path(__file__).resolve().parents[1] / "scripts" / "nrb_extract.py",
)
cli = importlib.util.module_from_spec(_SPEC)
sys.modules["nrb_extract_cli"] = cli
_SPEC.loader.exec_module(cli)


class _Result:
    status = "completed"
    dry_run = True
    duration_seconds = 1.0
    counters = {"blobs_selected": 3, "blobs_processed": 0, "pages_read": 0}
    notes = {"errors": []}
    counts = {}
    scope = {"extractor_version": "native-1"}
    strata = []
    samples = []


def test_a_bare_command_refuses_and_exits_two(capsys):
    assert asyncio.run(cli.main([])) == 2
    assert "refusing to start" in capsys.readouterr().err


def test_core_is_an_accepted_scope(monkeypatch):
    seen = {}

    async def fake(**kwargs):
        seen.update(kwargs)
        return _Result()

    monkeypatch.setattr(cli, "run_extract", fake)
    monkeypatch.setattr(cli, "load_profile_for", lambda *a, **k: {})
    assert asyncio.run(cli.main(["--core", "--dry-run"])) == 0
    assert set(cli.CORE_SECTIONS) == set(seen["sections"])


def test_limit_is_passed_through(monkeypatch):
    seen = {}

    async def fake(**kwargs):
        seen.update(kwargs)
        return _Result()

    monkeypatch.setattr(cli, "run_extract", fake)
    monkeypatch.setattr(cli, "load_profile_for", lambda *a, **k: {})
    asyncio.run(cli.main(["--limit", "17", "--dry-run"]))
    assert seen["limit"] == 17


def test_a_manifest_is_a_scope_on_its_own_and_its_keys_are_passed_through(
    monkeypatch, tmp_path
):
    from app.nrb import manifest as manifest_module, sampling

    rows = [
        {"comparison_key": f"https://www.nrb.org.np/u/{i}.pdf", "resource_type": "pdf",
         "fetch_status": "fetched", "content_sha256": f"{i:064d}",
         "document_type": "circular", "owner": "bfr", "year": 2024}
        for i in range(12)
    ]
    path = tmp_path / "m.json"
    manifest_module.write_manifest(
        manifest_module.build_manifest(
            rows, sampling.stratified_sample(rows, size=6),
            drawn_at="2026-08-15T00:00:00+00:00", catalog_counts={},
        ),
        path,
    )

    seen = {}

    async def fake(**kwargs):
        seen.update(kwargs)
        return _Result()

    monkeypatch.setattr(cli, "run_extract", fake)
    monkeypatch.setattr(cli, "load_profile_for", lambda *a, **k: {})
    assert asyncio.run(cli.main(["--manifest", str(path), "--dry-run"])) == 0
    assert len(seen["keys"]) == 6
    assert all(k.startswith("https://") for k in seen["keys"])
    # The strata travel with the cohort; they are NOT recomputed downstream.
    assert seen["manifest_strata"]


def test_the_cli_never_re_draws_a_cohort(monkeypatch):
    """There is no --sample flag. Sampling is scripts/nrb_sample.py's job alone."""
    with pytest.raises(SystemExit):
        cli._parse_args(["--sample", "400"])


def test_year_and_section_compose(monkeypatch):
    seen = {}

    async def fake(**kwargs):
        seen.update(kwargs)
        return _Result()

    monkeypatch.setattr(cli, "run_extract", fake)
    monkeypatch.setattr(cli, "load_profile_for", lambda *a, **k: {})
    asyncio.run(cli.main(["--section", "circular", "--year", "2019", "--dry-run"]))
    assert seen["sections"] == ["circular"]
    assert seen["years"] == [2019]


def test_a_held_lock_exits_two_rather_than_racing(monkeypatch, capsys):
    async def busy(**kwargs):
        raise cli.ExtractBusy("another NRB extract is already running")

    monkeypatch.setattr(cli, "run_extract", busy)
    assert asyncio.run(cli.main(["--core"])) == 2
    assert "refusing to start" in capsys.readouterr().err


def test_json_output_is_valid_json(monkeypatch, capsys):
    async def fake(**kwargs):
        return _Result()

    monkeypatch.setattr(cli, "run_extract", fake)
    monkeypatch.setattr(cli, "load_profile_for", lambda *a, **k: {})
    asyncio.run(cli.main(["--core", "--dry-run", "--json"]))
    import json
    json.loads(capsys.readouterr().out)
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
.venv/bin/pytest tests/test_nrb_extract_cli.py -q
```

Expected: `FileNotFoundError` on `scripts/nrb_extract.py`.

- [ ] **Step 3: Write `scripts/nrb_extract.py`**

```python
#!/usr/bin/env python
"""Extract and quality-profile fetched NRB files (Phase 6A).

Takes blobs the Phase 5 fetch put on disk, runs each through a native parser, and
classifies the result: usable, suspicious, needs OCR, unsupported, or failed.
Writes one `nrb_extractions` row per blob and prints the profile.

**This is where Phase 6A stops.** No OCR runs, no legacy font is converted,
nothing is chunked or embedded, no `documents`/`ingest_jobs` row is created,
`LOCAL_TOOLS` is unchanged and no endpoint was added. The output is EVIDENCE for
choosing a Phase 6B strategy, not a step toward one.

SCOPE IS REQUIRED — THERE IS NO DEFAULT
    Extraction is CPU work over up to 18.3k blobs, so a slice must be named — the
    same rule as `nrb_fetch.py`:

    # the regulatory core
    .venv/bin/python scripts/nrb_extract.py --core

    # the benchmark cohort, drawn once by scripts/nrb_sample.py (what the profile wants)
    .venv/bin/python scripts/nrb_extract.py --manifest docs/nrb/phase6a-manifest.json

    # a bounded smoke test
    .venv/bin/python scripts/nrb_extract.py --section circular --limit 25 -v

    # one cohort, on its own
    .venv/bin/python scripts/nrb_extract.py --year 2019 --limit 100

    # what would be extracted, parsing nothing
    .venv/bin/python scripts/nrb_extract.py --core --dry-run

WHAT IT WILL NOT DO
    Make a network request of any kind. Run OCR. Evaluate a spreadsheet formula.
    Execute a macro. Store extracted text (only a <=300-char preview). Parse a
    blob that does not hash to its own storage key.

RESUMABILITY
    Selection is "fetched blobs with no extraction at this extractor version",
    committed every 50 blobs — so a killed pass keeps its progress and a repeat
    pass takes the NEXT blobs. Bumping `EXTRACTOR_VERSION` makes the whole corpus
    selectable again without deleting anything.

Exit codes: 0 the pass completed with no failures, 1 it ran but something failed,
2 it could not start (no scope given, or another extract holds the lock).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.nrb.extract import ExtractBusy, run_extract  # noqa: E402
from app.nrb.extraction import EXTRACTOR_VERSION  # noqa: E402
from app.nrb.manifest import read_manifest  # noqa: E402
from app.nrb.profile import load_profile  # noqa: E402
from app.nrb.report import render_extraction, summarize_extraction  # noqa: E402

# Duplicated from `scripts/nrb_fetch.py` rather than imported: scripts/ is not a
# package, and a sys.path import between two CLIs would be a worse coupling than
# six repeated strings. If this list and the fetch's ever disagree, the fetch's is
# authoritative — it decides what is on disk.
CORE_SECTIONS = (
    "directive", "circular", "act", "rule_bylaw", "guideline_manual", "monetary_policy",
)


async def load_profile_for(version: str) -> dict:
    """The cohort breakdowns, read back after the pass has committed."""
    from app.db.session import SessionLocal

    async with SessionLocal() as session:
        return await load_profile(session, extractor_version=version)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    scope = parser.add_argument_group("scope (at least one is REQUIRED)")
    scope.add_argument("--core", action="store_true",
                       help=f"the regulatory core: {', '.join(CORE_SECTIONS)}")
    scope.add_argument("--section", action="append", default=None, metavar="TYPE",
                       help="restrict to this document_type; repeatable")
    scope.add_argument("--owner", action="append", default=None, metavar="CODE",
                       help="restrict to this NRB department/office code; repeatable")
    scope.add_argument("--type", action="append", default=None, metavar="KIND",
                       dest="resource_type",
                       help="restrict to this resource_type (pdf, spreadsheet, "
                            "document, image)")
    scope.add_argument("--year", action="append", type=int, default=None,
                       metavar="YYYY", help="restrict to this publication year; "
                                            "repeatable")
    scope.add_argument("--manifest", default=None, metavar="PATH",
                       help="extract exactly the benchmark cohort named in a "
                            "manifest written by scripts/nrb_sample.py. The cohort "
                            "is never re-drawn here.")
    scope.add_argument("--limit", type=int, default=None, metavar="N",
                       help="extract at most N blobs (stable order, so it resumes)")
    scope.add_argument("--all", action="store_true",
                       help="every fetched blob. Explicit, because it is CPU work.")

    behaviour = parser.add_argument_group("behaviour")
    behaviour.add_argument("--force", action="store_true",
                           help="re-extract blobs already recorded at this version")
    behaviour.add_argument("--dry-run", action="store_true",
                           help="report what would be extracted. Parses nothing.")
    behaviour.add_argument("--json", action="store_true",
                           help="emit the profile as JSON")
    behaviour.add_argument("-v", "--verbose", action="store_true",
                           help="show progress logs")
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,   # keeps stdout clean for --json redirection
    )

    sections = list(args.section or [])
    if args.core:
        sections.extend(CORE_SECTIONS)
    sections = list(dict.fromkeys(sections))

    if not (sections or args.owner or args.resource_type or args.year
            or args.manifest or args.limit or args.all):
        print(
            "refusing to start: no scope given. Extraction is CPU work over up to "
            "18.3k blobs, so a slice must be chosen explicitly — try "
            "--manifest docs/nrb/phase6a-manifest.json, --core --dry-run, or "
            "--all if you really mean all of it.",
            file=sys.stderr,
        )
        return 2

    manifest_keys = None
    manifest_strata = None
    if args.manifest:
        manifest = read_manifest(args.manifest)
        manifest_keys = manifest.keys()
        manifest_strata = list(manifest.strata)
        print(
            f"manifest: {len(manifest_keys)} files drawn {manifest.drawn_at}"
            + (f" (SHORTFALL {manifest.shortfall})" if manifest.shortfall else ""),
            file=sys.stderr,
        )

    try:
        result = await run_extract(
            sections=sections or None,
            owners=args.owner,
            resource_types=args.resource_type,
            years=args.year,
            keys=manifest_keys,
            limit=args.limit,
            force=args.force,
            manifest_strata=manifest_strata,
            dry_run=args.dry_run,
        )
    except ExtractBusy as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return 2

    profile = {} if args.dry_run else await load_profile_for(EXTRACTOR_VERSION)
    summary = summarize_extraction(result, profile)
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str)
          if args.json else render_extraction(summary))

    if not result.ok:
        print(
            f"NOTE: pass status is {result.status} "
            f"(failed={result.counters.get('failed', 0)})",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
.venv/bin/pytest tests/test_nrb_extract_cli.py -q
```

Expected: PASS, 8 tests. If `load_profile_for` is called on the dry-run path the monkeypatch is unnecessary but harmless — leave it, it documents the seam.

- [ ] **Step 5: Verify the real CLI refuses and dry-runs**

```bash
.venv/bin/python scripts/nrb_extract.py ; echo "exit=$?"
DATABASE_URL='postgresql+asyncpg://gateway:<pw>@127.0.0.1:5432/local_ai_gateway_p4' \
  .venv/bin/python scripts/nrb_extract.py --core --dry-run
```

Expected: exit 2 with the refusal, then a dry-run summary naming the selected blob count with nothing parsed.

- [ ] **Step 6: Commit**

```bash
git add scripts/nrb_extract.py tests/test_nrb_extract_cli.py
git commit -m "feat(nrb): scripts/nrb_extract.py — scope-required extraction CLI (Phase 6A)"
```

---

### Task 11: The Docling calibration — extraction vs extraction

Turns "pypdf is a fair proxy" from an assertion into a measurement. Worker-only dependency, imported inside the function.

**Files:**
- Modify: `app/nrb/extraction.py` (append the Docling adapter)
- Create: `scripts/nrb_calibrate.py`
- Test: `tests/test_nrb_extraction.py` (append), plus `tests/test_rag_parsing_docling.py` as the regression gate.

**Interfaces:**
- Consumes: `app.rag.parsing._docling_converter`, `app.rag.parsing._pdf_pipeline_options` (read-only; `parsing.py` is **not** modified).
- Produces: `extraction.docling_pipeline_is_native() -> tuple[bool, str]`, `extraction.docling_extract(path: Path) -> ExtractionResult`.

**The instrument, and why the first draft was wrong.** The first draft compared
pypdf against `parsing.parse_to_chunks`. That is the RAG *pipeline*: Docling, then
`merge_blocks`, then `drop_small_blocks`, then front-matter skipping, then
chunking. A disagreement could therefore come from RAG's chunk filtering rather
than from what Docling read off the page, and the number would not mean what it
claimed to. This task compares **extraction with extraction**: Docling's own item
stream, no filtering, run through the *same* `quality.measure_text` /
`quality.measure_pages` / `quality.classify` as pypdf's output.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_nrb_extraction.py`:

```python
def test_docling_is_not_imported_when_the_nrb_extraction_module_loads():
    """The calibration must not drag torch into anything that imports extraction.

    A subprocess, because `sys.modules` is process-global — the same technique
    `tests/test_rag_parsing_docling.py` uses on `app.rag.parsing`.
    """
    import subprocess
    import sys

    code = (
        "import app.nrb.extraction, sys; "
        "assert not [m for m in sys.modules if m.startswith('docling')], "
        "'docling imported at module scope'; "
        "assert 'torch' not in sys.modules, 'torch imported at module scope'"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_the_shared_docling_pipeline_is_still_cpu_and_ocr_off():
    """The guard that makes the calibration trustworthy AND keeps Phase 6A honest.

    The adapter reuses `app/rag/parsing`'s converter rather than building its own,
    so if that pinning ever changes, the calibration would quietly start OCRing —
    which Phase 6A forbids outright. This fails loudly instead.
    """
    pytest.importorskip("docling")
    ok, evidence = extraction.docling_pipeline_is_native()
    assert ok, evidence


def test_docling_extract_returns_the_same_result_shape_as_pypdf(tmp_path):
    pytest.importorskip("docling")
    path = _pdf(tmp_path, ["Nepal Rastra Bank circular for all licensed banks"] * 2)
    native = extraction.extract_file(path, family="pdf", extension="pdf")
    docling = extraction.docling_extract(path)
    assert docling.parser == "docling"
    assert docling.status in quality.STATUSES
    # Same fields, so the comparison is like with like.
    assert set(docling.metrics) & set(native.metrics)
    assert docling.char_count > 0


def test_docling_extract_maps_a_textless_pdf_to_needs_ocr(tmp_path):
    pytest.importorskip("docling")
    assert extraction.docling_extract(
        _pdf(tmp_path, ["", "", ""])
    ).status == quality.STATUS_NEEDS_OCR


def test_docling_extract_does_not_apply_rag_chunk_filtering(tmp_path):
    """A short document survives here even though the RAG pipeline drops it.

    `parse_to_chunks` would raise ParseError("front matter or fragments") on a
    document this small once `drop_small_blocks` has run. The calibration must
    still see its text, or the comparison measures RAG's filter, not Docling.
    """
    pytest.importorskip("docling")
    result = extraction.docling_extract(_pdf(tmp_path, ["Short notice."]))
    assert result.status != quality.STATUS_FAILED
    assert "Short" in result.text


def test_a_corrupt_pdf_does_not_raise_out_of_docling_extract(tmp_path):
    pytest.importorskip("docling")
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.4\ngarbage")
    result = extraction.docling_extract(path)
    assert result.status == quality.STATUS_FAILED
    assert str(tmp_path) not in (result.error or "")
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/pytest tests/test_nrb_extraction.py -q -k docling
```

Expected: `AttributeError: module 'app.nrb.extraction' has no attribute 'docling_pipeline_is_native'`. The import-scope test should already pass — if it fails now, Task 4 imported docling at module scope and must be fixed first.

- [ ] **Step 3: Append the Docling adapter to `app/nrb/extraction.py`**

```python
# --------------------------------------------------------------------------- #
# Calibration: what the OTHER engine makes of the same bytes.
#
# Not part of the profiling path. This exists so "pypdf is a fair proxy for the
# native-extraction question" is a measured claim rather than an assertion — and
# so the phase question "is native Docling sufficient?" has an answer.
# --------------------------------------------------------------------------- #
def docling_pipeline_is_native() -> tuple[bool, str]:
    """Is the shared Docling pipeline still CPU-pinned with OCR off?

    Two things depend on this. The calibration is only meaningful if both engines
    read the same embedded text layer — Docling with OCR on would be measuring a
    different question entirely. And Phase 6A forbids running OCR at all, so
    reusing someone else's converter means checking what it is configured to do
    rather than assuming.

    Returns `(ok, evidence)` so the caller can print WHY it refused.
    """
    from ..rag.parsing import _pdf_pipeline_options

    options = _pdf_pipeline_options()
    device = getattr(getattr(options, "accelerator_options", None), "device", None)
    device_name = str(getattr(device, "value", device)).lower()
    ocr = bool(getattr(options, "do_ocr", False))
    return (not ocr and device_name == "cpu"), f"do_ocr={ocr}, device={device_name}"


def _docling_pages(document) -> list[str]:
    """Docling's item stream, grouped into per-page text. NO RAG filtering.

    Deliberately does NOT go through `parsing.parse_to_chunks`: that applies
    `merge_blocks`, `drop_small_blocks`, front-matter skipping and chunking on top
    of Docling, so a disagreement with pypdf could come from RAG's filter rather
    than from what Docling read off the page. Every item's text is kept, in
    document order, placed on the page `item.prov[0].page_no` reports — which is
    what makes `quality.measure_pages` applicable to both engines and the
    scanned-PDF rules comparable.
    """
    pages: dict[int, list[str]] = {}
    ordered: list[str] = []
    for item, _level in document.iterate_items():
        label = getattr(getattr(item, "label", None), "value", "") or ""
        if label == "table":
            try:
                text = item.export_to_markdown(document).strip()
            except Exception:  # noqa: BLE001 - a malformed table is not fatal
                text = ""
        else:
            text = (getattr(item, "text", "") or "").strip()
        if not text:
            continue
        prov = getattr(item, "prov", None) or []
        page = prov[0].page_no if prov else 0
        pages.setdefault(page, []).append(text)
        ordered.append(text)

    try:
        total = int(document.num_pages())
    except Exception:  # noqa: BLE001 - not every docling version exposes it
        total = max(pages) if pages else 0
    if not total:
        # No page metadata at all: fall back to one synthetic page, so the text
        # metrics still apply and only the page rules are skipped.
        return ["\n".join(ordered)] if ordered else []
    # An empty entry for a page Docling found nothing on — that IS the scanned
    # page signal, and dropping it would make coverage read as 100%.
    return ["\n".join(pages.get(number, [])) for number in range(1, total + 1)]


def docling_extract(path: Path) -> ExtractionResult:
    """Docling's native extraction, scored by the SAME rules as pypdf's.

    Reuses `parsing._docling_converter()` — a private helper, and depended on
    deliberately. Copying its three configuration lines here would create a second
    pipeline configuration that could drift, and the way it would drift is by
    silently enabling OCR. Reusing it means the calibration is pinned to whatever
    department RAG actually runs, and `docling_pipeline_is_native` fails loudly if
    that stops being CPU/no-OCR.

    `app/rag/parsing.py` itself is NOT modified: its behaviour is load-bearing for
    department RAG, and Phase 6A must not change department semantics to make NRB
    convenient.
    """
    started = time.monotonic()
    ok, evidence = docling_pipeline_is_native()
    if not ok:
        return _failed("pdf", f"docling pipeline is not native ({evidence})", started)
    try:
        from ..rag.parsing import _docling_converter

        document = _docling_converter().convert(str(path)).document
        pages = _docling_pages(document)
    except ImportError:
        return _failed("pdf", "docling is not installed (worker deps only)", started)
    except Exception as exc:  # noqa: BLE001 - a calibration must not kill a batch
        logger.warning("NRB calibrate: docling failed (%s)", type(exc).__name__)
        return _failed("pdf", type(exc).__name__, started)

    text = "\n".join(pages)
    return _result(
        parser="docling",
        family="pdf",
        evidence=quality.Evidence(
            family="pdf",
            parsed=True,
            error=None,
            text_metrics=quality.measure_text(text),
            pages=quality.measure_pages(pages) if pages else None,
            sheets=None,
        ),
        text=text,
        started=started,
    )
```

- [ ] **Step 4: Write `scripts/nrb_calibrate.py`**

```python
#!/usr/bin/env python
"""Compare pypdf and Docling NATIVE EXTRACTION over the benchmark cohort.

Phase 6A screens with pypdf (~41 pages/s) rather than Docling (~1-2 pages/s on
CPU) because both read the same embedded text layer to answer the same question.
This script is the evidence for that choice — and the answer to "is native Docling
sufficient for a meaningful percentage of the corpus?", which cannot be answered
by asserting it.

    .venv/bin/python scripts/nrb_calibrate.py --manifest docs/nrb/phase6a-manifest.json --limit 40

WHAT IS COMPARED
    Extraction, not pipelines. It does NOT call `parse_to_chunks`, which layers
    RAG's chunk merging, small-block dropping and front-matter skipping on top of
    Docling — a disagreement there could come from the filter rather than the
    parser. Both engines' raw text goes through the SAME Phase 6A metrics and
    classifier.

WHAT IS REPORTED
    Per file: both statuses and reasons, both char counts, both Devanagari ratios,
    the three core legacy-font metrics from each, and a bounded preview of each.
    In aggregate: status agreement, reason agreement, and the two ASYMMETRIC
    counts that matter more than any average —

      * DOCLING RESCUES PYPDF — pypdf says needs_ocr/suspicious, Docling says
        extracted. This is the case that would invalidate the screen.
      * PYPDF RESCUES DOCLING — the reverse.

    A single agreement percentage would hide both inside it.

REQUIRES THE WORKER DEPENDENCIES (docling, torch). A separate script so nothing in
the API path can import it. Slow by design: 40 files is minutes. Never run this
over the whole corpus.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.nrb import catalog, extraction, filestore, quality, sniff  # noqa: E402
from app.nrb.manifest import read_manifest  # noqa: E402

DEFAULT_LIMIT = 40

# The metrics printed side by side. The three after the ratios are what the
# legacy-font rule keys on, so a disagreement about "is this garbage" is
# attributable to a specific measurement rather than to a mood.
COMPARED_METRICS = (
    "char_count",
    "devanagari_ratio",
    "latin_letter_ratio",
    "stopword_rate",
    "vowelless_token_ratio",
    "intraword_symbol_ratio",
)


def _metrics(result) -> dict:
    merged = {"char_count": result.char_count}
    for name in COMPARED_METRICS:
        if name in result.metrics:
            merged[name] = result.metrics[name]
    return merged


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", default=None, metavar="PATH",
                        help="draw the calibration files from this benchmark "
                             "cohort, so calibration and profile describe the "
                             "same corpus")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"how many PDFs to compare (default {DEFAULT_LIMIT}). "
                             "Keep it small; Docling is minutes per dozen files.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

    ok, evidence = extraction.docling_pipeline_is_native()
    if not ok:
        print(f"refusing to calibrate: {evidence}. The shared Docling pipeline is "
              f"no longer CPU/no-OCR, so this would not be a native comparison — "
              f"and Phase 6A does not run OCR.", file=sys.stderr)
        return 2

    keys = read_manifest(args.manifest).keys() if args.manifest else None

    from app.db.session import SessionLocal

    async with SessionLocal() as session:
        targets = await catalog.select_extract_targets(
            session, resource_types=["pdf"], keys=list(keys) if keys else None,
            limit=args.limit, force=True,
            extractor_version=extraction.EXTRACTOR_VERSION,
        )

    base = filestore.base_dir()
    rows = []
    started = time.monotonic()
    for target in targets:
        path = filestore.resolve_path(target.storage_key, base)
        if not path.exists():
            continue
        native = extraction.extract_file(
            path, family=sniff.family_for(target.sniffed_mime),
            extension=target.extension,
        )
        docling = extraction.docling_extract(path)
        rows.append({
            "sha": target.content_sha256[:12],
            "pypdf": {"status": native.status, "reason": native.reason,
                      "ms": native.duration_ms, "preview": native.preview[:120],
                      **_metrics(native)},
            "docling": {"status": docling.status, "reason": docling.reason,
                        "ms": docling.duration_ms, "preview": docling.preview[:120],
                        **_metrics(docling)},
        })

    total = len(rows) or 1
    usable = (quality.STATUS_EXTRACTED,)
    docling_rescues = [
        r for r in rows
        if r["pypdf"]["status"] in (quality.STATUS_NEEDS_OCR, quality.STATUS_SUSPICIOUS)
        and r["docling"]["status"] in usable
    ]
    pypdf_rescues = [
        r for r in rows
        if r["docling"]["status"] in (quality.STATUS_NEEDS_OCR, quality.STATUS_SUSPICIOUS)
        and r["pypdf"]["status"] in usable
    ]
    summary = {
        "compared": len(rows),
        "status_agreement": round(
            sum(r["pypdf"]["status"] == r["docling"]["status"] for r in rows) / total, 4
        ),
        "reason_agreement": round(
            sum(r["pypdf"]["reason"] == r["docling"]["reason"] for r in rows) / total, 4
        ),
        "docling_rescues_pypdf": len(docling_rescues),
        "pypdf_rescues_docling": len(pypdf_rescues),
        "pypdf_seconds": round(sum(r["pypdf"]["ms"] for r in rows) / 1000, 1),
        "docling_seconds": round(sum(r["docling"]["ms"] for r in rows) / 1000, 1),
        "wall_seconds": round(time.monotonic() - started, 1),
        "disagreements": [
            r for r in rows if r["pypdf"]["status"] != r["docling"]["status"]
        ],
        "rows": rows,
    }
    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0

    print(f"compared             {summary['compared']}")
    print(f"status agreement     {summary['status_agreement']:.1%}")
    print(f"reason agreement     {summary['reason_agreement']:.1%}")
    print(f"pypdf total          {summary['pypdf_seconds']}s")
    print(f"docling total        {summary['docling_seconds']}s")
    print(f"speedup              "
          f"{summary['docling_seconds'] / max(summary['pypdf_seconds'], 1e-9):.0f}x")
    print()
    print(f"DOCLING RESCUES PYPDF  {summary['docling_rescues_pypdf']}   "
          f"<- this is the number that would invalidate the screen")
    print(f"PYPDF RESCUES DOCLING  {summary['pypdf_rescues_docling']}")
    if summary["disagreements"]:
        print(f"\nDISAGREEMENTS ({len(summary['disagreements'])}) — read every one:")
        for row in summary["disagreements"]:
            print(f"\n  {row['sha']}")
            for engine in ("pypdf", "docling"):
                side = row[engine]
                metrics = " ".join(
                    f"{name}={side.get(name)}" for name in COMPARED_METRICS
                    if name in side
                )
                print(f"    {engine:<8} {side['status']}/{side['reason']}  {metrics}")
                print(f"             {side['preview'][:100]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [ ] **Step 5: Run the calibration tests and the RAG regression gate**

```bash
.venv/bin/pytest tests/test_nrb_extraction.py -q
.venv/bin/pytest tests/test_rag_parsing.py tests/test_rag_parsing_docling.py -q
```

Expected: all PASS. The RAG suites must be **unchanged** — `app/rag/parsing.py` was not touched, and if any of it moves, something imported it in a way that altered its behaviour.

- [ ] **Step 6: Commit**

```bash
git add app/nrb/extraction.py scripts/nrb_calibrate.py tests/test_nrb_extraction.py
git commit -m "feat(nrb): pypdf-vs-Docling extraction calibration, same metrics both sides"
```

---

### Task 12: Postgres integration tests

**Files:**
- Create: `tests/test_nrb_extract_integration.py`

**Interfaces:**
- Consumes: everything from Tasks 4-9.
- Produces: nothing consumed downstream.

Read `tests/test_nrb_sync_integration.py` first and copy its fixture verbatim — the isolation rule is load-bearing: the NRB catalog is global with no department to scope a fixture to, so every test runs inside a rolled-back transaction (`join_transaction_mode="create_savepoint"`) and clears the `nrb_*` tables *inside* it. A test that really committed would corrupt a developer's catalog.

- [ ] **Step 1: Write the tests**

```python
"""Extraction against real Postgres.

ISOLATION, copied from `test_nrb_sync_integration.py` and for the same reason:
the NRB catalog is GLOBAL. There is no department to scope a fixture to, so every
test here runs inside a transaction that is rolled back, and clears the nrb_*
tables *inside* it. A test that really committed would wipe a developer's
18,577-source catalog.
"""

import asyncio
import hashlib
import os

import pytest
from fpdf import FPDF
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.nrb import catalog, extraction, filestore
from app.nrb.extract import run_extract
from app.nrb.models import NRBExtraction, NRBFile, NRBSource, NRBSourceFile

DATABASE_URL = os.environ.get("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL.startswith("postgresql"), reason="needs Postgres"
)


def _blob(base, body_pages, ext="pdf"):
    """Write a real content-addressed blob and return (sha256, storage_key)."""
    pdf = FPDF()
    for body in body_pages:
        pdf.add_page()
        if body:
            pdf.set_font("helvetica", size=12)
            pdf.cell(0, 10, body)
    raw = bytes(pdf.output())
    sha = hashlib.sha256(raw).hexdigest()
    key = filestore.storage_key_for(sha, ext)
    path = filestore.resolve_path(key, base)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return sha, key, len(raw)


async def _seed(session, base, *, title, sha, key, length, doc_type="circular",
                year=2024, resource_type="pdf", extension="pdf"):
    source = NRBSource(
        page_url=f"https://www.nrb.org.np/bfr/{title}/",
        url_key=f"https://www.nrb.org.np/bfr/{title}",
        title=title, document_type=doc_type, owner="bfr",
        metadata_hash=hashlib.sha256(title.encode()).hexdigest(),
        published_at=__import__("datetime").datetime(year, 6, 1,
                                                     tzinfo=__import__("datetime").timezone.utc),
    )
    session.add(source)
    await session.flush()
    blob = NRBFile(
        comparison_key=f"https://www.nrb.org.np/uploads/{sha[:8]}.{extension}",
        source_url=f"https://www.nrb.org.np/uploads/{sha[:8]}.{extension}",
        resource_type=resource_type, type_source="mime", host="www.nrb.org.np",
        extension=extension, fetch_status="fetched", content_sha256=sha,
        content_length=length, storage_key=key, sniffed_mime="application/pdf",
    )
    session.add(blob)
    await session.flush()
    session.add(NRBSourceFile(source_id=source.id, file_id=blob.id,
                              ordinal=0, relationship_type="primary"))
    await session.flush()
    return source, blob


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A rolled-back session plus a throwaway blob store.

    A NullPool engine per call, not the app's module-level one: that pools
    connections bound to the first event loop, and each `asyncio.run` makes a new
    one — the second test would die with "Event loop is closed".
    """
    monkeypatch.setattr(filestore, "base_dir", lambda: tmp_path)
    engine = create_async_engine(DATABASE_URL, poolclass=NullPool)
    yield engine, tmp_path
    asyncio.run(engine.dispose())


def _run(engine, base, coro_factory):
    async def go():
        async with engine.connect() as connection:
            transaction = await connection.begin()
            factory = async_sessionmaker(
                bind=connection, expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )
            async with factory() as session:
                for table in ("nrb_extractions", "nrb_source_files",
                              "nrb_files", "nrb_sources"):
                    await session.execute(text(f"DELETE FROM {table}"))
                await session.flush()
                result = await coro_factory(session, factory, base)
            await transaction.rollback()
            return result

    return asyncio.run(go())


def test_an_extraction_is_persisted_with_its_metrics(db):
    engine, base = db

    async def scenario(session, factory, base):
        sha, key, length = _blob(base, ["Nepal Rastra Bank circular for all banks"] * 2)
        await _seed(session, base, title="circular one", sha=sha, key=key, length=length)
        await run_extract(limit=10, engine=engine, session_factory=lambda: session)
        row = (await session.execute(select(NRBExtraction))).scalars().one()
        return row

    row = _run(engine, base, scenario)
    assert row.status in ("extracted", "suspicious", "needs_ocr")
    assert row.extractor_version == extraction.EXTRACTOR_VERSION
    assert row.metrics
    assert row.page_count == 2
    assert row.preview


def test_a_repeat_pass_creates_no_duplicate_row(db):
    engine, base = db

    async def scenario(session, factory, base):
        sha, key, length = _blob(base, ["Nepal Rastra Bank circular for all banks"])
        await _seed(session, base, title="circular two", sha=sha, key=key, length=length)
        await run_extract(limit=10, engine=engine, session_factory=lambda: session)
        second = await run_extract(limit=10, engine=engine,
                                   session_factory=lambda: session)
        rows = (await session.execute(select(NRBExtraction))).scalars().all()
        return second, rows

    second, rows = _run(engine, base, scenario)
    assert len(rows) == 1
    assert second.counters["blobs_selected"] == 0   # nothing left to do


def test_bumping_the_extractor_version_makes_the_result_selectable_again(db, monkeypatch):
    engine, base = db

    async def scenario(session, factory, base):
        sha, key, length = _blob(base, ["Nepal Rastra Bank circular for all banks"])
        await _seed(session, base, title="circular three", sha=sha, key=key, length=length)
        await run_extract(limit=10, engine=engine, session_factory=lambda: session)
        monkeypatch.setattr(extraction, "EXTRACTOR_VERSION", "native-2")
        again = await run_extract(limit=10, engine=engine,
                                  session_factory=lambda: session)
        rows = (await session.execute(select(NRBExtraction))).scalars().all()
        return again, rows

    again, rows = _run(engine, base, scenario)
    assert again.counters["blobs_selected"] == 1
    assert {r.extractor_version for r in rows} == {"native-1", "native-2"}


def test_two_file_rows_sharing_a_blob_produce_ONE_extraction(db):
    """The identity decision, asserted. Two URLs, identical bytes, one verdict."""
    engine, base = db

    async def scenario(session, factory, base):
        sha, key, length = _blob(base, ["Nepal Rastra Bank circular for all banks"])
        await _seed(session, base, title="नेपाल राष्ट्र बैंक परिपत्र",
                    sha=sha, key=key, length=length)
        # A second source + file row pointing at the SAME bytes under a different
        # URL, with a Latin title. If the title fed the verdict, the answer would
        # depend on which of these was processed first.
        source = NRBSource(
            page_url="https://www.nrb.org.np/bfr/english-title/",
            url_key="https://www.nrb.org.np/bfr/english-title",
            title="Unified Directive 2024", document_type="directive", owner="bfr",
            metadata_hash="b" * 64,
        )
        session.add(source)
        await session.flush()
        twin = NRBFile(
            comparison_key="https://www.nrb.org.np/uploads/other-name.pdf",
            source_url="https://www.nrb.org.np/uploads/other-name.pdf",
            resource_type="pdf", type_source="mime", host="www.nrb.org.np",
            extension="pdf", fetch_status="fetched", content_sha256=sha,
            content_length=length, storage_key=key, sniffed_mime="application/pdf",
        )
        session.add(twin)
        await session.flush()
        session.add(NRBSourceFile(source_id=source.id, file_id=twin.id,
                                  ordinal=0, relationship_type="primary"))
        await session.flush()

        result = await run_extract(limit=10, engine=engine,
                                   session_factory=lambda: session)
        rows = (await session.execute(select(NRBExtraction))).scalars().all()
        return result, rows

    result, rows = _run(engine, base, scenario)
    assert result.counters["blobs_selected"] == 1   # DISTINCT on content_sha256
    assert len(rows) == 1


def test_a_missing_blob_is_recorded_failed_and_the_batch_continues(db):
    engine, base = db

    async def scenario(session, factory, base):
        sha, key, length = _blob(base, ["Nepal Rastra Bank circular for all banks"])
        await _seed(session, base, title="good", sha=sha, key=key, length=length)
        # A row that claims to be fetched, whose blob is not on disk.
        ghost_sha = "c" * 64
        ghost = NRBFile(
            comparison_key="https://www.nrb.org.np/uploads/ghost.pdf",
            source_url="https://www.nrb.org.np/uploads/ghost.pdf",
            resource_type="pdf", type_source="mime", host="www.nrb.org.np",
            extension="pdf", fetch_status="fetched", content_sha256=ghost_sha,
            content_length=10, storage_key=filestore.storage_key_for(ghost_sha, "pdf"),
            sniffed_mime="application/pdf",
        )
        session.add(ghost)
        await session.flush()
        result = await run_extract(limit=10, engine=engine,
                                   session_factory=lambda: session)
        rows = (await session.execute(select(NRBExtraction))).scalars().all()
        return result, rows

    result, rows = _run(engine, base, scenario)
    assert result.counters["blobs_processed"] == 2   # one bad file did not stop it
    assert result.counters["failed"] == 1
    failed = [r for r in rows if r.status == "failed"][0]
    assert "not on disk" in (failed.error or "")


def test_a_corrupt_blob_is_failed_rather_than_parsed(db):
    engine, base = db

    async def scenario(session, factory, base):
        sha, key, length = _blob(base, ["Nepal Rastra Bank circular for all banks"])
        await _seed(session, base, title="corrupt", sha=sha, key=key, length=length)
        # Overwrite the blob so it no longer hashes to its own storage key.
        filestore.resolve_path(key, base).write_bytes(b"%PDF-1.4\nnot the same bytes")
        result = await run_extract(limit=10, engine=engine,
                                   session_factory=lambda: session)
        rows = (await session.execute(select(NRBExtraction))).scalars().all()
        return result, rows

    result, rows = _run(engine, base, scenario)
    assert result.counters["failed"] == 1
    assert "hash" in (rows[0].error or "").lower()


def test_a_dry_run_writes_nothing(db):
    engine, base = db

    async def scenario(session, factory, base):
        sha, key, length = _blob(base, ["Nepal Rastra Bank circular for all banks"])
        await _seed(session, base, title="dry", sha=sha, key=key, length=length)
        result = await run_extract(limit=10, dry_run=True, engine=engine,
                                   session_factory=lambda: session)
        rows = (await session.execute(select(NRBExtraction))).scalars().all()
        return result, rows

    result, rows = _run(engine, base, scenario)
    assert result.counters["blobs_selected"] == 1
    assert rows == []


def test_only_fetched_files_are_selectable(db):
    engine, base = db

    async def scenario(session, factory, base):
        pending = NRBFile(
            comparison_key="https://www.nrb.org.np/uploads/pending.pdf",
            source_url="https://www.nrb.org.np/uploads/pending.pdf",
            resource_type="pdf", type_source="mime", host="www.nrb.org.np",
            extension="pdf", fetch_status="pending",
        )
        blocked = NRBFile(
            comparison_key="http://uat.nrb.org.np/uploads/blocked.pdf",
            source_url="http://uat.nrb.org.np/uploads/blocked.pdf",
            resource_type="pdf", type_source="mime", host="uat.nrb.org.np",
            extension="pdf", fetch_status="blocked_host",
            blocked_reason="host is not the approved NRB host",
        )
        session.add_all([pending, blocked])
        await session.flush()
        return await catalog.select_extract_targets(
            session, extractor_version=extraction.EXTRACTOR_VERSION
        )

    assert _run(engine, base, scenario) == []


def test_the_profile_query_breaks_2019_out_as_its_own_cohort(db):
    engine, base = db

    async def scenario(session, factory, base):
        from app.nrb.profile import load_profile

        for year, title in ((2019, "old one"), (2024, "new one")):
            sha, key, length = _blob(base,
                                     [f"Nepal Rastra Bank circular {year} for banks"])
            await _seed(session, base, title=title, sha=sha, key=key,
                        length=length, year=year)
        await run_extract(limit=10, engine=engine, session_factory=lambda: session)
        return await load_profile(session,
                                  extractor_version=extraction.EXTRACTOR_VERSION)

    profile = _run(engine, base, scenario)
    assert "2019" in profile["by_cohort"]
    assert "2023-2026" in profile["by_cohort"]


def test_the_title_signal_is_reported_but_never_stored(db):
    engine, base = db

    async def scenario(session, factory, base):
        from app.nrb.profile import load_profile

        sha, key, length = _blob(base, ["ffihW ffifiHrz reU=,. iqrn rrq qtq " * 30])
        await _seed(session, base, title="नेपाल राष्ट्र बैंक परिपत्र",
                    sha=sha, key=key, length=length)
        await run_extract(limit=10, engine=engine, session_factory=lambda: session)
        row = (await session.execute(select(NRBExtraction))).scalars().one()
        profile = await load_profile(
            session, extractor_version=extraction.EXTRACTOR_VERSION
        )
        return row, profile

    row, profile = _run(engine, base, scenario)
    # The stored row knows nothing about the title...
    assert not hasattr(row, "title")
    assert "title" not in (row.metrics or {})
    # ...but the report can still corroborate.
    assert "with_devanagari_title" in profile["title_corroboration"]
```

- [ ] **Step 2: Run the integration suite**

```bash
DATABASE_URL='postgresql+asyncpg://gateway:<pw>@127.0.0.1:5432/local_ai_gateway_p4' \
  .venv/bin/pytest tests/test_nrb_extract_integration.py -q
```

Expected: PASS, 11 tests. If `run_extract`'s `session_factory=lambda: session` does not work as a factory (it returns the same session rather than an async context manager), wrap it: `lambda: contextlib.nullcontext(session)` — check `test_nrb_fetch_integration.py` for how Phase 5 solved the same problem and copy that exactly.

- [ ] **Step 3: Verify the catalog was NOT modified by the test run**

```bash
PGPASSWORD=postgres psql -h 127.0.0.1 -U postgres -d local_ai_gateway_p4 -At -c \
  "SELECT count(*) FROM nrb_sources; SELECT count(*) FROM nrb_files;"
```

Expected: 18,577 and 18,266 — unchanged. If either is 0, the rollback isolation is broken and must be fixed before anything else.

- [ ] **Step 4: Commit**

```bash
git add tests/test_nrb_extract_integration.py
git commit -m "test(nrb): extraction integration suite against Postgres (Phase 6A)"
```

---

### Task 13: The live profile — draw the manifest, fetch it exactly, measure it

The point of the whole phase. Everything before this was scaffolding.

**Files:**
- Create: `docs/nrb/phase6a-manifest.json` (committed — the benchmark definition)
- Create: `docs/nrb/phase6a-profile.txt`, `docs/nrb/phase6a-calibration.txt`
- Test: no unit test — the deliverable is measurements.

**Interfaces:**
- Consumes: `scripts/nrb_sample.py`, `scripts/nrb_fetch.py --manifest`, `scripts/nrb_extract.py --manifest`, `scripts/nrb_calibrate.py --manifest`.
- Produces: the numbers Task 14 writes into `docs/nrb-integration.md`.

Approved budget: **~400 files, ~400 MB.** Do not exceed it because a stratum is
sparse — report the sparse stratum instead. **The cohort is drawn once, in Step 1,
and never re-drawn.** Every later step names the manifest file.

- [ ] **Step 1: Draw the cohort — once**

```bash
export DATABASE_URL='postgresql+asyncpg://gateway:<pw>@127.0.0.1:5432/local_ai_gateway_p4'
.venv/bin/python scripts/nrb_sample.py --size 400 --out docs/nrb/phase6a-manifest.json
```

Read the printed strata table before going further. Sanity checks:

- 2019 present and at ~30%, not ~50% — the cap worked.
- `<=2018`, `2020-2022`, `2023-2026` all present.
- `act`, `rule_bylaw`, `monetary_policy`, `guideline_manual`, `directive`,
  `circular` all present. Several will be marked WEAK; that is expected and gets
  reported, not fixed.
- `pdf`, `spreadsheet`, `image` and `document` all present.
- `SHORTFALL` is 0, or its note explains which constraint bound.

If any of that is wrong, fix the sampler and re-draw **now** — this is the only
moment at which re-drawing is free. Once files are fetched against this manifest,
re-drawing means the profile and the download no longer describe the same cohort.

Commit the manifest immediately, before anything is fetched:

```bash
git add docs/nrb/phase6a-manifest.json
git commit -m "docs(nrb): Phase 6A benchmark cohort — 400 files, drawn once"
```

- [ ] **Step 2: Fetch exactly that cohort**

```bash
export DATABASE_URL='postgresql+asyncpg://gateway:<pw>@127.0.0.1:5432/local_ai_gateway_p4'

# preview first — --dry-run makes NO HTTP request at all
.venv/bin/python scripts/nrb_fetch.py --manifest docs/nrb/phase6a-manifest.json --dry-run

# then the real pass, byte-capped as a belt-and-braces bound on top of the manifest
.venv/bin/python scripts/nrb_fetch.py --manifest docs/nrb/phase6a-manifest.json \
    --max-bytes 500000000 -v
```

One command, not thirteen scoped approximations. The manifest names the exact
files; Phase 5's host guard, HTTPS requirement, pacing, byte cap, redirect
refusal, soft-404 rule, advisory lock and resumability all apply unchanged, and a
manifest entry whose file is `blocked_host` still cannot be selected.

If the pass is interrupted, **re-run the identical command** — selection is
`pending`-only, so it resumes rather than restarting.

Record `files_fetched`, `files_failed`, `files_deduplicated` and the failure
samples. A cluster of soft-404s means NRB moved files and `nrb_sync.py` should be
re-run first; a cluster of timeouts means back off `NRB_CRAWL_DELAY_SECONDS`.

Then confirm coverage of the cohort specifically:

```bash
PGPASSWORD=postgres psql -h 127.0.0.1 -U postgres -d local_ai_gateway_p4 -At -c \
  "SELECT fetch_status, count(*) FROM nrb_files GROUP BY 1;
   SELECT count(DISTINCT content_sha256) FROM nrb_files WHERE fetch_status='fetched';"
du -sh nrb_files
```

Any manifest file that did not fetch is a **stated gap**, not a silent one — note
its count and reason. `nrb_extract.py` will warn about the same number.

- [ ] **Step 3: Run the profile over the cohort**

```bash
export DATABASE_URL='postgresql+asyncpg://gateway:<pw>@127.0.0.1:5432/local_ai_gateway_p4'
.venv/bin/python scripts/nrb_extract.py --manifest docs/nrb/phase6a-manifest.json -v \
  | tee <scratchpad>/nrb_profile.txt

.venv/bin/python scripts/nrb_extract.py --manifest docs/nrb/phase6a-manifest.json --json \
  > <scratchpad>/nrb_profile.json
```

The second invocation selects zero (everything is already extracted at this
version) and re-renders the profile from the database — which is also the
resumability check.

Note the gap between the manifest size and `blobs_selected`: manifest entries
that share bytes collapse to one blob. That is correct, and both numbers get
reported.

- [ ] **Step 4: Calibrate against Docling, over the same cohort**

```bash
DATABASE_URL='postgresql+asyncpg://gateway:<pw>@127.0.0.1:5432/local_ai_gateway_p4' \
  .venv/bin/python scripts/nrb_calibrate.py \
    --manifest docs/nrb/phase6a-manifest.json --limit 40 \
  | tee <scratchpad>/nrb_calibration.txt
```

Record the status agreement, the reason agreement, the wall-clock ratio, and
above all the two asymmetric counts. **Read every disagreement**, with both
engines' metrics and previews side by side.

`DOCLING RESCUES PYPDF` is the number that would invalidate the screen. If it is
more than a couple of files, say so plainly and treat the "pypdf is a fair proxy"
claim as **not established** — the profile's numbers then become a lower bound on
usability, and that caveat goes in the report and in `docs/nrb-integration.md`.
Do not average it away.

- [ ] **Step 5: Manually validate the heuristic against the real documents**

Take 5 blobs classified `extracted`, 5 `suspicious`, 5 `needs_ocr`, from the
cohort:

```bash
PGPASSWORD=postgres psql -h 127.0.0.1 -U postgres -d local_ai_gateway_p4 -c \
 "SELECT e.status, e.reason, f.storage_key, left(s.title, 40), e.preview
    FROM nrb_extractions e
    JOIN nrb_files f ON f.content_sha256 = e.content_sha256
    JOIN nrb_source_files sf ON sf.file_id = f.id
    JOIN nrb_sources s ON s.id = sf.source_id
   WHERE e.status = 'suspicious' LIMIT 5;"
```

Open each `nrb_files/<storage_key>` and compare what the page *looks* like with
what was extracted. Record honestly:

- **False positives** — a genuinely readable document called `suspicious`. Each is
  a threshold that is too tight.
- **False negatives** — garbage called `extracted`. Each is a threshold that is
  too loose, and this is the dangerous direction.

Do **not** tune a threshold to make an individual case pass. If a threshold
genuinely needs to move: move it, bump `EXTRACTOR_VERSION` to `native-2`, re-run
the profile over **the same manifest**, and report both sets of numbers.

- [ ] **Step 6: Record throughput and the corpus-level extrapolation**

From the profile: files/minute, pages/minute, and the share of the cohort in each
status. State the extrapolation to the 18,263-file corpus as an estimate **with
the cohort's strata and its weak cells named**, never as a measured fact.

- [ ] **Step 7: Commit the evidence**

```bash
mkdir -p docs/nrb
cp <scratchpad>/nrb_profile.txt docs/nrb/phase6a-profile.txt
cp <scratchpad>/nrb_calibration.txt docs/nrb/phase6a-calibration.txt
git add docs/nrb/
git commit -m "docs(nrb): Phase 6A live extraction profile and Docling calibration"
```

---

### Task 14: Documentation

**Files:**
- Modify: `docs/nrb-integration.md` (add §11, update §1's status table and §3's verify block)
- Modify: `CLAUDE.md` (add the Phase 6A gotcha paragraph and the new module list)

- [ ] **Step 1: Update `docs/nrb-integration.md` §1**

Change the Phase 6 row to split 6A/6B:

```markdown
| 6A | Native extraction + deterministic quality profiling | **Done, live-profiled 2026-08-15** — §11 |
| 6B | OCR / fallback extraction strategy | Not started — gate in §11.9 |
```

- [ ] **Step 2: Write §11 of `docs/nrb-integration.md`**

Follow §9 and §10's shape exactly: Files, the design decision and why the brief was not followed, the live numbers as a table, special cases, and an "Evaluation & Improvement" block with the four numbered points (success metric, eval, feedback capture, review loop). Include:

- The pypdf-vs-Docling decision **with the measured speed ratio, the status and
  reason agreement rates, and the two rescue counts** — and the note that the
  comparison is extraction-vs-extraction, not against `parse_to_chunks`, because
  that would have measured RAG's chunk filtering.
- The benchmark manifest: that the cohort was drawn once from the full catalog and
  is committed at `docs/nrb/phase6a-manifest.json`, so the profile is re-runnable
  rather than merely reported — plus any manifest files that did not fetch.
- The content-intrinsic identity decision and the shared-blob reason.
- The full status table, by cohort with **2019 broken out**, by document type, by format.
- The legacy-font detector's rule, its thresholds, and its measured false-positive/false-negative findings from Task 13 Step 5, stated honestly.
- The `.xls`/`.doc` gap with exact counts.
- §11.9 "The Phase 6B gate": what Phase 6A leaves it (a work queue that is a query — `nrb_extractions WHERE status IN ('needs_ocr','suspicious')`), what is still undecided, and the recommendation **from the measurements only**.

- [ ] **Step 3: Add the verify commands to §3**

```bash
# Phase 6A suites (pure)
.venv/bin/pytest tests/test_nrb_quality.py tests/test_nrb_extraction.py \
                 tests/test_nrb_sampling.py tests/test_nrb_manifest.py \
                 tests/test_nrb_extract_cli.py tests/test_nrb_extraction_report.py

# Phase 6A integration (needs Postgres; every test rolls back)
.venv/bin/pytest tests/test_nrb_extract_integration.py

# the benchmark cohort — drawn ONCE; --force re-draws and invalidates comparisons
.venv/bin/python scripts/nrb_sample.py --size 400 --out docs/nrb/phase6a-manifest.json

# fetch exactly that cohort (network; every Phase 5 safety rule still applies)
.venv/bin/python scripts/nrb_fetch.py --manifest docs/nrb/phase6a-manifest.json --dry-run

# the profile itself. Scope is REQUIRED; --dry-run parses nothing.
.venv/bin/python scripts/nrb_extract.py --core --dry-run
.venv/bin/python scripts/nrb_extract.py --manifest docs/nrb/phase6a-manifest.json -v

# the honesty check on using pypdf rather than Docling (worker deps, slow)
.venv/bin/python scripts/nrb_calibrate.py --manifest docs/nrb/phase6a-manifest.json --limit 40
```

- [ ] **Step 4: Add the `CLAUDE.md` gotcha**

Add to the Conventions/gotchas list, in the same voice as the existing NRB entries — lead with the fact that is expensive to re-derive:

```markdown
- **A parser returning text does NOT mean the extraction is good, and on NRB's
  regulatory core it usually means the opposite.** Phase 6A
  (`app/nrb/{quality,extraction,extract,sampling,profile}.py`, run by
  `scripts/nrb_extract.py`) screens fetched blobs with **pypdf, not Docling** —
  measured ~41 pages/s vs ~1-2 on CPU, reading the same embedded text layer to
  answer the same question, with `scripts/nrb_calibrate.py` measuring the
  agreement rate so that claim stays evidence rather than assertion. Docling's
  layout/table/provenance work is Phase 7's need, not Phase 6A's.
  `app/rag/parsing.py` is untouched. Three things a rewrite must not lose:
  (1) **`nrb_extractions` is keyed on `(content_sha256, extractor_version)` and
  every column is a function of the BYTES alone** — a blob is shared across
  sources, so letting a source title feed the persisted verdict would store a
  different answer depending on which source the pass reached first; the
  title-corroboration signal therefore lives in `profile.py`, computed over ALL
  referencing sources at read time; (2) **no extracted text is persisted**, only
  a <=300-char preview, because Phase 7 re-parses with Docling anyway and a
  cached text artefact is something a later phase would eventually embed by
  accident; (3) the legacy-font detector **gates on `latin_letter_ratio` before
  it reads `stopword_rate`** — a numeric statistical table also scores zero
  English stopwords, and without that gate every table in the corpus reads as
  garbage. Ties break toward `suspicious`, never `extracted`.
- **The Phase 6A cohort is a committed FILE, drawn once** —
  `docs/nrb/phase6a-manifest.json` (`app/nrb/manifest.py`, written by
  `scripts/nrb_sample.py`), and `nrb_fetch.py`/`nrb_extract.py`/`nrb_calibrate.py`
  all take `--manifest`. Do not go back to approximating it with
  `--section`/`--year`/`--limit` and re-sampling what lands: Phase 5 selects
  `pending` rows in **id order**, which is REST paging order, so stratifying over
  the result measures the id order rather than the corpus — and it is not
  reproducible, because any later fetch changes what there is to re-sample.
  `sampling.stratified_sample` is floor-round-robin -> proportional -> cohort cap
  -> **redistribute what the cap removed** (without that last pass a 400 request
  silently returns ~250), and it reports a `shortfall` rather than rounding down.
- **The Docling calibration compares extraction with extraction, never against
  `parse_to_chunks`.** That function layers `merge_blocks`, `drop_small_blocks`
  and front-matter skipping on top of Docling, so a disagreement measured through
  it could come from RAG's chunk filter rather than from what Docling read off the
  page. `extraction.docling_extract` reuses `parsing._docling_converter()` —
  private, and depended on deliberately, because it carries the CPU pinning and
  `do_ocr=False` and a copied config could drift into enabling OCR — then walks
  `iterate_items()` itself and scores the text with the **same**
  `quality.measure_text`/`classify` as pypdf. `docling_pipeline_is_native()` is
  the guard that fails loudly if that pinning ever changes.
```

Also add `quality`+`extraction`+`extract`+`sampling`+`profile` to the `app/nrb/` line in the Layout section.

- [ ] **Step 5: Run the complete test suite and report honestly**

```bash
.venv/bin/pytest tests/test_nrb_quality.py tests/test_nrb_extraction.py \
    tests/test_nrb_sampling.py tests/test_nrb_manifest.py \
    tests/test_nrb_extract_cli.py tests/test_nrb_extraction_report.py \
    tests/test_files_documents_pdf_pages.py -q
.venv/bin/pytest tests/ -k nrb -q
DATABASE_URL='postgresql+asyncpg://gateway:<pw>@127.0.0.1:5432/local_ai_gateway_p4' \
  .venv/bin/pytest -q
```

Report three numbers separately: the Phase 6A count, all NRB tests, and the full suite. **`tests/test_rag_reingest_integration.py::test_department_filter_restricts_the_set` fails on a dirty database and is a known pre-existing failure** (documented in `docs/nrb-integration.md` §9.10, verified against a stashed tree with no NRB code present). Report it as pre-existing and unrelated; do not fix it here.

- [ ] **Step 6: Commit**

```bash
git add docs/nrb-integration.md CLAUDE.md
git commit -m "docs(nrb): Phase 6A — extraction quality profiling, measured"
```

---

## Self-review notes

Checked against the spec, section by section, after the three amendments.

**Spec coverage.** §3 architecture -> Tasks 1, 2, 4, 7, 7A, 8, 9, 10. §3.1
pypdf/Docling, extraction-vs-extraction -> Tasks 4 and 11. §3.2 the one refactor
-> Task 3. §4 format behaviour -> Task 4 (a test per format). §5 metrics -> Task 1
(every listed metric is a `TextMetrics`, `PageStats` or `SheetStats` field). §6
status rules -> Task 2, one test per rule. §7 legacy-font -> Task 2's
`looks_like_legacy_font`, thresholds named as constants. §8 metadata-assisted, not
persisted -> Tasks 5, 9, 12. §9 persistence -> Task 5. §10 sampling, four passes
-> Task 7. §10.1 the manifest -> Task 7A, consumed by Tasks 6, 8, 10, 11, 13. §11
CLI -> Task 10, plus the `--year`/`--manifest` fetch changes in Task 6. §12
failure isolation and safety -> Task 8's `_extract_target` and Task 12's
missing/corrupt blob tests. §13 tests -> Tasks 1, 2, 4, 7, 7A, 9, 10, 12. §14 live
profile -> Task 13. §15 out of scope -> the Global Constraints. §16 Evaluation &
Improvement -> Task 14 Step 2.

**The three amendments, and where each is pinned by a test.**

1. *Exact benchmark manifest.* `app/nrb/manifest.py` + `scripts/nrb_sample.py`
   (Task 7A); exact-key scope on the fetch (Task 6 Step 1) and on the extract
   (Task 6 Step 5); `--manifest` on all three CLIs; Task 13 Step 2 is ONE fetch
   command against the manifest rather than thirteen scoped approximations.
   Pinned by `test_a_manifest_is_a_scope_on_its_own_and_its_keys_are_passed_through`,
   `test_the_cli_never_re_draws_a_cohort` (there is no `--sample` flag any more)
   and the manifest round-trip suite. `count_unfetched` makes a partly-downloaded
   cohort a stated number rather than a silent shrink.
2. *Allocation redistribution.* Pass 1 is round-robin (not a `for … break` over a
   sorted list), pass 4 hands back every cap-trimmed slot, and `Sample` gained
   `shortfall` + `notes`. Pinned by the six new tests in Task 7 — exactly-400,
   redistribution, cap-still-holds, infeasible-reports-shortfall, corpus-smaller,
   and the floor-larger-than-budget test that asserts 10 strata represented where
   lexicographic filling would give 3.
3. *Calibration at the extraction level.* `docling_extract` walks
   `iterate_items()` through `parsing._docling_converter()` and scores it with the
   same metrics; `parse_to_chunks` is not called anywhere in the calibration path.
   `docling_pipeline_is_native()` guards the CPU/no-OCR pinning. Pinned by
   `test_docling_extract_does_not_apply_rag_chunk_filtering` (a short document
   that `drop_small_blocks` would delete must still be visible) and
   `test_the_shared_docling_pipeline_is_still_cpu_and_ocr_off`.

**Type consistency.** `EXTRACTOR_VERSION` is defined once (Task 4) and imported
everywhere. `ExtractionResult`'s field list in Task 4 matches every construction
site in Task 8's `_extract_target` and Task 11's `docling_extract`.
`catalog.ExtractTarget`'s field order matches the `select_extract_targets` column
order that unpacks into it. `quality.Evidence`'s six positional fields match every
call site. **`keys` means `comparison_key` in every signature it appears in** —
`select_fetch_targets`, `select_extract_targets`, `count_unfetched`, `run_fetch`,
`run_extract`, `Manifest.keys()` — which is the one identity that was ambiguous in
the first draft, and the reason `content_sha256` is never called `keys` anywhere.
`MANIFEST_MAX_KEYS` is defined in both `catalog.py` and `manifest.py`; the values
must match, and the plan says so in both places.

**Known soft spots, flagged rather than hidden.**
1. Task 12's `session_factory=lambda: session` may not satisfy `run_extract`'s
   `async with session_factory()`. The step says to copy Phase 5's solution from
   `test_nrb_fetch_integration.py` rather than invent one.
2. Task 4's spreadsheet test has one convoluted assertion; the step tells the
   implementer to simplify it to `assert "400" not in result.text`.
3. `_docling_pages` depends on `document.num_pages()` and `item.prov[0].page_no`,
   verified against docling 2.118 in `app/rag/parsing.py` but wrapped in
   `try/except` here because a version bump could move them. If page metadata is
   unavailable the comparison degrades to text-only and the page rules are
   skipped — stated in the code, not silent.
4. `parsing._docling_converter` and `_pdf_pipeline_options` are private. Depending
   on them is deliberate (a copied config could drift into enabling OCR) and
   `docling_pipeline_is_native()` turns a breaking change into a loud failure —
   but it is still a private dependency, and a future maintainer should know it.
5. The legacy-font thresholds are calibrated against **one** cohort (49
   circulars). Task 13 Step 5 is where they meet documents that are not
   circulars, and the plan forbids tuning them per-case without a version bump
   and a re-run over the same manifest.
