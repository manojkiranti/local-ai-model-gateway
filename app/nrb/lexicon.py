"""Vocabulary evidence: is this line real English, and is that real Nepali?

Two questions the structural rules in `devanagari.py` provably cannot answer, and
the spike measured both failures on real benchmark files:

  * `(Rs. in crore)` converts to `९च्क। ष्ल अचयचभ०` — **zero** illegal clusters,
    **zero** latin residue. Structurally impeccable Devanagari, and it used to be
    an English table cell. Only knowing that "rs", "in" and "crore" are English
    words stops the converter touching that line.
  * `@)^%` converts to `२०६५` under Preeti (correct) and `द्दण्टछ` under
    FONTASY_HIMALI_TT (nonsense). Both are legal Devanagari and the WRONG one
    scores the higher `devanagari_ratio`. Only knowing that २०६५ is a plausible
    Nepali year distinguishes them.

**The vocabulary is derived from the NRB corpus itself, not from a dictionary
package.** Three reasons, in order of importance: it is reproducible from files
we already have (a system word list differs per machine); it carries the domain
(`crore`, `rastra`, `bittiya`, `निर्देशन`, `परिपत्र`) that a general dictionary
does not; and it has no licence attached, unlike this phase's converter.

**It is drawn only from `extracted`/`clean` blobs — never from `suspicious`
ones.** That keeps the population it is fitted on disjoint from the population it
is evaluated against, which is the same discipline §11.9 demands of the legacy
threshold: a measure tuned on the cohort it later judges has measured nothing.

The artifact is frozen and fingerprinted (`scripts/nrb_build_lexicon.py`), so a
conversion verdict can be reproduced against a named vocabulary rather than
against whatever was on disk that day.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "ENGLISH_MIN_RATE",
    "ENGLISH_MIN_RUNS",
    "Lexicon",
    "LexiconError",
    "MIN_DOCUMENT_FREQUENCY",
    "build_lexicon",
    "english_acronyms",
    "english_harvest",
    "english_runs",
    "english_word_rate",
    "is_confidently_english",
    "lexicon_fingerprint",
    "load_lexicon",
    "nepali_tokens",
    "nepali_word_rate",
]

LEXICON_VERSION = "nrb-lexicon-v1"

# A latin run of at least 2 letters. Runs, not tokens: legacy-font output welds
# symbols into words (`g]kfn`, `a}+s`), so tokenising on whitespace alone would
# find almost no candidates in exactly the text this guard must judge. Splitting
# to runs gives Preeti its best possible shot at LOOKING English, which is the
# conservative direction for a guard whose failure mode is converting English.
_LATIN_RUN = re.compile(r"[A-Za-z]{2,}")
_TOKEN = re.compile(r"\S+")
_VOWEL = re.compile(r"[aeiou]")

# Devanagari LETTERS only. The digits ०-९ (U+0966-096F) and the dandas । ॥ are
# inside the block and would otherwise be "words": `२०७६` is script evidence but
# never vocabulary evidence, and counting it would let a mapping score for putting
# digits in the right BLOCK while getting every digit wrong — which is exactly how
# FONTASY_HIMALI_TT fails on this corpus (`@)^%` → द्दण्टछ, not २०६५).
_DEVANAGARI_WORD = re.compile("[ऀ-ॣॱ-ॿ꣠-ꣿ]{2,}")

# Real English two-letter words. Without this the lexicon absorbs PDF extraction
# noise — `fi` from a ligature split appeared in enough clean documents to look
# like vocabulary, and it made the Preeti heading `g]kfn /fi6« a}+s` score 0.5 on
# the English guard. Two-letter runs are where a glyph-mapped string is most
# likely to collide with English by chance, so this is the one place the corpus
# is not allowed to speak for itself.
TWO_LETTER_ENGLISH = frozenset(
    """am an as at be by do go he if in is it me my no of on or so to up us we
    id re ex""".split()
)

# A word must appear in at least this many distinct source documents to enter the
# lexicon. Kills per-document artifacts — a mangled header repeated on 80 pages of
# ONE file is still one document, and would otherwise look like common vocabulary.
MIN_DOCUMENT_FREQUENCY = 3

# --- the English guard's thresholds ---------------------------------------- #
# Deliberately generous toward "this is English", because the two errors are not
# symmetric. Refusing to convert a legacy line loses one line and reports itself
# as unresolved. Converting an English line destroys content while every naive
# signal reports success — that is the exact failure the spike found.
ENGLISH_MIN_RATE = 0.60   # share of latin runs that must be known English words
ENGLISH_MIN_RUNS = 2      # below this a line carries no vocabulary evidence
# …except that a SINGLE long known word is evidence on its own. `Turnover` and
# `Outstanding` are whole lines in the benchmark's English table — one run each,
# so a flat two-run floor abstains and the conversion router (which treats a
# short line inside a legacy document as a candidate) converts them. Four
# characters is the floor because that is where a real English word stops being
# confusable with a fragment of glyph-mapped text (`kfn`, `fi6`).
SINGLE_RUN_MIN_LENGTH = 4


class LexiconError(RuntimeError):
    """The frozen lexicon is missing, unreadable, or not the one named."""


@dataclass(frozen=True)
class Lexicon:
    """A frozen, fingerprinted vocabulary pair."""

    version: str
    english: frozenset[str]
    nepali: frozenset[str]
    fingerprint: str
    provenance: dict[str, Any]

    def as_json(self) -> dict[str, Any]:
        """Deterministic on-disk form — both word lists SORTED, so two builds over
        the same corpus produce byte-identical files and a diff means the corpus
        changed."""
        return {
            "version": self.version,
            "fingerprint": self.fingerprint,
            "provenance": self.provenance,
            "english": sorted(self.english),
            "nepali": sorted(self.nepali),
        }


def lexicon_fingerprint(
    version: str, english: set[str] | frozenset[str], nepali: set[str] | frozenset[str]
) -> str:
    """Identity of a vocabulary, over its CONTENT.

    Bound to the algorithm version as well as the words, exactly like
    `manifest.selection_sha256` and `calibration`'s subset id: changing what
    counts as a word must not be able to reuse an existing fingerprint.
    """
    h = hashlib.sha256()
    h.update(version.encode())
    for name, words in (("english", english), ("nepali", nepali)):
        h.update(b"\x00" + name.encode() + b"\x00")
        for word in sorted(words):
            h.update(word.encode() + b"\n")
    return h.hexdigest()


def english_runs(line: str) -> list[str]:
    """Lowercased latin runs of >=2 letters, in order."""
    return [m.group(0).lower() for m in _LATIN_RUN.finditer(line)]


def english_harvest(text: str) -> list[str]:
    """Latin runs from the lines of a document that are NOT glyph-mapped.

    Harvesting a whole document indiscriminately poisons the guard, and the
    failure is not theoretical — it was measured. Widening the English source to
    every `clean` PDF below the classifier's own 0.20 let the Preeti lines inside
    otherwise-English documents contribute `egbf` (भन्दा), `tyf` (तथा), `jofh`
    (ब्याज) and `qmfgl` as "English words". The guard then blocked 32 genuine
    Preeti lines in one circular — the exact opposite of its purpose, and far
    quieter than the failure it was built to prevent.

    The filter is `quality.line_looks_glyph_mapped`, the same detector native-1
    used, so there is no second opinion about what a legacy line is. Only an
    affirmative `True` is dropped: `None` (too short to judge) is kept, because
    the alternative is discarding every heading and cell in the corpus.
    """
    from . import quality  # local: keeps this module importable on its own

    return [
        run
        for line in text.splitlines()
        if quality.line_looks_glyph_mapped(line) is not True
        for run in english_runs(line)
    ]


def english_acronyms(text: str) -> list[str]:
    """ALL-CAPS runs of 2-6 letters from non-glyph-mapped lines, lowercased.

    Collected separately because the vowel rule below would otherwise throw away
    every acronym — and this corpus is made of them. `NRB`, `SLF`, `BFI` have no
    vowel, and losing `nrb` is what let the table line `iv. NRB Bond - - -`
    through the English guard and into the converter.

    Uniform capitalisation is the discriminator: NRB's acronyms are all-caps,
    while glyph-mapped text is relentlessly mixed-case (`ljQLo`, `k|fKt`,
    `OGgf]e]l6e`) — that mid-token case switching is one of the three signals
    `quality` detects legacy fonts with in the first place.
    """
    from . import quality

    return [
        run.lower()
        for line in text.splitlines()
        if quality.line_looks_glyph_mapped(line) is not True
        for run in _LATIN_RUN.findall(line)
        if run.isupper() and 2 <= len(run) <= 6
    ]


def nepali_tokens(text: str) -> list[str]:
    """All-Devanagari words of >=2 characters, in order.

    Punctuation and digits are excluded by construction: `२०६५` is script
    evidence but not vocabulary evidence, and letting numerals into the hit rate
    would let a mapping score well for getting digits into the right BLOCK
    while getting every digit wrong.
    """
    return _DEVANAGARI_WORD.findall(text)


def english_word_rate(line: str, lexicon: Lexicon) -> tuple[float, int]:
    """`(share of latin runs that are known English, number of runs judged)`.

    The count travels with the ratio for the same reason `legacy_line_counts`
    does: 1.0 over one run is not a finding, and a bare ratio cannot say so.
    """
    runs = english_runs(line)
    if not runs:
        return 0.0, 0
    hits = sum(1 for r in runs if r in lexicon.english)
    return round(hits / len(runs), 4), len(runs)


def nepali_word_rate(text: str, lexicon: Lexicon) -> tuple[float, int]:
    """`(share of Devanagari words that are known Nepali, number judged)`."""
    words = nepali_tokens(text)
    if not words:
        return 0.0, 0
    hits = sum(1 for w in words if w in lexicon.nepali)
    return round(hits / len(words), 4), len(words)


def is_confidently_english(line: str, lexicon: Lexicon) -> bool:
    """THE PRE-CONVERSION GUARD. True ⇒ never hand this line to a converter.

    Runs on the RAW line, before any conversion, and that ordering is the whole
    point. Judging the OUTPUT cannot work: `Instruments Times Offer Amount`
    becomes 91% Devanagari with a collapsed legacy ratio, so every after-the-fact
    measure reports a successful recovery of a table that has just been
    destroyed. The input, by contrast, is unambiguous — four common English
    words.

    This does NOT alter `quality.classify` or the `legacy_line_ratio >= 0.20`
    threshold. It is a veto applied at conversion time to a line the classifier
    already flagged, so native-1's measurements stay exactly as committed.
    """
    rate, runs = english_word_rate(line, lexicon)
    if runs >= ENGLISH_MIN_RUNS:
        return rate >= ENGLISH_MIN_RATE
    if runs == 1:
        only = english_runs(line)[0]
        return len(only) >= SINGLE_RUN_MIN_LENGTH and only in lexicon.english
    return False


def build_lexicon(
    english_documents: list[str],
    nepali_documents: list[str],
    *,
    provenance: dict[str, Any],
    min_document_frequency: int = MIN_DOCUMENT_FREQUENCY,
) -> Lexicon:
    """Derive a vocabulary from whole documents. Pure — the caller does the I/O.

    Document frequency, never total frequency: a word repeated 4,000 times in one
    workbook is one document's habit, not the corpus's vocabulary.
    """
    def _collect(documents: list[str], extract) -> frozenset[str]:
        seen: dict[str, int] = {}
        for doc in documents:
            for word in set(extract(doc)):
                seen[word] = seen.get(word, 0) + 1
        return frozenset(
            w for w, n in seen.items() if n >= min_document_frequency
        )

    # Two admission rules on top of document frequency, both aimed at the same
    # leak: Preeti fragments from short lines the line filter could not judge.
    #
    #   * length — a 2-letter run must be a real English word (`TWO_LETTER_ENGLISH`).
    #   * vowels — an English word has one. `tyf` (तथा), `qmfgl` and `kfn` (पाल)
    #     do not, and all three survived the line filter and reached the lexicon.
    #
    # The vowel rule costs a handful of genuine words (`gym`, `myth`, `dry`); none
    # of them appears in NRB regulatory English, and every one of them is a worse
    # loss than letting a Preeti token disable the guard on a real Nepali line.
    # Acronyms are exempted (`english_acronyms`) because they are the one large
    # class of vowel-less REAL words this corpus is full of.
    acronyms = _collect(english_documents, english_acronyms)
    english = frozenset(
        w
        for w in _collect(english_documents, english_harvest)
        if (len(w) > 2 and _VOWEL.search(w))
        or w in TWO_LETTER_ENGLISH
        or w in acronyms
    )
    nepali = _collect(nepali_documents, nepali_tokens)
    return Lexicon(
        version=LEXICON_VERSION,
        english=english,
        nepali=nepali,
        fingerprint=lexicon_fingerprint(LEXICON_VERSION, english, nepali),
        provenance={
            **provenance,
            "min_document_frequency": min_document_frequency,
            "english_documents": len(english_documents),
            "nepali_documents": len(nepali_documents),
            "english_words": len(english),
            "nepali_words": len(nepali),
        },
    )


def load_lexicon(path: str | Path, *, expect_fingerprint: str | None = None) -> Lexicon:
    """Read a frozen lexicon, and verify it is the one that was named.

    The fingerprint is RECOMPUTED from the words rather than trusted from the
    file, so an edited word list cannot keep its old identity. Same guard as
    `manifest.verify_manifest`, for the same reason: an evidence artifact whose
    id no longer matches its content is worse than no artifact.
    """
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LexiconError(
            f"lexicon not found: {path} — build it with scripts/nrb_build_lexicon.py"
        ) from exc
    except json.JSONDecodeError as exc:
        raise LexiconError(f"lexicon is not valid JSON: {path}: {exc}") from exc

    english = frozenset(raw.get("english", ()))
    nepali = frozenset(raw.get("nepali", ()))
    version = raw.get("version", "")
    actual = lexicon_fingerprint(version, english, nepali)
    if actual != raw.get("fingerprint"):
        raise LexiconError(
            f"lexicon {path} has been edited: recorded fingerprint "
            f"{raw.get('fingerprint')!r} but its content hashes to {actual!r}"
        )
    if expect_fingerprint is not None and actual != expect_fingerprint:
        raise LexiconError(
            f"lexicon {path} is {actual!r}, not the expected {expect_fingerprint!r}"
        )
    return Lexicon(
        version=version,
        english=english,
        nepali=nepali,
        fingerprint=actual,
        provenance=raw.get("provenance", {}),
    )
