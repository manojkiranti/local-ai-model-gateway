"""What each glyph of an embedded TrueType program SAYS, from the font's own tables.

Word writes a ToUnicode label per glyph, and for Kalimati those labels are
systematically wrong (docs/prod-incident-2026-09-20.md §9.10 B). The font
itself is not: its `cmap` says which character each base glyph draws, and its
GSUB says which glyphs every ligature, half form, reph and alternate was built
from. This module reads both and returns, per glyph id, a TOKEN — the text the
glyph draws, with two private-use markers (`reorder.PREBASE`, `reorder.REPH`)
standing in for the two signs drawn out of logical position.

THE RULES
    * Only a Unicode cmap speaks (platform 0, or Windows encoding 1/10). The
      Windows symbol cmap (3, 0) maps private 0xF0xx codes and says nothing.
    * A cmap glyph says its character; for several codepoints, the Devanagari
      one, then the lowest. The cmap glyph of ि is PREBASE.
    * A single substitution's output says what its input says — so an
      alternate of the reph is still the reph.
    * A ligature says its components' tokens, concatenated.
    * A ligature formed by an `rphf` lookup from र + ् is REPH. A lookup shared
      by `rphf` and any other feature is refused: which feature formed the
      glyph cannot be known, and guessing is how रि becomes र्.
    * A two-glyph ligature formed by `blwf`/`pstf`/`vatu` from [C, ्] is the
      logical ्C. Old-spec (`deva`) fonts like Kalimati build the below-base ra
      from the pair [र, ्]; read literally it is र्, and प्रकृति becomes
      पर्कृति (measured 2026-10-02).
    * Only lookups that default-on shaping features reach are followed.
      Optional alternates (`aalt`, `salt`, `ssNN`) are not applied by Word or by
      HarfBuzz, and following them would make glyphs ambiguous for nothing.
    * A glyph reached by two derivations whose tokens differ (after NFC) is
      AMBIGUOUS and resolves to None. Ambiguity propagates: a ligature built
      from an ambiguous glyph has several candidates too, and a refusal
      propagates the same way, even if another rule also yields the glyph.

The derivation (`derive`) is pure, over flattened `Rule`s, so every rule above
is testable without a font. fontTools is imported only inside
`build_glyph_table`: this is the ONLY file that knows fontTools exists.
"""

from __future__ import annotations

import hashlib
import io
import itertools
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .reorder import PREBASE, REPH

__all__ = [
    "DEFAULT_FEATURES",
    "FontIdentity",
    "GlyphTable",
    "Rule",
    "VATTU_FEATURES",
    "build_glyph_table",
    "derive",
    "seed_tokens",
]

DEFAULT_FEATURES = frozenset({
    "ccmp", "locl", "nukt", "akhn", "rphf", "rkrf", "pref", "blwf", "abvf",
    "half", "pstf", "vatu", "cjct", "init", "pres", "abvs", "blws", "psts",
    "haln", "calt", "clig", "liga", "rlig",
})
VATTU_FEATURES = frozenset({"blwf", "pstf", "vatu"})
MAX_CANDIDATES = 8
MAX_PASSES = 16

_VIRAMA = "्"
_I_SIGN = "ि"
_REPH_TEXT = "र्"


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _is_consonant(ch: str) -> bool:
    o = ord(ch)
    return 0x0915 <= o <= 0x0939 or 0x0958 <= o <= 0x095F or 0x0978 <= o <= 0x097F


def _is_devanagari(ch: str) -> bool:
    return 0x0900 <= ord(ch) <= 0x097F


@dataclass(frozen=True)
class Rule:
    """One substitution, flattened from GSUB: `inputs` (glyph order) → `output`."""

    tags: frozenset[str]
    lookup: int
    inputs: tuple[int, ...]
    output: int


@dataclass(frozen=True)
class FontIdentity:
    """Which font program this is. Phase 2's donor check compares these."""

    family: str
    glyph_count: int
    advance_sha256: str

    def label(self) -> str:
        return f"{self.family or '?'}/{self.glyph_count}"


