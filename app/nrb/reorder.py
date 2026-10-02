"""Visual glyph order → logical Unicode order, for Devanagari. Pure.

A PDF draws glyphs in VISUAL order, and two Devanagari signs are drawn somewhere
other than where they are typed: the vowel sign ि (U+093F) is drawn BEFORE the
consonant cluster it follows, and the reph — र् at the start of a cluster — is
drawn above the cluster and after it in the glyph stream. `glyphtable` gives the
glyphs that carry those two signs a private-use MARKER instead of their text,
and this module moves each marker back to where the character is typed:

    PREBASE + क्ष        →  क्षि
    क + ो + REPH        →  र्को
    PREBASE + त + REPH  →  र्ति      (reph first, then ि after the whole cluster)

Reph is resolved before the pre-base sign because, logically, र् is part of the
cluster the sign follows.

A marker with nothing legal to attach to is an ORPHAN. It is counted and turned
into its own character in place — never attached to a guessed neighbour — and
any orphan fails the repair gate (`fontrepair`), so an orphan's text is never
served as repaired. This module does not know about fonts, PDFs or the gate.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

__all__ = ["MARKERS", "PREBASE", "REPH", "Reordered", "reorder"]

PREBASE = ""  # the pre-base vowel sign ि, still in visual position
REPH = ""     # the reph (र् drawn above), still in visual position
MARKERS = frozenset({PREBASE, REPH})

_I_SIGN = "ि"
_REPH_TEXT = "र्"
_NUKTA = "़"
_VIRAMA = "्"
_JOINERS = frozenset({"‌", "‍"})
# Dependent signs that follow a cluster and may sit between it and its reph:
# candrabindu/anusvara/visarga, the matras, and the Vedic/extended matras.
_MARKS = frozenset(
    chr(c)
    for c in (0x0900, 0x0901, 0x0902, 0x0903, 0x093A, 0x093B,
              *range(0x093E, 0x094D), 0x094E, 0x094F,
              0x0955, 0x0956, 0x0957, 0x0962, 0x0963)
)


@dataclass(frozen=True)
class Reordered:
    text: str
    orphans: int


def _is_consonant(ch: str) -> bool:
    o = ord(ch)
    return 0x0915 <= o <= 0x0939 or 0x0958 <= o <= 0x095F or 0x0978 <= o <= 0x097F


def _cluster_end(s: str, i: int) -> int:
    """Index just past the consonant cluster that starts at `s[i]` (a consonant)."""
    j = i + 1
    if j < len(s) and s[j] == _NUKTA:
        j += 1
    while j < len(s) and s[j] == _VIRAMA:
        k = j + 1
        if k < len(s) and s[k] in _JOINERS:
            k += 1
        if k < len(s) and _is_consonant(s[k]):
            j = k + 1
            if j < len(s) and s[j] == _NUKTA:
                j += 1
        else:
            return k
    return j


def _cluster_start(s: str, last: int) -> int:
    """Index of the first consonant of the cluster whose last consonant (or its
    nukta) is `s[last]`."""
    i = last
    if s[i] == _NUKTA and i > 0:
        i -= 1
    while i >= 2:
        k = i - 1
        if s[k] in _JOINERS:
            k -= 1
        if k >= 1 and s[k] == _VIRAMA:
            m = k - 1
            if s[m] == _NUKTA and m > 0:
                m -= 1
            if _is_consonant(s[m]):
                i = m
                continue
        break
    return i


def reorder(visual: str) -> Reordered:
    s = visual
    orphans = 0

    while REPH in s:
        p = s.index(REPH)
        q = p - 1
        while q >= 0 and s[q] in _MARKS:
            q -= 1
        ends_a_cluster = q >= 0 and (
            _is_consonant(s[q])
            or (s[q] == _NUKTA and q > 0 and _is_consonant(s[q - 1]))
        )
        if ends_a_cluster:
            start = _cluster_start(s, q)
            s = s[:start] + _REPH_TEXT + s[start:p] + s[p + 1:]
        else:
            orphans += 1
            s = s[:p] + _REPH_TEXT + s[p + 1:]

    while PREBASE in s:
        p = s.index(PREBASE)
        if p + 1 < len(s) and _is_consonant(s[p + 1]):
            end = _cluster_end(s, p + 1)
            s = s[:p] + s[p + 1:end] + _I_SIGN + s[end:]
        else:
            orphans += 1
            s = s[:p] + _I_SIGN + s[p + 1:]

    return Reordered(unicodedata.normalize("NFC", s), orphans)
