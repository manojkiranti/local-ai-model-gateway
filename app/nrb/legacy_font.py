"""The legacy-font → Unicode converter boundary. Text in, text out.

NRB publishes most of its Nepali as Preeti-family fonts: Devanagari glyphs mapped
onto latin codepoints. `quality.py` detects that (`legacy_font_suspected`); this
module is the other half — turning `g]kfn /fi6« a}+s` back into नेपाल राष्ट्र बैंक.

**This file is the ONLY place in the repository that knows a third-party
converter exists.** Everything else depends on the `LegacyFontConverter` Protocol,
so replacing the backend is one new class, not a sweep. That indirection is not
speculative generality — see the licence note below, which makes a replacement a
foreseeable event rather than a hypothetical one.

*** npttf2utf IS GPL-3.0 ***

It is the only evaluated converter that is CORRECT on this corpus. The MIT
alternative (`preeti-unicode-converter` 0.1.1) mangles matra reordering —
`आर्थकि` for आर्थिक, `माैदि्रक` for मौद्रिक — measured 2026-08-15. So the choice was
between a correct copyleft library and a permissive wrong one.

Three things keep that decision reversible and contained:

  1. It is declared in `requirements-nrb.txt`, which `Dockerfile` does not
     install. The API image cannot acquire it by accident.
  2. Nothing from it is copied here. No mapping table, no rule set, no code —
     `map.json` is read from the installed package at runtime.
  3. The import is lazy and failure is explicit (`ConverterUnavailable`), so the
     rest of `app/nrb` imports fine on a machine that never installed it.

GPL-3 obligations attach to DISTRIBUTION, not to internal use, so this is an open
**licensing gate before shipping**, recorded in `docs/nrb-integration.md` §12 —
not a resolved question. A later independently-derived mapping table would slot
in behind the same Protocol.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = [
    "ConverterUnavailable",
    "LegacyFontConverter",
    "MAPPINGS",
    "Npttf2UtfConverter",
    "available_mappings",
    "backend_version",
    "converter_for",
    "converters",
]


class ConverterUnavailable(RuntimeError):
    """The backend is not installed, or does not offer the requested mapping.

    Raised rather than returning the input unchanged. A converter that silently
    no-ops looks exactly like a document that needed no conversion, and the whole
    point of this phase is telling those two apart.
    """


@runtime_checkable
class LegacyFontConverter(Protocol):
    """One legacy font mapping, as a pure text→text function.

    `name` identifies the ADAPTER (which library), `mapping` the FONT. Both are
    recorded on every attempt, because "Preeti via npttf2utf 0.3.7" and "Preeti
    via a table we wrote" are different claims about the same document.
    """

    name: str
    mapping: str
    version: str

    def convert(self, text: str) -> str:
        ...


# The mappings npttf2utf 0.3.7 ships. Named here so a version bump that drops or
# renames one fails loudly at `converter_for` instead of quietly narrowing the
# evaluation — the same reason `quality.REASONS` is a closed vocabulary.
#
# Measured on the NRB corpus 2026-08-15: Preeti / Kantipur / Sagarmatha agree
# almost everywhere, FONTASY_HIMALI_TT and PCS NEPALI disagree mainly on digits
# (`@)^%` → द्दण्टछ rather than २०६५) and are wrong on every document reviewed.
# All five are kept because proving the last two wrong IS a result.
MAPPINGS = (
    "Preeti",
    "Kantipur",
    "Sagarmatha",
    "FONTASY_HIMALI_TT",
    "PCS NEPALI",
)


@functools.lru_cache(maxsize=1)
def _font_mapper():
    """The backend, loaded once.

    Lazy, for the same reason `rag/parsing.py` imports Docling inside a function:
    a module-scope import would put a GPL-3 package on the import path of
    everything that touches `app.nrb`, including the API. `lru_cache` because
    `FontMapper.__init__` re-reads and re-parses a 34 KB JSON rule file every
    time, and the evaluation constructs converters per mapping per document.
    """
    try:
        import npttf2utf
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise ConverterUnavailable(
            "npttf2utf is not installed. It is declared in requirements-nrb.txt, "
            "deliberately NOT in requirements.txt — see the licence note in "
            "app/nrb/legacy_font.py."
        ) from exc

    from pathlib import Path

    rule_file = Path(npttf2utf.__file__).parent / "map.json"
    if not rule_file.is_file():
        raise ConverterUnavailable(f"npttf2utf rule file is missing: {rule_file}")
    return npttf2utf.FontMapper(str(rule_file))


def backend_version() -> str:
    """The installed backend version, for the evidence record.

    A conversion result is only reproducible against a named converter version,
    so this is recorded on every attempt rather than assumed.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover - py<3.8 only
        return "unknown"
    try:
        return version("npttf2utf")
    except PackageNotFoundError as exc:
        raise ConverterUnavailable("npttf2utf is not installed") from exc


def available_mappings() -> tuple[str, ...]:
    """The mappings the INSTALLED backend actually offers, in `MAPPINGS` order.

    Intersected rather than trusted: `MAPPINGS` is what 0.3.7 shipped, and this
    is what is on the machine. A mapping we expect and do not get is reported by
    `converter_for`, not skipped here.
    """
    offered = set(_font_mapper().supported_maps)
    return tuple(m for m in MAPPINGS if m in offered)


@dataclass(frozen=True)
class Npttf2UtfConverter:
    """`LegacyFontConverter` backed by npttf2utf.

    Frozen and stateless — the backend is a module-level cache, so two converters
    for the same mapping are interchangeable and conversion is a pure function of
    (text, mapping, backend version).
    """

    mapping: str
    version: str
    name: str = "npttf2utf"

    def convert(self, text: str) -> str:
        """Text → Unicode Devanagari, deterministically.

        Empty input returns empty rather than raising: an empty line is a
        legitimate thing to hand a converter, and the no-text negative control
        depends on this being a boring no-op rather than an error.
        """
        if not text:
            return ""
        try:
            return self._mapper().map_to_unicode(text, from_font=self.mapping)
        except Exception as exc:  # the backend raises bare Exceptions in places
            raise ConverterUnavailable(
                f"npttf2utf failed on mapping {self.mapping!r}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    @staticmethod
    def _mapper():
        return _font_mapper()


def converter_for(mapping: str) -> Npttf2UtfConverter:
    """One converter, or an explicit failure.

    Never falls back to another mapping. Mapping identity is the thing under
    measurement — quietly substituting Preeti for a missing Kantipur would make
    the evaluation's central question unanswerable.
    """
    offered = available_mappings()
    if mapping not in offered:
        raise ConverterUnavailable(
            f"mapping {mapping!r} is not available; the installed backend offers "
            f"{list(offered)}"
        )
    return Npttf2UtfConverter(mapping=mapping, version=backend_version())


def converters() -> tuple[Npttf2UtfConverter, ...]:
    """Every available mapping, each a converter in its own right.

    Deliberately a flat tuple with no preference order beyond `MAPPINGS`. The
    evaluation runs each mapping from the SAME original text and never chains one
    into another — one mapping's corruption must not become another's input —
    so there is nothing here that could express a fallback chain.
    """
    v = backend_version()
    return tuple(Npttf2UtfConverter(mapping=m, version=v) for m in available_mappings())