@dataclass(frozen=True)
class GlyphTable:
    tokens: Mapping[int, str]
    ambiguous: frozenset[int]
    cmap_chars: Mapping[int, frozenset[str]]
    has_gsub: bool
    identity: FontIdentity

    def token(self, gid: int) -> str | None:
        """What the glyph draws, or None when the font's own tables cannot say."""
        if gid in self.ambiguous:
            return None
        return self.tokens.get(gid)


def seed_tokens(cmap_chars: Mapping[int, frozenset[str]]) -> dict[int, str]:
    seed: dict[int, str] = {}
    for gid, chars in cmap_chars.items():
        ch = sorted(chars, key=lambda c: (not _is_devanagari(c), ord(c)))[0]
        seed[gid] = PREBASE if ch == _I_SIGN else ch
    return seed


def _token_for(rule: Rule, text: str) -> str | None:
    """The token a rule's output draws, given its inputs' concatenated tokens.
    None means "refuse": the output is ambiguous by rule, not by evidence."""
    if "rphf" in rule.tags:
        if (rule.tags & DEFAULT_FEATURES) - {"rphf"}:
            return None
        return REPH if len(rule.inputs) == 2 and _nfc(text) == _REPH_TEXT else None
    if (
        rule.tags & VATTU_FEATURES
        and len(rule.inputs) == 2
        and len(text) == 2
        and text[1] == _VIRAMA
        and _is_consonant(text[0])
    ):
        return _VIRAMA + text[0]
    return text


def derive(
    seed: Mapping[int, str], rules: Sequence[Rule]
) -> tuple[dict[int, str], frozenset[int]]:
    """Every glyph's candidate tokens, to a fixed point. Pure.

    Candidates only ever grow, so this terminates; `MAX_CANDIDATES` caps a set
    that is ambiguous anyway, and `MAX_PASSES` caps chains of substitutions.
    """
    cands: dict[int, set[str]] = {g: {_nfc(t)} for g, t in seed.items()}
    refused: set[int] = set()
    active = [r for r in rules if r.tags & DEFAULT_FEATURES]
    for _ in range(MAX_PASSES):
        changed = False
        for rule in active:
            if refused.intersection(rule.inputs):
                if rule.output not in refused:
                    refused.add(rule.output)
                    changed = True
                continue
            if not all(g in cands for g in rule.inputs):
                continue
            pools = [sorted(cands[g]) for g in rule.inputs]
            for combo in itertools.islice(itertools.product(*pools), MAX_CANDIDATES + 1):
                token = _token_for(rule, "".join(combo))
                if token is None:
                    if rule.output not in refused:
                        refused.add(rule.output)
                        changed = True
                    continue
                bucket = cands.setdefault(rule.output, set())
                token = _nfc(token)
                if token not in bucket and len(bucket) <= MAX_CANDIDATES:
                    bucket.add(token)
                    changed = True
        if not changed:
            break
    tokens = {g: next(iter(s)) for g, s in cands.items() if len(s) == 1 and g not in refused}
    ambiguous = frozenset({g for g, s in cands.items() if len(s) > 1} | refused)
    return tokens, ambiguous


# --------------------------------------------------------------------------- #
# Reading a real font
# --------------------------------------------------------------------------- #
def _subtables(lookup: Any) -> Iterator[tuple[int, Any]]:
    for st in lookup.SubTable:
        typ = lookup.LookupType
        if typ == 7:
            typ, st = st.ExtensionLookupType, st.ExtSubTable
        yield typ, st


_RULE_SETS = (
    ("SubRuleSet", "SubRule"),
    ("SubClassSet", "SubClassRule"),
    ("ChainSubRuleSet", "ChainSubRule"),
    ("ChainSubClassSet", "ChainSubClassRule"),
)


def _referenced(st: Any) -> Iterable[int]:
    """Lookups a contextual (type 5/6) subtable applies, in any of its formats."""
    for rec in getattr(st, "SubstLookupRecord", None) or []:
        yield rec.LookupListIndex
    for set_name, rule_name in _RULE_SETS:
        for rule_set in getattr(st, set_name, None) or []:
            if rule_set is None:
                continue
            for rule in getattr(rule_set, rule_name, None) or []:
                for rec in getattr(rule, "SubstLookupRecord", None) or []:
                    yield rec.LookupListIndex


