"""HarfBuzz shaping for the native-3 repair gate. The ONLY file that knows uharfbuzz exists.

A Word export draws glyph ids, not characters. The repair (`fontrepair`) turns
those glyph ids back into text through the font's own tables; this module runs
the other direction — text into glyph ids — so the gate can ask the only
question that proves the repair: does the text we rebuilt, shaped with the
page's own embedded font, reproduce exactly the glyphs the page draws?

HarfBuzz is the reference implementation of OpenType shaping and is designed to
match Uniscribe, the shaper Word uses. Where the two disagree on a run, the page
is not served repaired — the gate fails safe (spec §4.3, §11 risk 1).

uharfbuzz is a worker-only dependency (`requirements-worker.txt`). Imported
inside the functions, never at module scope, so `app.nrb.recovery` — which
imports `fontrepair`, which imports this — stays loadable in the API image;
`tests/test_nrb_repair_import_boundary.py` asserts that by subprocess.
"""

from __future__ import annotations

from functools import lru_cache

__all__ = ["LANGUAGE", "SCRIPT", "ShaperUnavailable", "available", "shape", "version"]

# Fixed, and part of the native engine identity (`fontrepair.engine_version`):
# a font's Nepali `locl` lookups change which glyph is chosen, so a different
# language would be a different shaper.
SCRIPT = "Deva"
LANGUAGE = "ne"


class ShaperUnavailable(RuntimeError):
    """uharfbuzz is not installed. The repair records the page unrepaired."""


def available() -> bool:
    try:
        import uharfbuzz  # noqa: F401
    except Exception:  # noqa: BLE001 - absent, or a broken wheel: same answer
        return False
    return True


def version() -> str:
    """The HarfBuzz version, or `unavailable`. Read live into the engine identity."""
    try:
        import uharfbuzz as hb
    except Exception:  # noqa: BLE001
        return "unavailable"
    return str(hb.version_string())


@lru_cache(maxsize=16)
def _font(program: bytes):
    """One HarfBuzz font per font program, per process. `bytes` caches its own
    hash, so the key costs one pass over the program the first time only."""
    import uharfbuzz as hb

    return hb.Font(hb.Face(hb.Blob(program)))


def shape(program: bytes, text: str) -> tuple[int, ...]:
    """`text` shaped with `program` → glyph ids, in visual order."""
    try:
        import uharfbuzz as hb
    except Exception as exc:  # noqa: BLE001
        raise ShaperUnavailable(
            f"uharfbuzz is not installed ({type(exc).__name__})"
        ) from exc
    buf = hb.Buffer()
    buf.add_str(text)
    buf.direction = "ltr"
    buf.script = SCRIPT
    buf.language = LANGUAGE
    hb.shape(_font(program), buf, {})
    return tuple(info.codepoint for info in buf.glyph_infos)
