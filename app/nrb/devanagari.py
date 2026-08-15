"""Is this string PLAUSIBLE Devanagari? Pure — no I/O, no data files, no model.

This module exists because of one measurement, and it is the finding that shaped
the whole of Phase 6B (spike, 2026-08-15).

Run the benchmark's known English statistics table through a Preeti converter:

    "Instruments Times Offer Amount"
        -> "mक्ष्लकतचगभलतक mत्ष्भक इााभच mब्यगलत"

`devanagari_ratio` 0.0 → **0.9091**. `legacy_line_ratio` 0.2632 → **0.0**. Character
count preserved. Every obvious "did conversion work" signal says YES, and the
result is a destroyed English table. Worse, on a genuine Nepali line the WRONG
mapping can outscore the right one — `@)^%` becomes `२०६५` under Preeti
(devanagari 0.9796) and the nonsense `द्दण्टछ` under FONTASY_HIMALI_TT
(devanagari **0.9808**). Devanagari-ness is not correctness, and on the question
of WHICH mapping it is actively anti-correlated with it.

So conversion needs an instrument that measures Devanagari the way a reader does.
Two signals here, neither of which a wrong mapping can fake by producing more
Devanagari:

  * **Structural legality** — sequences the script does not permit at all, like a
    matra on an independent vowel (`इाा`, in the wreckage above). Pure Unicode
    rules, no vocabulary, valid for any Devanagari language.
  * **Latin residue** — a token holding both Devanagari and latin letters
    (`mक्ष्लकतचगभलतक`). Correct conversion of a Nepali line leaves no latin glued
    inside a word; a converter chewing on English leaves it everywhere.

A third signal, vocabulary, needs a corpus and therefore lives in `lexicon.py`.
Structure alone cannot tell `द्दण्टछ` from `२०६५` — both are legal Devanagari — and
pretending otherwise is how a wrong mapping would pass.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

__all__ = [
    "DEVANAGARI_DIGITS",
    "DevanagariShape",
    "illegal_cluster_count",
    "is_devanagari_char",
    "latin_residue_tokens",
    "measure_devanagari",
]

_TOKEN = re.compile(r"\S+")
_LATIN_LETTER = re.compile(r"[A-Za-z]")

# --- the script's own building blocks -------------------------------------- #
# Ranges rather than a character list: the Devanagari block is contiguous by
# category, and spelling it out invites a typo that silently disables a rule.

# Independent vowels अ आ इ ई उ ऊ ऋ ऌ ऍ ऎ ए ऐ ऑ ऒ ओ औ. These carry their vowel
# themselves, so they can never take a matra — that is rule 1 below.
_INDEPENDENT_VOWELS = frozenset(chr(c) for c in range(0x0904, 0x0915))

# Consonants क–ह, the nukta'd forms क़–य़, and the extended block.
_CONSONANTS = frozenset(
    [chr(c) for c in range(0x0915, 0x093A)]
    + [chr(c) for c in range(0x0958, 0x0960)]
    + [chr(c) for c in range(0x0978, 0x0980)]
)

# Dependent vowel signs (matras). A consonant takes AT MOST one, which is rule 2.
# `ऺ ऻ` (093A/093B) and the Vedic `ॎ ॏ ॕ ॖ ॗ` are included for completeness — they
# are matras and obey the same adjacency rules.
_VOWEL_SIGNS = frozenset(
    [chr(c) for c in (0x093A, 0x093B)]
    + [chr(c) for c in range(0x093E, 0x094D)]
    + [chr(c) for c in (0x094E, 0x094F, 0x0955, 0x0956, 0x0957, 0x0962, 0x0963)]
)

VIRAMA = "्"          # ् — kills the inherent vowel, joins a conjunct
NUKTA = "़"           # ़ — a modifier, and never the first thing in a word
_ANUSVARA_ETC = frozenset("ऀँंः")  # ऀ ँ ं ः

DEVANAGARI_DIGITS = frozenset(chr(c) for c in range(0x0966, 0x0970))
_DANDA = frozenset("।॥")  # । ॥

# Everything in the block, including the Extended range. Same definition as
# `quality._DEVANAGARI`, deliberately — one script test, two consumers.
_DEVANAGARI = re.compile(r"[ऀ-ॿ꣠-ꣿ]")

# A mark that MUST have something to attach to, so it cannot open a token.
_NEEDS_A_BASE = _VOWEL_SIGNS | {VIRAMA, NUKTA} | _ANUSVARA_ETC


def is_devanagari_char(ch: str) -> bool:
    return bool(_DEVANAGARI.match(ch))


def _ratio(part: int, whole: int) -> float:
    """Rounded to 4 places, so two runs produce byte-identical JSON — the same
    contract as `quality._ratio`."""
    return round(part / whole, 4) if whole else 0.0


def illegal_cluster_count(text: str) -> int:
    """Sequences Devanagari does not permit, counted.

    Conservative on purpose. Every rule below is a hard orthographic
    impossibility, not a rarity — an unusual-but-legal conjunct must not be
    counted, or the measure stops meaning "this is not Devanagari" and starts
    meaning "this is unusual Devanagari", which is a judgement about content.

    What this CANNOT do is tell a wrong mapping from a right one when both emit
    legal script: `द्दण्टछ` (FONTASY, nonsense) and `२०६५` (Preeti, correct) are
    both perfectly legal. That is `lexicon.py`'s job. Structure catches the
    catastrophe; vocabulary catches the mistake.
    """
    illegal = 0
    for token in _TOKEN.findall(text):
        prev = ""
        for i, ch in enumerate(token):
            # 7. a mark with nothing to attach to — a token cannot open with one.
            if i == 0 or not is_devanagari_char(prev):
                if ch in _NEEDS_A_BASE:
                    illegal += 1
                prev = ch
                continue

            if ch in _VOWEL_SIGNS:
                # 1. an independent vowel already carries its vowel.
                # 2. a consonant takes at most one matra.
                # 3. a virama has just removed the vowel slot.
                # 8. digits and dandas are not bases.
                if (
                    prev in _INDEPENDENT_VOWELS
                    or prev in _VOWEL_SIGNS
                    or prev == VIRAMA
                    or prev in DEVANAGARI_DIGITS
                    or prev in _DANDA
                ):
                    illegal += 1
            elif ch == VIRAMA:
                # 4/5/6. a virama kills a consonant's inherent vowel; there is
                # nothing for it to do after a vowel, a matra or another virama.
                if (
                    prev in _INDEPENDENT_VOWELS
                    or prev in _VOWEL_SIGNS
                    or prev == VIRAMA
                    or prev in DEVANAGARI_DIGITS
                    or prev in _DANDA
                ):
                    illegal += 1
            elif ch == NUKTA and prev not in _CONSONANTS:
                illegal += 1
            prev = ch
    return illegal


def latin_residue_tokens(text: str) -> int:
    """Tokens carrying BOTH Devanagari and latin letters.

    The signature of a converter run over text that was not legacy-encoded.
    `mक्ष्लकतचगभलतक` is what "Instruments" becomes; correct conversion of a Nepali
    line produces none of these, because a real English word in a mixed document
    is its own token and is never routed to the converter in the first place.

    Counted per token rather than per character: one stray latin letter welded
    into a Devanagari word is the whole finding, and weighting it by how long the
    word happens to be would bury it.
    """
    return sum(
        1
        for token in _TOKEN.findall(text)
        if _DEVANAGARI.search(token) and _LATIN_LETTER.search(token)
    )


@dataclass(frozen=True)
class DevanagariShape:
    """Structural measurements of one candidate Unicode Devanagari string."""

    devanagari_chars: int
    devanagari_ratio: float
    token_count: int
    devanagari_tokens: int

    illegal_clusters: int
    # Per DEVANAGARI CHARACTER, not per character: a mostly-latin line with two
    # Devanagari characters and one illegal cluster is catastrophically bad, and
    # dividing by the whole line would score it as almost clean.
    illegal_cluster_ratio: float

    latin_residue_tokens: int
    latin_residue_ratio: float

    def as_dict(self) -> dict[str, float | int]:
        return asdict(self)


def measure_devanagari(text: str) -> DevanagariShape:
    """Every structural signal for one string. Never raises; empty input is all
    zeros, which is an honest measurement of a converter that produced nothing."""
    tokens = _TOKEN.findall(text)
    non_ws = sum(1 for ch in text if not ch.isspace())
    dev_chars = len(_DEVANAGARI.findall(text))
    dev_tokens = sum(1 for t in tokens if _DEVANAGARI.search(t))
    illegal = illegal_cluster_count(text)
    residue = latin_residue_tokens(text)
    return DevanagariShape(
        devanagari_chars=dev_chars,
        devanagari_ratio=_ratio(dev_chars, non_ws),
        token_count=len(tokens),
        devanagari_tokens=dev_tokens,
        illegal_clusters=illegal,
        illegal_cluster_ratio=_ratio(illegal, dev_chars),
        latin_residue_tokens=residue,
        latin_residue_ratio=_ratio(residue, len(tokens)),
    )