def _lookup_tags(table: Any) -> dict[int, frozenset[str]]:
    """Each lookup's feature tags, a contextually-applied lookup inheriting the
    tags of every lookup that applies it."""
    lookups = list(table.LookupList.Lookup) if table.LookupList else []
    tags: dict[int, set[str]] = defaultdict(set)
    if table.FeatureList:
        for rec in table.FeatureList.FeatureRecord:
            for li in rec.Feature.LookupListIndex:
                tags[li].add(rec.FeatureTag)
    refs: dict[int, set[int]] = defaultdict(set)
    for li, lookup in enumerate(lookups):
        for typ, st in _subtables(lookup):
            if typ in (5, 6):
                refs[li].update(_referenced(st))
    changed = True
    while changed:
        changed = False
        for li, targets in refs.items():
            for target in targets:
                before = len(tags[target])
                tags[target] |= tags[li]
                changed |= len(tags[target]) != before
    return {li: frozenset(tags[li]) for li in range(len(lookups))}


def _rules(table: Any, gid_of: Mapping[str, int]) -> list[Rule]:
    tags = _lookup_tags(table)
    lookups = list(table.LookupList.Lookup) if table.LookupList else []
    out: list[Rule] = []

    def add(t: frozenset[str], li: int, names: Sequence[str], result: str) -> None:
        ids = [gid_of.get(n) for n in names]
        target = gid_of.get(result)
        if target is not None and all(i is not None for i in ids):
            out.append(Rule(t, li, tuple(ids), target))  # type: ignore[arg-type]

    for li, lookup in enumerate(lookups):
        t = tags.get(li, frozenset())
        for typ, st in _subtables(lookup):
            if typ == 1:
                for src, dst in (getattr(st, "mapping", None) or {}).items():
                    add(t, li, (src,), dst)
            elif typ == 4:
                for first, ligs in (getattr(st, "ligatures", None) or {}).items():
                    for lig in ligs:
                        add(t, li, (first, *lig.Component), lig.LigGlyph)
            elif typ == 8:
                for src, dst in zip(st.Coverage.glyphs, st.Substitute):
                    add(t, li, (src,), dst)
    return out


def _is_unicode_subtable(sub: Any) -> bool:
    """A real Unicode cmap only. fontTools' `isUnicode()` also admits (3, 0), the
    Windows SYMBOL encoding, whose codepoints (0xF0xx) are a private mapping, not
    characters; reading them as text would invent tokens."""
    return sub.platformID == 0 or (sub.platformID == 3 and sub.platEncID in (1, 10))


def _cmap_chars(font: Any, gid_of: Mapping[str, int]) -> dict[int, frozenset[str]]:
    chars: dict[int, set[str]] = defaultdict(set)
    if "cmap" not in font:
        return {}
    for sub in font["cmap"].tables:
        if sub.format == 14 or not _is_unicode_subtable(sub):
            continue
        for cp, name in (getattr(sub, "cmap", None) or {}).items():
            gid = gid_of.get(name)
            if gid is not None:
                chars[gid].add(chr(cp))
    return {g: frozenset(s) for g, s in chars.items()}


def _identity(font: Any, order: Sequence[str]) -> FontIdentity:
    family = ""
    if "name" in font:
        family = (font["name"].getDebugName(1) or "").strip()
    advances = ""
    if "hmtx" in font:
        metrics = font["hmtx"].metrics
        advances = ",".join(str(metrics.get(n, (0, 0))[0]) for n in order)
    return FontIdentity(family, len(order), hashlib.sha256(advances.encode()).hexdigest())


def build_glyph_table(program: bytes) -> GlyphTable:
    """One embedded TrueType program → its glyph table. Raises on an unreadable
    program; `fontrepair` records that as `unsupported_font`."""
    from fontTools.ttLib import TTFont

    font = TTFont(io.BytesIO(program))
    order = font.getGlyphOrder()
    gid_of = {name: i for i, name in enumerate(order)}
    chars = _cmap_chars(font, gid_of)
    has_gsub = "GSUB" in font
    rules = _rules(font["GSUB"].table, gid_of) if has_gsub else []
    tokens, ambiguous = derive(seed_tokens(chars), rules)
    return GlyphTable(tokens, ambiguous, chars, has_gsub, _identity(font, order))
