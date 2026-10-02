# native-3 Font Repair (Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair the NRB PDFs whose Word-exported Kalimati text layer is garbled, by rebuilding each page's Unicode from the glyph ids through the embedded font's own cmap+GSUB, accepting a page only when HarfBuzz re-shapes the result into exactly the glyphs the page draws. Then measure it on a new cohort, have a Nepali reader validate it, repair the production corpus, and ship it to the bank as a data-only update.

**Architecture:** The repair is the **native route's engine** (spec D1). Classification and page routing are untouched, so `base_version` stays `native-2|recovery-1|prov-1|gate=0.8|unjudged=0.8`, and only native units re-run. Four new modules each own one library:
- `glyphtable` (fontTools): gid → text, from the font's own tables;
- `reorder` (pure): visual → logical;
- `shaping` (uharfbuzz): the reference shaper;
- `fontrepair` (pypdf): one page plus the gate.

`recovery.native_unit` is the only entry point, reached from both cold recovery and cache refresh, behind `NRB_NATIVE_REPAIR` (default off).

**Tech Stack:** Python 3.10 (project `.venv`), pypdf 6.15, fontTools ≥4.60, uharfbuzz 0.56.x (HarfBuzz 14.5), PostgreSQL 16/17 locally, a `pgvector/pgvector:pg15` container for release work, openpyxl 3.1.5, `pdftoppm`.

**Spec:** `docs/superpowers/specs/2026-10-02-native3-font-repair-design.md`. Read it before any task: the plan argues from it, and every number below comes from it.

---

## Measured before this plan was written (throwaway, read-only, 2026-10-02)

A scratch-venv probe (`hbvenv`, not the project venv) round-tripped Word's runs through font tables → reorder → HarfBuzz. These facts shaped the tasks; they are development evidence, not proof.

- **The gate catches our own mistakes.** The first probe misread Kalimati's below-base ra: `प्रकृति` became `पर्कृति`, and HarfBuzz refused it. Kalimati is an old-spec `deva` font; its `blwf`/`vatu` lookups form the below-base ra from the glyph pair **[र, ्]**, which stands for the logical `्र`. Shaping the *correct* words reproduces Word's glyphs exactly: `प्रकृतिका`, `बिक्रेताले`, `भित्र`.
- **After that fix, 99.6–99.9% of runs round-trip.** Over five Word 2007–2013 documents: 2637/2648, 3314/3321, 2000/2008 and 1633/1635 runs. The residue, which Task 5 must classify:
  - an eyelash-र glyph (`glyph00226`, `र्‍`, likely a ZWJ form);
  - `भित्र्याउन` (`5acd61c8e9a8` p.27);
  - a final `द्` that Word drew as `glyph00217` where HarfBuzz chose `glyph00622` (`परिषद्`, `2b11bf653aa2` p.21). This one is probably a **genuine** Word-vs-HarfBuzz difference.
- **Some embedded fonts have no Unicode cmap at all.** `getBestCmap()` returned `None` on three of seven Word documents (`35445ee37706`, `8d2d3695eb2a`, `730b6d941031`) and crashed the probe. This is Review Focus 1.

## Clarifications of the spec made while planning (please review)

1. **A font is suspect per DOCUMENT, not per page** (refines spec §4.2). A font is suspect when any code in its ToUnicode carries a label that contradicts the cmap. A page drawing only ligature glyphs (which have no cmap entry, so cannot "contradict") would otherwise escape repair while its labels are still wrong.
2. **A page of a detected document that has no Devanagari is `not_applicable`, with no caveat** (refines spec §3.2/§5.1, "every page"). An English annex is not garbled, and §29.2's over-warning rule applies.
3. **The workbook shows the whole page image, not a crop** (spec §6.4). Locating an excerpt's box needs the page's own (garbled) text positions. Each row instead names the line range on the page.
4. **The GSUB closure follows only lookups that default-on shaping features reach.** `aalt`, `salt`, `ssNN` and other optional alternates are ignored. Word does not apply them, and including them would make many glyphs ambiguous.
5. **The unrepaired reason for an unreadable font program is `unsupported_font`**: an encoding other than Identity-H, a non-CIDFontType2 descendant, no `FontFile2`, or a program fontTools cannot parse. The spec's "unsupported encoding" names only one of those causes.
6. **The repair version stays `repair-1` until the first unit is written with it** (Task 14). Before then nothing is cached under it, so Task 5's fixes need no bump. After Task 14, any change to `glyphtable`, `reorder`, `fontrepair`'s gate or the detector constants bumps `REPAIR_VERSION`.

## Global Constraints

- G1. Python 3.10, the project's own `.venv`; never a sibling's venv. Python commands are `.venv/bin/python …`, tests are `.venv/bin/pytest …`.
- G2. `requirements-worker.txt` gains exactly `fonttools>=4.60,<5` and `uharfbuzz>=0.56.2,<0.57`. `requirements.txt` gains nothing. Licences: fontTools MIT; uharfbuzz Apache-2.0, bundling HarfBuzz "Old MIT".
- G3. **No migration.** `.venv/bin/alembic heads` stays `e1a4c6f9b2d7 (head)`.
- G4. `NRB_NATIVE_REPAIR` defaults to `false` until Task 18. With it off, the native engine string is exactly `passthrough/native-2`.
- G5. `base_version()` stays `native-2|recovery-1|prov-1|gate=0.8|unjudged=0.8`. `plan_document` and `route_page` are not edited, and `RECOVERY_ROUTING_VERSION` stays `recovery-1`.
- G6. Detector constants: `DETECT_DENSITY = 0.001` (1.0 per 1,000 Devanagari characters) and `DETECT_MIN_DEVANAGARI = 500`. Frozen in Task 10, before the cohort is drawn.
- G7. Gate, all four per page: coverage, no orphans, **100%** of suspect-font runs round-trip, layout agreement.
- G8. Markers: `PREBASE = ""` (the pre-base `ि` in visual position) and `REPH = ""` (the reph in visual position).
- G9. Databases (`app/nrb/dbguard.py`):
  - EVIDENCE scripts are `local_ai_gateway_p4` only;
  - OPERATIONAL scripts admit p4 and `local_ai_gateway_build`;
  - nothing touches `local_ai_gateway` or `gw_prod_snapshot`.
- G10. **Long jobs run as `systemd-run --user` units, never from a Claude shell.** That covers the fetch, extract and evidence passes, the worker, the detect run, the release build, dumps and the rehearsal.
- G11. **The full suite passes before every commit.** Run it as a unit; `$SCRATCH` is your session scratchpad directory:
  ```bash
  systemd-run --user --wait --collect --unit="native3-suite-$(date +%s)" --working-directory="$PWD" \
    -p StandardOutput=file:"$SCRATCH/suite.log" -p StandardError=file:"$SCRATCH/suite.log" \
    .venv/bin/pytest -q -p no:cacheprovider -rfE ; tail -3 "$SCRATCH/suite.log"
  ```
  Expected: `… passed, 115 skipped …` and **no `failed`**. The baseline on 2026-10-02 was **2790 passed, 115 skipped**. A skip count above 115 means a test is silently not running (CLAUDE.md, "compare the skip count"): find out why before committing.
- G12. Work on branch `feat/native3-font-repair`. Every commit message ends with:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  ```
- G13. Never print or log a database password. Scripts take the URL from `DATABASE_URL`, built from `.env` credentials, exactly as `scripts/nrb_prod_scope.py` does.

## Review Focus

These input classes are the ones most likely to bite. Each line names the test that pins it.

1. **An embedded font with no Unicode cmap** (3 of 7 measured Word documents). Expected: no exception, and the page is `unrepaired:no_suspect_font` with its native text kept. Tests: `test_a_font_with_no_unicode_cmap_says_nothing` (Task 3) and `test_a_font_with_no_unicode_cmap_is_never_suspect` (Task 4).
2. **Text drawn inside a Form XObject, or a `Tf` scoped by `q`/`Q`.** Expected: runs are still attributed to the right font. Tests: `test_text_inside_a_form_xobject_is_walked` and `test_save_restore_around_the_text_does_not_lose_the_font` (Task 4).
3. **A malformed PDF, or a font program fontTools cannot parse.** Expected: `unrepaired:read_failed` or `unrepaired:unsupported_font`, never an exception escaping recovery. Tests: `test_an_unreadable_file_is_read_failed` and `test_a_garbage_font_program_is_unsupported_font` (Task 4), and `test_a_repair_engine_that_raises_keeps_the_native_text` (Task 6).
4. **The flag on in a deployment without uharfbuzz.** Expected: the engine string is `native-3/repair-unavailable`, detected pages are caveated, nothing crashes, and installing it later invalidates exactly those units. Test: `test_flag_on_without_the_shaper_is_a_named_unavailable_engine` (Task 7).
5. **A non-PDF native document (`.docx`/`.xlsx`) with the flag on.** Expected: its text is untouched and the repair engine is never called. Test: `test_a_non_pdf_native_document_is_never_repaired` (Task 7).

## File structure

| file | responsibility | task |
|---|---|---|
| `app/nrb/shaping.py` | the only uharfbuzz importer; `shape(program, text)` | 1 |
| `app/nrb/reorder.py` | pure visual→logical; owns the markers | 2 |
| `app/nrb/glyphtable.py` | the only fontTools importer; gid → token, identity | 3 |
| `app/nrb/fontrepair.py` | detector, ToUnicode parse/rewrite, run walker, gate, engine | 4 |
| `tests/nrb_font_pdf.py` | test helper: Word-like PDFs from an OFL font | 4 |
| `tests/fixtures/fonts/Lohit-Devanagari.{ttf,LICENSE}` | OFL-1.1 test font | 1 |
| `scripts/nrb_native3_detect.py` | OPERATIONAL: detection + repair report over a department | 5 |
| `app/nrb/recovery.py` | `page_routes`, `native_unit`, rule 6 | 5, 6 |
| `app/nrb/recovery_cache.py` | the version split, refresh, report, stats | 7 |
| `app/nrb/rag.py`, `app/rag/worker.py`, `app/config.py`, `.env.example` | dependency, chunk metadata, log, flag | 7 |
| `app/rag/sources.py`, `app/tools/local/{search,read}_department_doc*.py` | one caveat predicate | 8 |
| `app/nrb/extraction.py` | `native-3` evidence metrics | 9 |
| `scripts/nrb_native3_cohort.py` | EVIDENCE: draw + freeze the cohort | 10 |
| `scripts/nrb_native3_evidence.py` | EVIDENCE: the cohort measurements | 12 |
| `scripts/nrb_native3_workbook.py`, `scripts/nrb_native3_review_import.py` | reader sheet in/out (no DB) | 13 |
| `app/rag/reingest.py`, `scripts/nrb_corpus_fingerprint.py` | re-ingest by id; before/after proof | 14 |
| `scripts/build_release_db.py` | the foreign-document guard | 15 |
| `scripts/build_nrb_update_sql.py`, `scripts/nrb_update_rehearsal.py` | the bank's data-only update and its rehearsal | 16 |
| `/home/manoj/nrb-release/UPDATE.md` | copy-paste steps for the bank | 17 |

---

# Milestone 1 — the engine

### Task 1: Worker dependencies, `shaping.py`, and the test font

**Files:**
- Modify: `requirements-worker.txt` (append at the end)
- Create: `app/nrb/shaping.py`
- Create: `tests/fixtures/fonts/Lohit-Devanagari.ttf`, `tests/fixtures/fonts/Lohit-Devanagari.LICENSE`
- Test: `tests/test_nrb_shaping.py`

**Interfaces:**
- Produces:
  - `shaping.SCRIPT == "Deva"`, `shaping.LANGUAGE == "ne"`;
  - `shaping.available() -> bool`, `shaping.version() -> str` (HarfBuzz version, or `"unavailable"`);
  - `shaping.shape(program: bytes, text: str) -> tuple[int, ...]` (gids in visual order);
  - `shaping.ShaperUnavailable(RuntimeError)`, and `shaping._font` (an `lru_cache`).

- [ ] **Step 1: Append the dependencies to `requirements-worker.txt`**

```text

# --- native-3: NRB text-layer repair through the embedded font ------------- #
#
# `app/nrb/glyphtable.py` is the only importer of fontTools and
# `app/nrb/shaping.py` the only importer of uharfbuzz; both import inside a
# function, and `Dockerfile` installs `requirements.txt` alone — so the API
# image never loads either. fontTools was already here through docling; it is
# named because the repair now depends on it directly.
# Licences: fontTools MIT; uharfbuzz Apache-2.0, bundling HarfBuzz under the
# "Old MIT" licence. Permissive — no distribution gate of the npttf2utf kind —
# but keep uharfbuzz's LICENSE/NOTICE in the image.
# docs/superpowers/specs/2026-10-02-native3-font-repair-design.md §3.4
fonttools>=4.60,<5
uharfbuzz>=0.56.2,<0.57
```

- [ ] **Step 2: Install them into the project venv**

Run: `.venv/bin/pip install "fonttools>=4.60,<5" "uharfbuzz>=0.56.2,<0.57"`
Expected: `Successfully installed uharfbuzz-0.56.2` (fontTools 4.63.0 is already satisfied).
Then run `.venv/bin/python -c "import uharfbuzz as hb; print(hb.__version__, hb.version_string())"`, which should print `0.56.2 14.5.0`.

- [ ] **Step 3: Add the OFL test font and its licence**

```bash
mkdir -p tests/fixtures/fonts
cp /usr/share/fonts/truetype/lohit-devanagari/Lohit-Devanagari.ttf tests/fixtures/fonts/
cp /usr/share/doc/fonts-lohit-deva/copyright tests/fixtures/fonts/Lohit-Devanagari.LICENSE
grep -m1 "License: OFL-1.1" tests/fixtures/fonts/Lohit-Devanagari.LICENSE
```
Expected: `License: OFL-1.1`. The font is 155,012 bytes and has 711 glyphs. Its GSUB has both `dev2` and `deva` scripts.

- [ ] **Step 4: Write the failing test** — `tests/test_nrb_shaping.py`

```python
"""HarfBuzz, the repair gate's reference shaper (spec §4.3).

A repaired page is accepted only when its text, shaped with the page's own
embedded font, reproduces the glyph ids the page draws. These tests pin the
shaper's contract; the gate itself is tests/test_nrb_fontrepair.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("uharfbuzz")

from app.nrb import shaping  # noqa: E402

LOHIT = Path(__file__).parent / "fixtures" / "fonts" / "Lohit-Devanagari.ttf"


def _program() -> bytes:
    return LOHIT.read_bytes()


def test_the_shaper_is_available_and_names_its_harfbuzz():
    assert shaping.available() is True
    assert shaping.version()[0].isdigit()


def test_a_bare_consonant_shapes_to_one_real_glyph():
    gids = shaping.shape(_program(), "क")
    assert len(gids) == 1 and gids[0] != 0


def test_a_reph_cluster_is_fewer_glyphs_than_characters():
    """र्क is three characters drawn as क plus a reph. A ToUnicode label per
    GLYPH therefore cannot spell it, which is why Word's labels go wrong."""
    assert len(shaping.shape(_program(), "र्क")) < 3


def test_script_and_language_are_fixed():
    """Part of the engine identity: Nepali `locl` changes glyph choice."""
    assert (shaping.SCRIPT, shaping.LANGUAGE) == ("Deva", "ne")


def test_a_missing_uharfbuzz_raises_its_own_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "uharfbuzz", None)
    shaping._font.cache_clear()
    assert shaping.available() is False
    assert shaping.version() == "unavailable"
    with pytest.raises(shaping.ShaperUnavailable):
        shaping.shape(_program(), "क")
```

- [ ] **Step 5: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_nrb_shaping.py -v`
Expected: FAIL / ERROR, `ModuleNotFoundError: No module named 'app.nrb.shaping'`.

- [ ] **Step 6: Implement `app/nrb/shaping.py`**

```python
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
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_nrb_shaping.py -v`
Expected: 5 passed.

- [ ] **Step 8: Run the full suite (G11), then commit**

```bash
git add requirements-worker.txt app/nrb/shaping.py tests/test_nrb_shaping.py tests/fixtures/fonts/
git commit -m "feat(nrb): HarfBuzz shaping for the native-3 gate, worker-only

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: `reorder.py`: visual glyph order → logical Unicode

**Files:**
- Create: `app/nrb/reorder.py`
- Test: `tests/test_nrb_reorder.py`

**Interfaces:**
- Produces: `reorder.PREBASE`, `reorder.REPH`, `reorder.MARKERS`, the frozen dataclass `reorder.Reordered(text: str, orphans: int)`, and `reorder.reorder(visual: str) -> Reordered` (output NFC).

- [ ] **Step 1: Write the failing test** — `tests/test_nrb_reorder.py`

```python
"""Visual order → logical order. Pure; every case is a string written out.

The probe that preceded this plan (2026-10-02) got the rakar wrong once and the
round-trip gate refused it — these cases are what the reorderer must get right
before the gate is even reached.
"""

from __future__ import annotations

import pytest

from app.nrb.reorder import PREBASE, REPH, reorder

P, R = PREBASE, REPH


@pytest.mark.parametrize("visual, logical", [
    (P + "त", "ति"),                       # the pre-base sign moves after its consonant
    (P + "स्त", "स्ति"),                    # ...after the WHOLE cluster
    (P + "क्ष", "क्षि"),                    # a half-form conjunct is one cluster
    ("प्र" + P + "क", "प्रकि"),             # rakar is not a reph and stays put
    (P + "प्र", "प्रि"),                    # ि before a rakar cluster
    ("क" + "ो" + R, "र्को"),               # reph after a matra moves to the cluster start
    ("क" + R + "ी", "र्की"),               # reph before a post-base matra
    (P + "त" + R, "र्ति"),                 # both: र् first, then the whole cluster, then ि
    ("न" + "े" + R, "र्ने"),               # a matra+reph ligature's token
    ("का" + P + "म", "कामि"),
    ("गदा", "गदा"),                        # no marker, unchanged
    ("क़" + P, "क़ि"),                      # a nukta consonant takes the sign after it
])
def test_reorder_moves_each_marker_to_where_it_is_typed(visual, logical):
    out = reorder(visual)
    assert (out.text, out.orphans) == (logical, 0)


@pytest.mark.parametrize("visual", [
    P,                 # nothing to attach to
    P + " ",           # a space is not a cluster
    "क" + P,           # pre-base at the end of a run
    R + "क",           # reph before any cluster
    " " + R,           # reph after a space
])
def test_an_unattachable_marker_is_an_orphan_never_a_guess(visual):
    out = reorder(visual)
    assert out.orphans == 1
    assert P not in out.text and R not in out.text


def test_two_syllables_with_markers_reorder_independently():
    # कीर्ति, visually: क ी ि(pre-base) त reph
    assert reorder("क" + "ी" + P + "त" + R).text == "कीर्ति"


def test_output_is_nfc():
    import unicodedata

    out = reorder("क़" + P)
    assert out.text == unicodedata.normalize("NFC", out.text)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_nrb_reorder.py -v`
Expected: ERROR, `ModuleNotFoundError: No module named 'app.nrb.reorder'`.

- [ ] **Step 3: Implement `app/nrb/reorder.py`**

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_nrb_reorder.py -v`
Expected: all passed (12 + 5 + 2 cases).

- [ ] **Step 5: Run the full suite (G11), then commit**

```bash
git add app/nrb/reorder.py tests/test_nrb_reorder.py
git commit -m "feat(nrb): visual-to-logical Devanagari reordering for native-3

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: `glyphtable.py`: what each glyph says, from the font's own tables

**Files:**
- Create: `app/nrb/glyphtable.py`
- Test: `tests/test_nrb_glyphtable.py`

**Interfaces:**
- Consumes: `reorder.PREBASE`, `reorder.REPH`, `reorder.reorder`, and `shaping.shape` (tests only).
- Produces:
  - `glyphtable.Rule(tags: frozenset[str], lookup: int, inputs: tuple[int, ...], output: int)`;
  - `glyphtable.seed_tokens(cmap_chars) -> dict[int, str]`;
  - `glyphtable.derive(seed, rules) -> tuple[dict[int, str], frozenset[int]]`;
  - `glyphtable.FontIdentity(family, glyph_count, advance_sha256)` with `.label() -> str`;
  - `glyphtable.GlyphTable(tokens, ambiguous, cmap_chars, has_gsub, identity)` with `.token(gid) -> str | None`;
  - `glyphtable.build_glyph_table(program: bytes) -> GlyphTable`;
  - `glyphtable.DEFAULT_FEATURES`, `glyphtable.VATTU_FEATURES`.

- [ ] **Step 1: Write the failing test** — `tests/test_nrb_glyphtable.py`

```python
"""gid → what the glyph draws, from the font's own cmap + GSUB (spec §3.3).

Two halves. The derivation rules are tested PURE, on hand-written `Rule`s, so
each rule is provable without a font. The real-font half proves those rules are
enough for a real Devanagari font: every glyph HarfBuzz draws for the sample
resolves, and tokens → reorder → shape reproduces the drawn glyphs.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

pytest.importorskip("fontTools")
pytest.importorskip("uharfbuzz")

from app.nrb import glyphtable as GT  # noqa: E402
from app.nrb import shaping  # noqa: E402
from app.nrb.reorder import PREBASE, REPH, reorder  # noqa: E402

FONTS = Path(__file__).parent / "fixtures" / "fonts"
LOHIT = FONTS / "Lohit-Devanagari.ttf"
SYSTEM_KALIMATI = Path("/usr/share/fonts/truetype/fonts-deva-extra/kalimati.ttf")

SAMPLE = (
    "नेपाल राष्ट्र बैंकको निर्देशन अनुसार कार्यालय",
    "प्रकृति विक्रेता गर्दा त्यस्तो वस्तु फिर्ता",
    "क्षेत्र संस्था स्थापना भित्र मिति सकिने",
)


def _r(tags, inputs, output, lookup=0):
    return GT.Rule(frozenset(tags), lookup, tuple(inputs), output)


# --------------------------------------------------------------------------- #
# The rules, pure
# --------------------------------------------------------------------------- #
def test_a_cmap_glyph_says_its_own_character():
    assert GT.derive({1: "क"}, []) == ({1: "क"}, frozenset())


def test_the_seed_marks_the_prebase_sign():
    assert GT.seed_tokens({5: frozenset({"ि"})}) == {5: PREBASE}


def test_the_seed_prefers_the_devanagari_codepoint():
    assert GT.seed_tokens({7: frozenset({"|", "।"})}) == {7: "।"}


def test_a_ligature_concatenates_its_components():
    tokens, _ = GT.derive({1: "क", 2: "्", 3: "ष"}, [_r({"akhn"}, (1, 2, 3), 10)])
    assert tokens[10] == "क्ष"


def test_an_rphf_ligature_is_the_reph_marker():
    tokens, _ = GT.derive({1: "र", 2: "्"}, [_r({"rphf"}, (1, 2), 10)])
    assert tokens[10] == REPH


def test_an_alternate_of_the_reph_is_still_the_reph():
    """The probe's first bug: a reph alternate lost its class."""
    rules = [_r({"rphf"}, (1, 2), 10), _r({"abvs"}, (10,), 11, lookup=1)]
    tokens, _ = GT.derive({1: "र", 2: "्"}, rules)
    assert tokens[11] == REPH


def test_an_old_spec_below_base_ra_reads_as_virama_ra():
    """Kalimati (an old-spec `deva` font) forms the below-base ra from the glyph
    pair [र, ्]; it stands for the logical ्र. Read as र्, प्रकृति became
    पर्कृति (measured 2026-10-02, the round trip refused it)."""
    rules = [_r({"blwf"}, (1, 2), 10), _r({"vatu"}, (3, 10), 11, lookup=1)]
    tokens, _ = GT.derive({1: "र", 2: "्", 3: "प"}, rules)
    assert tokens[10] == "्र"
    assert tokens[11] == "प्र"


def test_a_new_spec_below_base_ra_is_already_logical():
    tokens, _ = GT.derive({1: "्", 2: "र"}, [_r({"blwf"}, (1, 2), 10)])
    assert tokens[10] == "्र"


def test_a_matra_plus_reph_ligature_keeps_both():
    rules = [_r({"rphf"}, (1, 2), 10), _r({"abvs"}, (5, 10), 12, lookup=1)]
    tokens, _ = GT.derive({1: "र", 2: "्", 5: "े"}, rules)
    assert tokens[12] == "े" + REPH


def test_two_derivations_that_disagree_make_the_glyph_ambiguous():
    rules = [_r({"pres"}, (1,), 10), _r({"pres"}, (2,), 10, lookup=1)]
    tokens, ambiguous = GT.derive({1: "क", 2: "ख"}, rules)
    assert 10 in ambiguous and 10 not in tokens


def test_ambiguity_propagates_into_a_ligature_built_from_it():
    rules = [_r({"pres"}, (1,), 10), _r({"pres"}, (2,), 10, lookup=1),
             _r({"pres"}, (10, 3), 11, lookup=2)]
    tokens, ambiguous = GT.derive({1: "क", 2: "ख", 3: "्"}, rules)
    assert 11 in ambiguous and 11 not in tokens


def test_a_lookup_shared_by_rphf_and_another_feature_is_refused():
    tokens, ambiguous = GT.derive({1: "र", 2: "्"}, [_r({"rphf", "half"}, (1, 2), 10)])
    assert 10 in ambiguous and 10 not in tokens


def test_a_non_default_feature_is_ignored():
    """`aalt` is not applied by Word or HarfBuzz; following it would make the
    target ambiguous for no reason."""
    tokens, ambiguous = GT.derive({1: "क"}, [_r({"aalt"}, (1,), 10)])
    assert 10 not in tokens and 10 not in ambiguous


def test_canonically_equal_derivations_are_one_token():
    """क़ precomposed (U+0958) and क + nukta are the same text under NFC."""
    tokens, ambiguous = GT.derive({1: "क़", 2: "क", 3: "़"},
                                  [_r({"nukt"}, (2, 3), 1)])
    assert 1 in tokens and 1 not in ambiguous


# --------------------------------------------------------------------------- #
# A real font
# --------------------------------------------------------------------------- #
def _table(path: Path = LOHIT) -> GT.GlyphTable:
    return GT.build_glyph_table(path.read_bytes())


def _fonts():
    yield LOHIT
    if SYSTEM_KALIMATI.exists():
        yield SYSTEM_KALIMATI


@pytest.mark.parametrize("font", list(_fonts()), ids=lambda p: p.stem)
def test_every_glyph_harfbuzz_draws_for_the_sample_resolves(font):
    table, program = _table(font), font.read_bytes()
    for text in SAMPLE:
        for gid in shaping.shape(program, text):
            assert table.token(gid) is not None, (text, gid)


@pytest.mark.parametrize("font", list(_fonts()), ids=lambda p: p.stem)
def test_the_sample_round_trips_through_the_table(font):
    """The gate in miniature: glyphs → tokens → reorder → shape == glyphs."""
    table, program = _table(font), font.read_bytes()
    for text in SAMPLE:
        gids = shaping.shape(program, text)
        out = reorder("".join(table.token(g) for g in gids))
        assert out.orphans == 0, text
        assert out.text == text
        assert shaping.shape(program, out.text) == gids


def test_identity_records_the_glyph_count_and_the_advance_widths():
    identity = _table().identity
    assert identity.glyph_count == 711
    assert len(identity.advance_sha256) == 64
    assert identity.label().endswith("/711")


def _without_gsub(program: bytes) -> bytes:
    from fontTools.ttLib import TTFont

    font = TTFont(io.BytesIO(program))
    del font["GSUB"]
    out = io.BytesIO()
    font.save(out)
    return out.getvalue()


def _symbol_cmap_only(program: bytes) -> bytes:
    from fontTools.ttLib import TTFont
    from fontTools.ttLib.tables._c_m_a_p import CmapSubtable

    font = TTFont(io.BytesIO(program))
    sub = CmapSubtable.newSubtable(4)
    sub.platformID, sub.platEncID, sub.language = 3, 0, 0
    sub.cmap = {0xF041: font.getGlyphOrder()[1]}
    font["cmap"].tables = [sub]
    out = io.BytesIO()
    font.save(out)
    return out.getvalue()


def test_a_font_without_gsub_resolves_only_its_cmap_glyphs():
    """The Word-365 shape (spec §7): cmap kept, GSUB stripped."""
    full = LOHIT.read_bytes()
    stripped = GT.build_glyph_table(_without_gsub(full))
    assert stripped.has_gsub is False
    drawn = shaping.shape(full, "र्क")
    assert any(stripped.token(g) is None for g in drawn)


def test_a_font_with_no_unicode_cmap_says_nothing():
    """Review Focus 1: measured on 3 of 7 Word documents. No crash, no tokens."""
    table = GT.build_glyph_table(_symbol_cmap_only(LOHIT.read_bytes()))
    assert table.cmap_chars == {}
    assert table.tokens == {}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_nrb_glyphtable.py -v`
Expected: ERROR, `ModuleNotFoundError: No module named 'app.nrb.glyphtable'`.

- [ ] **Step 3: Implement `app/nrb/glyphtable.py`**

```python
"""What each glyph of an embedded TrueType program SAYS, from the font's own tables.

Word writes a ToUnicode label per glyph, and for Kalimati those labels are
systematically wrong (docs/prod-incident-2026-09-20.md §9.10 B). The font
itself is not: its `cmap` says which character each base glyph draws, and its
GSUB says which glyphs every ligature, half form, reph and alternate was built
from. This module reads both and returns, per glyph id, a TOKEN — the text the
glyph draws, with two private-use markers (`reorder.PREBASE`, `reorder.REPH`)
standing in for the two signs drawn out of logical position.

THE RULES
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
      from an ambiguous glyph has several candidates too.

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


def _cmap_chars(font: Any, gid_of: Mapping[str, int]) -> dict[int, frozenset[str]]:
    chars: dict[int, set[str]] = defaultdict(set)
    if "cmap" not in font:
        return {}
    for sub in font["cmap"].tables:
        if sub.format == 14 or not sub.isUnicode():
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_nrb_glyphtable.py -v`
Expected: all passed. The Kalimati parametrizations run only if `/usr/share/fonts/truetype/fonts-deva-extra/kalimati.ttf` exists; it does on this laptop.

If a SAMPLE word fails the real-font round trip, **do not weaken the test.** Print that word's `shaping.shape` gids, their glyph names (`TTFont(...).getGlyphOrder()[g]`) and `table.token(g)`. Then find which rule produced the wrong token, add a pure `derive` test for that rule, and fix the rule. The probe that preceded this plan hit exactly one such case (the vattu ordering, already covered above).

- [ ] **Step 5: Run the full suite (G11), then commit**

```bash
git add app/nrb/glyphtable.py tests/test_nrb_glyphtable.py
git commit -m "feat(nrb): glyph tables from an embedded font's own cmap and GSUB

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: `fontrepair.py`: one page, the detector, the gate

**Files:**
- Create: `app/nrb/fontrepair.py`
- Create: `tests/nrb_font_pdf.py` (a test helper, not a test module)
- Test: `tests/test_nrb_fontrepair.py`

**Interfaces:**
- Consumes: `glyphtable.build_glyph_table`, `GlyphTable.token`, `GlyphTable.cmap_chars`, `GlyphTable.identity.label()`; `reorder.reorder`; `shaping.shape`, `shaping.available`, `shaping.version`, `shaping.LANGUAGE`; `devanagari.measure_devanagari`, `devanagari.illegal_cluster_count`.
- Produces:
  - constants: `fontrepair.DETECT_DENSITY`, `DETECT_MIN_DEVANAGARI`, `REPAIR_VERSION = "repair-1"`, `ENGINE_PREFIX = "native-3"`, `UNAVAILABLE`, `STATUS_REPAIRED = "font_tables"`, `NOT_APPLICABLE = "not_applicable"`, `UNREPAIRED_PREFIX = "unrepaired:"`;
  - reasons: `COVERAGE`, `ORPHANS`, `ROUNDTRIP`, `LAYOUT`, `NO_SUSPECT_FONT`, `UNSUPPORTED_FONT`, `SHAPER_UNAVAILABLE`, `READ_FAILED`, `ENGINE_ERROR`, and `unrepaired(reason) -> str`;
  - detection: `Detection(suspect, density, devanagari_chars, illegal_clusters)` with `.as_dict()`, and `detect(texts) -> Detection`;
  - `RepairOutcome(status: str, text: str, detail: dict)`;
  - `engine_available() -> bool`, `engine_version() -> str`;
  - `parse_tounicode(data: bytes) -> dict[int, str]`, `corrected_cmap(labels) -> bytes`, `missing_in_order(needles, haystack) -> int`;
  - `FontRepairEngine` with `.available`, `.version` and `.repair_page(path: Path, page_number: int, native_text: str) -> RepairOutcome`;
  - `fonts_report(path) -> dict`;
  - `document_evidence(engine, path, pages, native_numbers) -> dict`.
- Detail keys on every attempted page: `runs`, `runs_matched`, `glyph_uses`, `unresolved_uses`, `orphans`, `layout_missing`, `fonts`, `font_identities`, `illegal_before`, `illegal_after`, `examples` (≤3).

- [ ] **Step 1: Write the test helper** — `tests/nrb_font_pdf.py`

```python
"""PDFs that draw Devanagari the way a Microsoft Word export does — native-3 test helper.

Word shapes each line with the font, writes the GLYPH IDS into the content
stream (Type0 font, Identity-H, CIDToGIDMap Identity), and writes ONE ToUnicode
label per glyph. For a Devanagari cluster it labels each glyph with the
character at the same POSITION in the cluster, and the first cluster to use a
glyph decides that glyph's label for the whole file — which is how gid 143 (त)
came to be labelled ि in every Word-2013 Kalimati file (prod-incident §9.10 B).
`word_bug_labels` reproduces that rule, so these PDFs garble the way the NRB
files do, and the ground truth is the text that was shaped.

The font is Lohit Devanagari (OFL-1.1, tests/fixtures/fonts). The system
Kalimati, when installed, exercises the old-spec `deva` path; it is never
committed.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Sequence

FIXTURES = Path(__file__).parent / "fixtures" / "fonts"
LOHIT = FIXTURES / "Lohit-Devanagari.ttf"
SYSTEM_KALIMATI = Path("/usr/share/fonts/truetype/fonts-deva-extra/kalimati.ttf")

LINES = (
    "नेपाल राष्ट्र बैंकको निर्देशन अनुसार कार्यालय",
    "प्रकृति विक्रेता गर्दा त्यस्तो वस्तु फिर्ता",
    "क्षेत्र संस्था स्थापना भित्र मिति सकिने",
)
# 6 x 3 lines is ~660 Devanagari characters: a safe margin over the detector's
# N = 500 floor even after Word's labels drop or merge characters.
REPEAT = 6


def clusters(program: bytes, text: str) -> list[tuple[list[int], str]]:
    """[(glyph ids in visual order, the logical text they draw)] per HarfBuzz cluster."""
    import uharfbuzz as hb

    font = hb.Font(hb.Face(hb.Blob(program)))
    buf = hb.Buffer()
    buf.add_codepoints([ord(c) for c in text])  # clusters index codepoints
    buf.direction = "ltr"
    buf.script = "Deva"
    buf.language = "ne"
    hb.shape(font, buf, {})
    infos = buf.glyph_infos
    starts = sorted({i.cluster for i in infos})
    end = {s: (starts[k + 1] if k + 1 < len(starts) else len(text)) for k, s in enumerate(starts)}
    out: list[tuple[list[int], str]] = []
    last = None
    for info in infos:
        if info.cluster == last:
            out[-1][0].append(info.codepoint)
        else:
            out.append(([info.codepoint], text[info.cluster:end[info.cluster]]))
            last = info.cluster
    return out


def shaped_lines(program: bytes, lines: Sequence[str]) -> list[list[int]]:
    return [[g for gids, _ in clusters(program, line) for g in gids] for line in lines]


def word_bug_labels(program: bytes, lines: Sequence[str]) -> dict[int, str]:
    labels: dict[int, str] = {}
    for line in lines:
        for gids, text in clusters(program, line):
            for i, gid in enumerate(gids):
                if i < len(gids) - 1:
                    piece = text[i] if i < len(text) else ""
                else:
                    piece = text[i:] if i < len(text) else ""
                if piece and gid not in labels:
                    labels[gid] = piece
    return labels


def honest_labels(program: bytes, lines: Sequence[str]) -> dict[int, str]:
    """Each drawn glyph labelled with what the font's own tables say it draws:
    a ToUnicode that agrees with the cmap everywhere, so it is never suspect."""
    from app.nrb import glyphtable
    from app.nrb.reorder import PREBASE, REPH

    table = glyphtable.build_glyph_table(program)
    out: dict[int, str] = {}
    for gids in shaped_lines(program, lines):
        for g in gids:
            token = table.token(g)
            if token is not None:
                out[g] = token.replace(PREBASE, "ि").replace(REPH, "र्")
    return out


def prebase_gid(program: bytes) -> int:
    from fontTools.ttLib import TTFont

    font = TTFont(io.BytesIO(program))
    return font.getGlyphOrder().index(font.getBestCmap()[0x093F])


def _tounicode(labels: dict[int, str]) -> bytes:
    rows = sorted(labels.items())
    out = [b"/CIDInit /ProcSet findresource begin", b"12 dict begin", b"begincmap",
           b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
           b"/CMapName /Adobe-Identity-UCS def", b"/CMapType 2 def",
           b"1 begincodespacerange", b"<0000> <FFFF>", b"endcodespacerange"]
    for i in range(0, len(rows), 100):
        chunk = rows[i:i + 100]
        out.append(f"{len(chunk)} beginbfchar".encode())
        out += [f"<{c:04X}> <{t.encode('utf-16-be').hex().upper()}>".encode() for c, t in chunk]
        out.append(b"endbfchar")
    out += [b"endcmap", b"CMapName currentdict /CMap defineresource pop", b"end", b"end"]
    return b"\n".join(out)


def build_pdf(
    program: bytes,
    pages: Sequence[Sequence[Sequence[int]]],
    labels: dict[int, str],
    *,
    base_font: str = "ABCDEE+TestDeva",
    in_form: bool = False,
    save_restore: bool = False,
) -> bytes:
    objs: list[bytes] = []

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    def stream(entries: str, data: bytes) -> bytes:
        return f"<< {entries} /Length {len(data)} >>\nstream\n".encode() + data + b"\nendstream"

    catalog = add(b"")
    tree = add(b"")
    program_obj = add(stream(f"/Length1 {len(program)}", program))
    descriptor = add(
        f"<< /Type /FontDescriptor /FontName /{base_font} /Flags 4 "
        f"/FontBBox [-500 -500 1500 1200] /ItalicAngle 0 /Ascent 1000 /Descent -400 "
        f"/CapHeight 700 /StemV 80 /FontFile2 {program_obj} 0 R >>".encode()
    )
    cidfont = add(
        f"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /{base_font} "
        f"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
        f"/FontDescriptor {descriptor} 0 R /CIDToGIDMap /Identity /DW 600 >>".encode()
    )
    cmap = add(stream("", _tounicode(labels)))
    font = add(
        f"<< /Type /Font /Subtype /Type0 /BaseFont /{base_font} /Encoding /Identity-H "
        f"/DescendantFonts [{cidfont} 0 R] /ToUnicode {cmap} 0 R >>".encode()
    )
    kids = []
    for lines in pages:
        def hexed(line):
            return "".join(f"{g:04X}" for g in line)

        if save_restore:
            # The font is set ONCE, in its own text object, and every line is
            # drawn inside q...Q without a Tf of its own: a walker that resets
            # the font at BT, or loses it across q/Q, drops every run.
            shown = "BT /F1 12 Tf ET q " + " ".join(
                f"BT 72 {740 - 18 * i} Td <{hexed(line)}> Tj ET" for i, line in enumerate(lines)
            ) + " Q"
        else:
            # One text object per line, as Word writes them: a run never spans
            # two lines, so a syllable is never split and lines never merge.
            shown = " ".join(
                f"BT /F1 12 Tf 72 {740 - 18 * i} Td <{hexed(line)}> Tj ET"
                for i, line in enumerate(lines)
            )
        body = shown
        if in_form:
            form = add(stream(
                f"/Type /XObject /Subtype /Form /BBox [0 0 612 792] "
                f"/Resources << /Font << /F1 {font} 0 R >> >>", body.encode()))
            contents = add(stream("", b"q /Fm1 Do Q"))
            resources = f"/XObject << /Fm1 {form} 0 R >>"
        else:
            contents = add(stream("", body.encode()))
            resources = f"/Font << /F1 {font} 0 R >>"
        kids.append(add(
            f"<< /Type /Page /Parent {tree} 0 R /MediaBox [0 0 612 792] "
            f"/Resources << {resources} >> /Contents {contents} 0 R >>".encode()))
    objs[catalog - 1] = f"<< /Type /Catalog /Pages {tree} 0 R >>".encode()
    objs[tree - 1] = (
        f"<< /Type /Pages /Count {len(kids)} "
        f"/Kids [{' '.join(f'{k} 0 R' for k in kids)}] >>".encode()
    )
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for n, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root {catalog} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def word_pdf(
    tmp_path: Path,
    shaping_program: bytes,
    *,
    embed: bytes | None = None,
    lines: Sequence[str] = LINES,
    repeat: int = REPEAT,
    pages: int = 1,
    labels: dict[int, str] | None = None,
    extra_line: Sequence[int] | None = None,
    name: str = "word.pdf",
    **kw,
) -> Path:
    """Shape with `shaping_program`, embed `embed` (default: the same program).
    Embedding a GSUB-stripped copy of the shaping font is the Word-365 case."""
    body = list(lines) * repeat
    gids = shaped_lines(shaping_program, body)
    if extra_line is not None:
        gids = gids + [list(extra_line)]
    chosen = labels if labels is not None else word_bug_labels(shaping_program, body)
    path = tmp_path / name
    path.write_bytes(build_pdf(embed or shaping_program, [gids] * pages, chosen, **kw))
    return path


def strip_gsub(program: bytes) -> bytes:
    from fontTools.ttLib import TTFont

    font = TTFont(io.BytesIO(program))
    del font["GSUB"]
    out = io.BytesIO()
    font.save(out)
    return out.getvalue()


def symbol_cmap_only(program: bytes) -> bytes:
    from fontTools.ttLib import TTFont
    from fontTools.ttLib.tables._c_m_a_p import CmapSubtable

    font = TTFont(io.BytesIO(program))
    sub = CmapSubtable.newSubtable(4)
    sub.platformID, sub.platEncID, sub.language = 3, 0, 0
    sub.cmap = {0xF041: font.getGlyphOrder()[1]}
    font["cmap"].tables = [sub]
    out = io.BytesIO()
    font.save(out)
    return out.getvalue()
```

- [ ] **Step 2: Write the failing test** — `tests/test_nrb_fontrepair.py`

```python
"""native-3's repair of one page, and its gate (spec §4).

Every PDF here is built by tests/nrb_font_pdf.py the way Word builds one: glyph
ids shaped by HarfBuzz, labels assigned by Word's positional rule. The ground
truth is the text that was shaped, so "repaired" is an exact comparison.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("uharfbuzz")
pytest.importorskip("fontTools")

from app.files import documents as file_documents  # noqa: E402
from app.nrb import fontrepair  # noqa: E402
from tests import nrb_font_pdf as W  # noqa: E402

LOHIT = W.LOHIT.read_bytes()


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _native(path: Path, page: int = 1) -> str:
    return file_documents.read_pdf_pages(path).pages[page - 1]


def _repair(path: Path, page: int = 1):
    return fontrepair.FontRepairEngine().repair_page(path, page, _native(path, page))


# --------------------------------------------------------------------------- #
# The detector (density, spec §4.1)
# --------------------------------------------------------------------------- #
def test_the_detector_constants_are_the_preregistered_values():
    assert fontrepair.DETECT_DENSITY == 0.001
    assert fontrepair.DETECT_MIN_DEVANAGARI == 500


def test_the_detector_fires_on_garbled_text_and_not_on_clean():
    assert fontrepair.detect(["कार्ाालर् " * 100]).suspect
    assert not fontrepair.detect(["कार्यालय " * 100]).suspect


def test_too_little_devanagari_never_fires():
    assert not fontrepair.detect(["कार्ाालर्"]).suspect


# --------------------------------------------------------------------------- #
# ToUnicode
# --------------------------------------------------------------------------- #
def test_tounicode_parses_bfchar_and_both_bfrange_forms():
    data = (b"2 beginbfchar\n<0001> <0915>\n<0002> <0930094D>\nendbfchar\n"
            b"1 beginbfrange\n<0010> <0012> <0905>\nendbfrange\n"
            b"1 beginbfrange\n<0020> <0021> [<0924> <093F>]\nendbfrange\n")
    labels = fontrepair.parse_tounicode(data)
    assert labels[1] == "क" and labels[2] == "र्"
    assert labels[0x11] == "आ"
    assert labels[0x21] == "ि"


def test_the_corrected_cmap_round_trips_through_the_parser():
    labels = {1: "क", 2: "", 300: "क्ष"}
    assert fontrepair.parse_tounicode(fontrepair.corrected_cmap(labels)) == labels


# --------------------------------------------------------------------------- #
# A Word page
# --------------------------------------------------------------------------- #
def test_a_word_page_is_garbled_natively(tmp_path):
    """The fixture's premise: Word's positional labels really do garble it."""
    native = _native(W.word_pdf(tmp_path, LOHIT))
    assert fontrepair.detect([native]).suspect, native[:300]


def test_a_word_page_is_repaired_to_exactly_the_shaped_text(tmp_path):
    out = _repair(W.word_pdf(tmp_path, LOHIT))
    assert out.status == fontrepair.STATUS_REPAIRED, out.detail
    assert _lines(out.text) == list(W.LINES) * W.REPEAT
    assert out.detail["runs"] == out.detail["runs_matched"] == len(W.LINES) * W.REPEAT
    assert out.detail["illegal_after"] == 0 < out.detail["illegal_before"]
    assert out.detail["font_identities"] == ["Lohit Devanagari/711"]


@pytest.mark.skipif(not W.SYSTEM_KALIMATI.exists(), reason="fonts-deva-extra Kalimati not installed")
def test_an_old_spec_kalimati_page_is_repaired_too(tmp_path):
    kalimati = W.SYSTEM_KALIMATI.read_bytes()
    out = _repair(W.word_pdf(tmp_path, kalimati))
    assert out.status == fontrepair.STATUS_REPAIRED, out.detail
    assert _lines(out.text) == list(W.LINES) * W.REPEAT


def test_a_correct_tounicode_is_left_alone(tmp_path):
    body = list(W.LINES) * W.REPEAT
    path = W.word_pdf(tmp_path, LOHIT, labels=W.honest_labels(LOHIT, body))
    out = _repair(path)
    assert out.status == fontrepair.unrepaired(fontrepair.NO_SUSPECT_FONT)
    assert out.text == _native(path)


def test_a_font_without_gsub_fails_coverage_and_keeps_the_native_text(tmp_path):
    """Word 365: shaped with the full font, embedded without GSUB (spec §7)."""
    path = W.word_pdf(tmp_path, LOHIT, embed=W.strip_gsub(LOHIT))
    out = _repair(path)
    assert out.status == fontrepair.unrepaired(fontrepair.COVERAGE)
    assert out.text == _native(path)
    assert out.detail["unresolved_uses"] > 0


def test_a_font_with_no_unicode_cmap_is_never_suspect(tmp_path):
    """Review Focus 1."""
    path = W.word_pdf(tmp_path, LOHIT, embed=W.symbol_cmap_only(LOHIT))
    out = _repair(path)
    assert out.status == fontrepair.unrepaired(fontrepair.NO_SUSPECT_FONT)
    assert out.text == _native(path)


def test_text_inside_a_form_xobject_is_walked(tmp_path):
    """Review Focus 2."""
    out = _repair(W.word_pdf(tmp_path, LOHIT, in_form=True))
    assert out.status == fontrepair.STATUS_REPAIRED, out.detail
    assert out.detail["runs"] == len(W.LINES) * W.REPEAT


def test_save_restore_around_the_text_does_not_lose_the_font(tmp_path):
    """Review Focus 2."""
    out = _repair(W.word_pdf(tmp_path, LOHIT, save_restore=True))
    assert out.status == fontrepair.STATUS_REPAIRED, out.detail
    assert out.detail["runs"] == len(W.LINES) * W.REPEAT


def test_a_roundtrip_mismatch_fails_the_page(tmp_path, monkeypatch):
    path = W.word_pdf(tmp_path, LOHIT)
    monkeypatch.setattr(fontrepair.shaping, "shape", lambda program, text: (0,))
    out = _repair(path)
    assert out.status == fontrepair.unrepaired(fontrepair.ROUNDTRIP)
    assert out.text == _native(path)
    assert out.detail["runs_matched"] == 0
    assert out.detail["examples"][0]["check"] == fontrepair.ROUNDTRIP


def test_an_orphan_marker_fails_the_page(tmp_path):
    lone = [W.shaped_lines(LOHIT, ["क"])[0][0], W.prebase_gid(LOHIT)]
    path = W.word_pdf(tmp_path, LOHIT, extra_line=lone)
    out = _repair(path)
    assert out.status == fontrepair.unrepaired(fontrepair.ORPHANS)


def test_a_page_without_devanagari_is_not_applicable(tmp_path):
    out = fontrepair.FontRepairEngine().repair_page(tmp_path / "absent.pdf", 1, "Annual report 2024")
    assert out.status == fontrepair.NOT_APPLICABLE
    assert out.text == "Annual report 2024"


def test_a_missing_shaper_is_recorded_not_raised(tmp_path, monkeypatch):
    path = W.word_pdf(tmp_path, LOHIT)
    monkeypatch.setattr(fontrepair, "engine_available", lambda: False)
    out = _repair(path)
    assert out.status == fontrepair.unrepaired(fontrepair.SHAPER_UNAVAILABLE)
    assert fontrepair.engine_version() == fontrepair.UNAVAILABLE


def test_an_unreadable_file_is_read_failed(tmp_path):
    """Review Focus 3."""
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf at all")
    out = fontrepair.FontRepairEngine().repair_page(bad, 1, "कार्ाालर्")
    assert out.status == fontrepair.unrepaired(fontrepair.READ_FAILED)
    assert out.text == "कार्ाालर्"


def test_a_garbage_font_program_is_unsupported_font(tmp_path):
    """Review Focus 3."""
    path = W.word_pdf(tmp_path, LOHIT, embed=b"\x00" * 64)
    out = _repair(path)
    assert out.status == fontrepair.unrepaired(fontrepair.UNSUPPORTED_FONT)


def test_the_engine_version_reads_its_terms_live(monkeypatch):
    base = fontrepair.engine_version()
    assert base.startswith("native-3/repair-1/D=1/N=500/lang=ne/hb-")
    monkeypatch.setattr(fontrepair, "DETECT_DENSITY", 0.002)
    assert fontrepair.engine_version() != base


def test_the_layout_check_requires_every_run_intact_and_in_order():
    assert fontrepair.missing_in_order(["क ख", "ग"], "कख\nग") == 0
    assert fontrepair.missing_in_order(["ग", "क"], "क ग") == 1


def test_one_document_is_opened_once_for_many_pages(tmp_path, monkeypatch):
    path = W.word_pdf(tmp_path, LOHIT, pages=3)
    opened = []
    original = fontrepair._Document

    class Counting(original):
        def __init__(self, p):
            opened.append(p)
            super().__init__(p)

    monkeypatch.setattr(fontrepair, "_Document", Counting)
    engine = fontrepair.FontRepairEngine()
    for page in (1, 2, 3):
        assert engine.repair_page(path, page, _native(path, page)).status == fontrepair.STATUS_REPAIRED
    assert len(opened) == 1


def test_fonts_report_names_the_contradicting_font(tmp_path):
    report = fontrepair.fonts_report(W.word_pdf(tmp_path, LOHIT))
    assert report["contradicts"] is True
    assert report["fonts"][0]["identity"] == "Lohit Devanagari/711"
```

- [ ] **Step 3: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_nrb_fontrepair.py -v`
Expected: ERROR, `ModuleNotFoundError: No module named 'app.nrb.fontrepair'`.

- [ ] **Step 4: Implement `app/nrb/fontrepair.py`**

```python
"""Read a PDF page through its embedded font when the PDF's own ToUnicode is wrong.

THE PROBLEM (docs/prod-incident-2026-09-20.md §9.8, §9.10 B; nrb-integration §17.6)
    Microsoft Word labels each glyph of a Devanagari cluster with the character
    at the same POSITION in the cluster, and the first cluster to use a glyph
    decides its label for the whole file. So Kalimati's gid 143 (त) is labelled
    ि in every Word-2013 file, and ~6% of the NRB corpus extracts as
    `कार्ाालर्` for कार्यालय. pypdf and poppler agree byte for byte: the PDFs
    are wrong, not the extractor. OCR fixes the spelling and loses 28-53% of an
    Act, so it cannot replace this text.

THE REPAIR, PER PAGE
    1. Find the page's SUSPECT fonts: Type0 over CIDFontType2, embedded
       `FontFile2`, Identity-H, an Identity or stream CIDToGIDMap, and a
       ToUnicode that contradicts the font's own cmap on at least one code.
       Judged per FONT across its whole ToUnicode (plan clarification 1).
    2. Build each suspect font's `GlyphTable` from its own cmap + GSUB.
    3. Install a CORRECTED ToUnicode — the table's tokens, markers included —
       on an in-memory reader, and extract the page with the SAME pypdf call
       native extraction makes (`documents.read_pdf_pages`), so the page's
       layout (lines, spaces) is pypdf's, unchanged.
    4. `reorder` the result visual → logical.

THE GATE — all four, or the page keeps today's text (spec §4.3)
    coverage   every glyph a suspect font draws resolves in its table
    orphans    reordering attaches every marker, per run and on the page
    roundtrip  every suspect-font run — consecutive text operators in one font
               inside one BT…ET — decoded, reordered and SHAPED with that
               font's own program reproduces the run's drawn gids, 100%
    layout     the served text contains every round-tripped run, whitespace
               ignored, in content order: pypdf only ADDED whitespace

    Density is the DETECTOR (`detect`), never the acceptance: a misread rakar
    produced पर्कृति — wrong, and with zero illegal clusters (§1.1).

FAILURE IS A STATUS, NEVER AN EXCEPTION
    `repair_page` never raises for a bad page or a bad file. Missing
    dependencies, an unreadable file, an unsupported font and every gate
    failure come back as `unrepaired:<reason>` with the native text; the caller
    (`recovery.native_unit`) keeps that text, non-authoritative — rule 6.

Module scope imports only the standard library and our own pure modules.
pypdf is imported inside the functions that need it, fontTools only through
`glyphtable`, uharfbuzz only through `shaping`.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

from . import devanagari, glyphtable, shaping
from .reorder import reorder

logger = logging.getLogger("app.nrb.fontrepair")

__all__ = [
    "COVERAGE", "DETECT_DENSITY", "DETECT_MIN_DEVANAGARI", "Detection",
    "ENGINE_ERROR", "ENGINE_PREFIX", "FontRepairEngine", "LAYOUT",
    "NOT_APPLICABLE", "NO_SUSPECT_FONT", "ORPHANS", "READ_FAILED",
    "REPAIR_VERSION", "ROUNDTRIP", "RepairOutcome", "SHAPER_UNAVAILABLE",
    "STATUS_REPAIRED", "UNAVAILABLE", "UNREPAIRED_PREFIX", "UNSUPPORTED_FONT",
    "corrected_cmap", "detect", "document_evidence", "engine_available",
    "engine_version", "fonts_report", "missing_in_order", "parse_tounicode",
    "unrepaired",
]

# --- identity --------------------------------------------------------------- #
# REPAIR_VERSION is bumped BY HAND for any change to glyphtable's rules,
# reorder, or this gate — after Task 14 of the plan, i.e. once units exist
# under it. The detector constants and the HarfBuzz version are read LIVE into
# `engine_version`, so editing them cannot be forgotten.
ENGINE_PREFIX = "native-3"
REPAIR_VERSION = "repair-1"
UNAVAILABLE = f"{ENGINE_PREFIX}/repair-unavailable"

# --- the detector (spec §4.1), frozen before the cohort is drawn ------------ #
# Development set (production scope, 2026-10-02): affected documents 5.05-185
# illegal clusters per 1k Devanagari characters, cleanest clean one 0.09.
DETECT_DENSITY = 0.001
DETECT_MIN_DEVANAGARI = 500

# --- statuses: a closed vocabulary, like recovery.ROUTES -------------------- #
STATUS_REPAIRED = "font_tables"
NOT_APPLICABLE = "not_applicable"
UNREPAIRED_PREFIX = "unrepaired:"
COVERAGE = "coverage"
ORPHANS = "orphans"
ROUNDTRIP = "roundtrip"
LAYOUT = "layout"
NO_SUSPECT_FONT = "no_suspect_font"
UNSUPPORTED_FONT = "unsupported_font"
SHAPER_UNAVAILABLE = "shaper_unavailable"
READ_FAILED = "read_failed"
ENGINE_ERROR = "engine_error"
GATE_CHECKS = (COVERAGE, ORPHANS, ROUNDTRIP, LAYOUT)
UNREPAIRED_REASONS = (
    *GATE_CHECKS, NO_SUSPECT_FONT, UNSUPPORTED_FONT, SHAPER_UNAVAILABLE,
    READ_FAILED, ENGINE_ERROR,
)

MAX_EXAMPLES = 3
MAX_XOBJECT_DEPTH = 3
_SHOW_OPS = frozenset({b"Tj", b"TJ", b"'", b'"'})


def unrepaired(reason: str) -> str:
    if reason not in UNREPAIRED_REASONS:
        raise ValueError(f"unknown unrepaired reason {reason!r}")
    return UNREPAIRED_PREFIX + reason


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Detection:
    suspect: bool
    density: float
    devanagari_chars: int
    illegal_clusters: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def detect(texts: Sequence[str]) -> Detection:
    """Illegal clusters per Devanagari character, over a document's native pages."""
    illegal = chars = 0
    for text in texts:
        shape = devanagari.measure_devanagari(text)
        illegal += shape.illegal_clusters
        chars += shape.devanagari_chars
    density = illegal / chars if chars else 0.0
    return Detection(
        suspect=chars >= DETECT_MIN_DEVANAGARI and density >= DETECT_DENSITY,
        density=round(density, 6),
        devanagari_chars=chars,
        illegal_clusters=illegal,
    )


# --------------------------------------------------------------------------- #
# Engine identity
# --------------------------------------------------------------------------- #
def engine_available() -> bool:
    if not shaping.available():
        return False
    try:
        import fontTools  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def engine_version() -> str:
    if not engine_available():
        return UNAVAILABLE
    return (
        f"{ENGINE_PREFIX}/{REPAIR_VERSION}"
        f"/D={DETECT_DENSITY * 1000:g}/N={DETECT_MIN_DEVANAGARI}"
        f"/lang={shaping.LANGUAGE}/hb-{shaping.version()}"
    )


# --------------------------------------------------------------------------- #
# ToUnicode
# --------------------------------------------------------------------------- #
_BFCHAR = re.compile(rb"beginbfchar(.*?)endbfchar", re.S)
_BFRANGE = re.compile(rb"beginbfrange(.*?)endbfrange", re.S)
_PAIR = re.compile(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>")
_RANGE = re.compile(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<[0-9A-Fa-f]*>|\[[^\]]*\])", re.S)
_HEX = re.compile(rb"<([0-9A-Fa-f]*)>")


def _utf16(hexstr: bytes) -> str:
    if len(hexstr) % 2:
        hexstr = hexstr + b"0"
    return bytes.fromhex(hexstr.decode("ascii")).decode("utf-16-be", "replace")


def parse_tounicode(data: bytes) -> dict[int, str]:
    labels: dict[int, str] = {}
    for block in _BFCHAR.findall(data):
        for src, dst in _PAIR.findall(block):
            labels[int(src, 16)] = _utf16(dst)
    for block in _BFRANGE.findall(data):
        for lo_hex, hi_hex, dst in _RANGE.findall(block):
            lo, hi = int(lo_hex, 16), int(hi_hex, 16)
            if hi < lo or hi - lo > 0xFFFF:
                continue
            if dst.startswith(b"["):
                for k, item in enumerate(_HEX.findall(dst)):
                    labels[lo + k] = _utf16(item)
                continue
            base = _utf16(dst[1:-1])
            if not base:
                continue
            for k in range(hi - lo + 1):
                last = ord(base[-1]) + k
                if last <= 0x10FFFF:
                    labels[lo + k] = base[:-1] + chr(last)
    return labels


def corrected_cmap(labels: dict[int, str]) -> bytes:
    rows = sorted(labels.items())
    out = [b"/CIDInit /ProcSet findresource begin", b"12 dict begin", b"begincmap",
           b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
           b"/CMapName /NRB-native3-repair def", b"/CMapType 2 def",
           b"1 begincodespacerange", b"<0000> <FFFF>", b"endcodespacerange"]
    for i in range(0, len(rows), 100):
        chunk = rows[i:i + 100]
        out.append(f"{len(chunk)} beginbfchar".encode())
        out += [f"<{c:04X}> <{t.encode('utf-16-be').hex().upper()}>".encode() for c, t in chunk]
        out.append(b"endbfchar")
    out += [b"endcmap", b"CMapName currentdict /CMap defineresource pop", b"end", b"end"]
    return b"\n".join(out)


def missing_in_order(needles: Sequence[str], haystack: str) -> int:
    """How many `needles` do not appear in `haystack` in order, whitespace ignored."""
    hay = "".join(haystack.split())
    cursor = missing = 0
    for needle in needles:
        compact = "".join(needle.split())
        if not compact:
            continue
        at = hay.find(compact, cursor)
        if at < 0:
            missing += 1
        else:
            cursor = at + len(compact)
    return missing


# --------------------------------------------------------------------------- #
# Fonts, documents, runs
# --------------------------------------------------------------------------- #
def _resolve(obj: Any) -> Any:
    getter = getattr(obj, "get_object", None)
    return getter() if callable(getter) else obj


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


@dataclass
class _Font:
    name: str
    font_obj: Any
    usable: bool = False
    why: str | None = None
    program: bytes = b""
    table: glyphtable.GlyphTable | None = None
    cid_to_gid: dict[int, int] | None = None
    labels: dict[int, str] = field(default_factory=dict)
    contradictions: int = 0
    corrected: bool = False

    @property
    def suspect(self) -> bool:
        return self.usable and self.contradictions > 0

    def gid(self, code: int) -> int:
        return code if self.cid_to_gid is None else self.cid_to_gid.get(code, 0)

    def codes_for(self, gid: int) -> list[int]:
        if self.cid_to_gid is None:
            return [gid]
        return [code for code, g in self.cid_to_gid.items() if g == gid]

    def install_corrected(self) -> None:
        """Replace this font's ToUnicode, in memory, with the table's tokens."""
        if self.corrected or self.table is None:
            return
        from pypdf.generic import DecodedStreamObject, NameObject

        labels = dict(self.labels)
        for gid, token in self.table.tokens.items():
            if gid in self.table.ambiguous:
                continue
            for code in self.codes_for(gid):
                labels[code] = token
        stream = DecodedStreamObject()
        stream.set_data(corrected_cmap(labels))
        self.font_obj[NameObject("/ToUnicode")] = stream
        self.corrected = True


def _cid_to_gid(obj: Any) -> dict[int, int] | None:
    resolved = _resolve(obj)
    if resolved is None or str(resolved) == "/Identity":
        return None
    data = resolved.get_data()
    return {i: int.from_bytes(data[2 * i:2 * i + 2], "big") for i in range(len(data) // 2)}


def _contradictions(font: _Font) -> int:
    count = 0
    table = font.table
    assert table is not None
    for code, label in font.labels.items():
        chars = table.cmap_chars.get(font.gid(code))
        if chars and _nfc(label) not in {_nfc(c) for c in chars}:
            count += 1
    return count


def _analyse(font: Any) -> _Font:
    out = _Font(name=str(font.get("/BaseFont", "") or ""), font_obj=font)
    if str(font.get("/Subtype", "")) != "/Type0":
        out.why = "not_type0"
        return out
    out.why = UNSUPPORTED_FONT
    if str(_resolve(font.get("/Encoding"))) != "/Identity-H":
        return out
    descendants = _resolve(font.get("/DescendantFonts")) or []
    if not descendants:
        return out
    cid = _resolve(descendants[0])
    if str(cid.get("/Subtype", "")) != "/CIDFontType2":
        return out
    descriptor = _resolve(cid.get("/FontDescriptor"))
    program_ref = descriptor.get("/FontFile2") if descriptor is not None else None
    if program_ref is None:
        return out
    try:
        out.program = _resolve(program_ref).get_data()
        out.cid_to_gid = _cid_to_gid(cid.get("/CIDToGIDMap"))
        out.table = glyphtable.build_glyph_table(out.program)
        tounicode = font.get("/ToUnicode")
        out.labels = parse_tounicode(_resolve(tounicode).get_data()) if tounicode is not None else {}
    except Exception as exc:  # noqa: BLE001 - an unreadable program is a status
        logger.info("native-3: font %s unusable (%s)", out.name, type(exc).__name__)
        return out
    out.usable, out.why = True, None
    out.contradictions = _contradictions(out)
    return out


@dataclass
class _Run:
    font: _Font
    gids: list[int]


def _raw(obj: Any) -> bytes | None:
    if isinstance(obj, (bytes, bytearray)):
        return bytes(obj)
    original = getattr(obj, "original_bytes", None)
    if original is not None:
        return bytes(original)
    return None


def _strings(op: bytes, operands: Sequence[Any]) -> list[bytes]:
    if op == b"TJ":
        return [b for b in (_raw(x) for x in (operands[0] if operands else [])) if b is not None]
    item = operands[2] if op == b'"' and len(operands) > 2 else (operands[0] if operands else None)
    raw = _raw(item)
    return [raw] if raw is not None else []


def _codes(raw: bytes) -> list[int]:
    return [int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw) - 1, 2)]


class _Document:
    """One PDF opened for repair. Its fonts are analysed once and corrected in place."""

    def __init__(self, path: Path) -> None:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                opened = reader.decrypt("")
            except Exception:  # noqa: BLE001
                opened = 0
            if not opened:
                raise PermissionError("encrypted")
        self.reader = reader
        self._fonts: dict[Any, _Font] = {}

    def font(self, fonts: Any, key: Any) -> _Font | None:
        if fonts is None or not hasattr(fonts, "get"):
            return None
        raw = fonts.raw_get(key) if hasattr(fonts, "raw_get") else fonts.get(key)
        if raw is None:
            return None
        ident = (raw.idnum, raw.generation) if hasattr(raw, "idnum") else id(raw)
        if ident not in self._fonts:
            self._fonts[ident] = _analyse(_resolve(raw))
        return self._fonts[ident]

    def runs(self, page: Any) -> tuple[list[_Run], set[str]]:
        """Every text run in a usable Type0 font, and the names of unusable ones."""
        try:
            resources = page.get_inherited("/Resources")
        except Exception:  # noqa: BLE001
            resources = None
        if resources is None:
            resources = page.get("/Resources")
        out: list[_Run] = []
        unusable: set[str] = set()
        contents = page.get_contents()
        if contents is not None:
            self._walk(contents, _resolve(resources), out, unusable, 0, set())
        return out, unusable

    def _walk(self, content: Any, resources: Any, out: list[_Run], unusable: set[str],
              depth: int, seen: set[Any]) -> None:
        from pypdf.generic import ContentStream

        stream = content if hasattr(content, "operations") else ContentStream(content, self.reader)
        fonts = _resolve(resources.get("/Font")) if resources is not None else None
        xobjects = _resolve(resources.get("/XObject")) if resources is not None else None
        current: _Font | None = None
        saved: list[_Font | None] = []
        run: _Run | None = None

        def flush() -> None:
            nonlocal run
            if run is not None and run.gids:
                out.append(run)
            run = None

        for operands, op in stream.operations:
            if op == b"q":
                saved.append(current)
            elif op == b"Q":
                restored = saved.pop() if saved else current
                if restored is not current:
                    flush()
                current = restored
            elif op in (b"BT", b"ET"):
                flush()
            elif op == b"Tf" and operands:
                font = self.font(fonts, operands[0])
                if font is not current:
                    flush()
                current = font
                if font is not None and not font.usable and font.why == UNSUPPORTED_FONT:
                    unusable.add(font.name)
            elif op in _SHOW_OPS:
                if current is None or not current.usable:
                    flush()
                    continue
                if run is None:
                    run = _Run(current, [])
                for raw in _strings(op, operands):
                    run.gids.extend(current.gid(c) for c in _codes(raw))
            elif op == b"Do" and operands and xobjects is not None and depth < MAX_XOBJECT_DEPTH:
                ref = xobjects.raw_get(operands[0]) if hasattr(xobjects, "raw_get") else xobjects.get(operands[0])
                if ref is None:
                    continue
                ident = (ref.idnum, ref.generation) if hasattr(ref, "idnum") else id(ref)
                xobject = _resolve(ref)
                if ident in seen or str(xobject.get("/Subtype", "")) != "/Form":
                    continue
                flush()
                seen.add(ident)
                inner = _resolve(xobject.get("/Resources")) or resources
                self._walk(xobject, inner, out, unusable, depth + 1, seen)
        flush()


# --------------------------------------------------------------------------- #
# The engine
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RepairOutcome:
    status: str
    text: str
    detail: dict[str, Any] = field(default_factory=dict)


def _example(detail: dict[str, Any], check: str, **facts: Any) -> None:
    if len(detail["examples"]) < MAX_EXAMPLES:
        detail["examples"].append({"check": check, **facts})


class FontRepairEngine:
    """The native route's repair engine. One per process; one PDF open at a time.

    Not thread-safe, and need not be: the worker recovers one document at a
    time inside `asyncio.to_thread`.
    """

    name = "font_tables"

    def __init__(self) -> None:
        self._key: tuple[str, int, int] | None = None
        self._doc: _Document | None = None

    @property
    def available(self) -> bool:
        return engine_available()

    @property
    def version(self) -> str:
        return engine_version()

    def _document(self, path: Path) -> _Document:
        stat = path.stat()
        key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
        if key != self._key or self._doc is None:
            self._doc = _Document(path)
            self._key = key
        return self._doc

    def repair_page(self, path: Path, page_number: int, native_text: str) -> RepairOutcome:
        path = Path(path)
        if devanagari.measure_devanagari(native_text).devanagari_chars == 0:
            return RepairOutcome(NOT_APPLICABLE, native_text)
        if not engine_available():
            return RepairOutcome(unrepaired(SHAPER_UNAVAILABLE), native_text)
        try:
            doc = self._document(path)
            page = doc.reader.pages[page_number - 1]
            runs, unusable = doc.runs(page)
        except Exception as exc:  # noqa: BLE001 - a bad file is a status
            return RepairOutcome(unrepaired(READ_FAILED), native_text, {"error": type(exc).__name__})

        suspect = {id(r.font): r.font for r in runs if r.font.suspect}
        if not suspect:
            reason = UNSUPPORTED_FONT if unusable else NO_SUSPECT_FONT
            return RepairOutcome(unrepaired(reason), native_text,
                                 {"unusable_fonts": sorted(unusable)})
        for font in suspect.values():
            font.install_corrected()

        detail: dict[str, Any] = {
            "runs": 0, "runs_matched": 0, "glyph_uses": 0, "unresolved_uses": 0,
            "orphans": 0, "layout_missing": 0,
            "fonts": sorted({f.name for f in suspect.values()}),
            "font_identities": sorted({f.table.identity.label() for f in suspect.values() if f.table}),
            "examples": [],
        }
        logical_runs: list[str] = []
        for run in runs:
            if not run.font.suspect:
                continue
            detail["runs"] += 1
            table = run.font.table
            assert table is not None
            tokens = [table.token(g) for g in run.gids]
            detail["glyph_uses"] += len(tokens)
            missing = [g for g, t in zip(run.gids, tokens) if t is None]
            if missing:
                detail["unresolved_uses"] += len(missing)
                _example(detail, COVERAGE, gids=missing[:12])
                continue
            out = reorder("".join(tokens))  # type: ignore[arg-type]
            if out.orphans:
                detail["orphans"] += out.orphans
                _example(detail, ORPHANS, text=out.text[:80])
                continue
            shaped = list(shaping.shape(run.font.program, out.text))
            if shaped == run.gids:
                detail["runs_matched"] += 1
            else:
                _example(detail, ROUNDTRIP, text=out.text[:80],
                         drawn=run.gids[:24], shaped=shaped[:24])
            logical_runs.append(out.text)

        served = reorder(page.extract_text() or "")
        detail["orphans"] += served.orphans
        detail["layout_missing"] = missing_in_order(logical_runs, served.text)
        detail["illegal_before"] = devanagari.illegal_cluster_count(native_text)
        detail["illegal_after"] = devanagari.illegal_cluster_count(served.text)

        failed = None
        if detail["unresolved_uses"]:
            failed = COVERAGE
        elif detail["orphans"]:
            failed = ORPHANS
        elif detail["runs_matched"] < detail["runs"]:
            failed = ROUNDTRIP
        elif detail["layout_missing"]:
            failed = LAYOUT
        if failed is not None:
            return RepairOutcome(unrepaired(failed), native_text, detail)
        return RepairOutcome(STATUS_REPAIRED, served.text, detail)


# --------------------------------------------------------------------------- #
# Evidence helpers (scripts and the native-3 extractor; no serving path uses them)
# --------------------------------------------------------------------------- #
def fonts_report(path: Path) -> dict[str, Any]:
    """Every Type0 font any page draws with, and whether one contradicts its cmap."""
    try:
        doc = _Document(Path(path))
    except Exception as exc:  # noqa: BLE001
        return {"error": type(exc).__name__, "fonts": [], "contradicts": False}
    fonts: dict[int, _Font] = {}
    for page in doc.reader.pages:
        try:
            runs, _ = doc.runs(page)
        except Exception:  # noqa: BLE001
            continue
        for run in runs:
            fonts[id(run.font)] = run.font
    rows = [
        {
            "name": f.name,
            "identity": f.table.identity.label() if f.table else None,
            "has_gsub": bool(f.table and f.table.has_gsub),
            "contradictions": f.contradictions,
        }
        for f in fonts.values()
    ]
    return {"fonts": rows, "contradicts": any(f.suspect for f in fonts.values())}


def document_evidence(
    engine: FontRepairEngine, path: Path, pages: Sequence[str], native_numbers: Sequence[int]
) -> dict[str, Any]:
    """Detection over the native pages and, when it fires, the repair of each."""
    detection = detect([pages[n - 1] for n in native_numbers])
    out: dict[str, Any] = {
        "engine": engine.version,
        "detection": detection.as_dict(),
        "native_pages": len(native_numbers),
        "statuses": {},
        "pages": [],
        "runs": 0,
        "runs_matched": 0,
        "font_identities": [],
        "examples": [],
    }
    if not detection.suspect:
        return out
    for number in native_numbers:
        outcome = engine.repair_page(path, number, pages[number - 1])
        out["statuses"][outcome.status] = out["statuses"].get(outcome.status, 0) + 1
        out["pages"].append({"page": number, "status": outcome.status,
                             "font_identities": outcome.detail.get("font_identities", [])})
        out["runs"] += outcome.detail.get("runs", 0)
        out["runs_matched"] += outcome.detail.get("runs_matched", 0)
        for label in outcome.detail.get("font_identities", []):
            if label not in out["font_identities"]:
                out["font_identities"].append(label)
        for example in outcome.detail.get("examples", []):
            if len(out["examples"]) < 5:
                out["examples"].append({"page": number, **example})
    return out
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_nrb_fontrepair.py -v`
Expected: all passed, with the Kalimati case running because the font is installed.

If `test_a_word_page_is_repaired_to_exactly_the_shaped_text` fails only because pypdf splits or joins lines differently, print `out.text` and adjust `_lines()`, never the expected text. If it fails on `runs_matched`, print `out.detail["examples"]` and debug `glyphtable`/`reorder` as in Task 3, Step 4.

- [ ] **Step 6: Run the full suite (G11), then commit**

```bash
git add app/nrb/fontrepair.py tests/nrb_font_pdf.py tests/test_nrb_fontrepair.py
git commit -m "feat(nrb): native-3 page repair through the embedded font, with the round-trip gate

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: `recovery.page_routes`, the detect script, and CHECKPOINT A (development measurement)

This task measures §11 risk 1 on the development set and **stops for the user's decision** before any integration work.

**Files:**
- Modify: `app/nrb/recovery.py` (add `page_routes` and `_routes`; `_recover_pdf` uses `_routes`; behaviour unchanged)
- Create: `scripts/nrb_native3_detect.py`
- Modify: `tests/test_nrb_dbguard.py` (add the script to `OPERATIONAL`)
- Test: `tests/test_nrb_native_repair_recovery.py` (created here; Task 6 extends it)
- Modify: `docs/nrb-integration.md` (new `## 31.` heading, `### 31.1`)

**Interfaces:**
- Produces: `recovery.page_routes(path: Path, plan: DocumentPlan, pages: Sequence[str]) -> tuple[tuple[str, str], ...]`. For `PLAN_NATIVE` every page is `(native, plan.reason)`; for `PLAN_PAGES` it is `route_page` per page; for any other plan it is `()`.
- Produces: `scripts/nrb_native3_detect.py --department CODE --out FILE [--repair-report] [--ids-out FILE] [--only PREFIX…]`.

- [ ] **Step 1: Pin `plan_document` and `route_page` BEFORE touching `recovery.py`**

Run this and copy the two hashes it prints:
```bash
.venv/bin/python - <<'EOF'
import ast, hashlib, inspect, textwrap
from app.nrb import recovery
for fn in (recovery.plan_document, recovery.route_page):
    tree = ast.dump(ast.parse(textwrap.dedent(inspect.getsource(fn))))
    print(fn.__name__, hashlib.sha256(tree.encode()).hexdigest())
EOF
```

- [ ] **Step 2: Write the failing tests** — create `tests/test_nrb_native_repair_recovery.py`

Replace `<HASH-PLAN>` and `<HASH-ROUTE>` with the two values Step 1 printed. They pin the routing code as it stands now, so it is the condition behind spec D1, not a guess.

```python
"""native-3 inside recovery: routing untouched, repair on native pages only (spec §2, §3, §5)."""

from __future__ import annotations

import ast
import hashlib
import inspect
import textwrap

import pytest

from app.nrb import extraction, recovery

# Recorded by Task 5 Step 1, BEFORE recovery.py was edited. If either changes,
# the routing changed: bump RECOVERY_ROUTING_VERSION (the base version) and
# re-record — the engine-version design (D1) is only honest while these hold.
PINNED = {
    "plan_document": "<HASH-PLAN>",
    "route_page": "<HASH-ROUTE>",
}


def _ast_sha(fn) -> str:
    return hashlib.sha256(ast.dump(ast.parse(textwrap.dedent(inspect.getsource(fn)))).encode()).hexdigest()


def test_the_routing_functions_are_unchanged():
    assert _ast_sha(recovery.plan_document) == PINNED["plan_document"]
    assert _ast_sha(recovery.route_page) == PINNED["route_page"]
    assert recovery.RECOVERY_ROUTING_VERSION == "recovery-1"


def test_page_routes_for_a_native_plan_is_native_everywhere(tmp_path):
    plan = recovery.DocumentPlan(recovery.PLAN_NATIVE, "clean", None)
    assert recovery.page_routes(tmp_path / "x.pdf", plan, ["a", "b"]) == (
        (recovery.ROUTE_NATIVE, "clean"), (recovery.ROUTE_NATIVE, "clean"))


def test_page_routes_for_no_recovery_is_empty(tmp_path):
    plan = recovery.DocumentPlan(recovery.PLAN_NONE, "parser_error", None)
    assert recovery.page_routes(tmp_path / "x.pdf", plan, ["a"]) == ()


def test_page_routes_agree_with_what_recover_routes(tmp_path):
    """One routing answer, two callers: recover() and the evidence scripts."""
    from tests.test_nrb_recovery import _write_pdf

    path = _write_pdf(tmp_path, "mixed.pdf", [
        {"font": "Preeti", "embedded": True}, {"image": True}, {"font": "Preeti", "embedded": True},
    ])
    plan = recovery.DocumentPlan(recovery.PLAN_PAGES, "legacy_font_suspected", 1.0)
    pages = ["g]kfn /fi6« a}+s " * 5, "", "g]kfn /fi6« a}+s " * 5]
    routes = recovery.page_routes(path, plan, pages)
    assert [r for r, _ in routes] == [recovery.ROUTE_LEGACY, recovery.ROUTE_OCR, recovery.ROUTE_LEGACY]
```

- [ ] **Step 3: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_nrb_native_repair_recovery.py -v`
Expected: `test_the_routing_functions_are_unchanged` PASSES (it pins today's code). The `page_routes` tests FAIL with `AttributeError: module 'app.nrb.recovery' has no attribute 'page_routes'`.

- [ ] **Step 4: Add `page_routes` to `app/nrb/recovery.py`**

Add `"page_routes",` to `__all__` (alphabetical, after `"ocr_unit",`). Insert this after `route_page` (before the `# Execution.` banner):

```python
def _routes(
    prov: provenance.DocumentProvenance, plan_reason: str, pages: Sequence[str]
) -> tuple[tuple[str, str], ...]:
    """`route_page` for every page of a page-routed PDF, in page order."""
    return tuple(
        route_page(prov.page(index), plan_reason=plan_reason, text_chars=len(text.strip()))
        for index, text in enumerate(pages, start=1)
    )


def page_routes(
    path: Path, plan: DocumentPlan, pages: Sequence[str]
) -> tuple[tuple[str, str], ...]:
    """`(route, why)` per page, decided exactly as `recover` decides it.

    Public because the native-3 evidence (scripts, `extraction`'s native-3
    metrics) must ask "which pages are native" the same way recovery does —
    the detector's input is the NATIVE pages, and a second derivation of that
    set could disagree with the one that serves text.
    """
    if plan.plan == PLAN_NATIVE:
        return tuple((ROUTE_NATIVE, plan.reason) for _ in pages)
    if plan.plan != PLAN_PAGES:
        return ()
    return _routes(provenance.read_pdf_provenance(path), plan.reason, pages)
```

In `_recover_pdf`, replace the loop header:

```python
    out: list[PageText] = []
    for index, text in enumerate(pages, start=1):
        route, why = route_page(
            prov.page(index), plan_reason=plan.reason, text_chars=len(text.strip())
        )
```
with:
```python
    out: list[PageText] = []
    for index, (text, (route, why)) in enumerate(
        zip(pages, _routes(prov, plan.reason, pages)), start=1
    ):
```
The loop body is unchanged.

- [ ] **Step 5: Run the recovery tests to verify the refactor changed nothing**

Run: `.venv/bin/pytest tests/test_nrb_native_repair_recovery.py tests/test_nrb_recovery.py tests/test_nrb_recovery_cache.py -q`
Expected: all passed. `test_the_routing_functions_are_unchanged` still passes, because only `_recover_pdf` changed.

- [ ] **Step 6: Write `scripts/nrb_native3_detect.py`**

```python
#!/usr/bin/env python
"""Which NRB documents does native-3's detector flag, and what would the repair do?

    DATABASE_URL=postgresql+asyncpg://gateway:***@127.0.0.1:5432/local_ai_gateway_build \\
        .venv/bin/python scripts/nrb_native3_detect.py --department nrb \\
        --out "$SCRATCH/detect.json" [--repair-report] [--ids-out "$SCRATCH/ids.txt"]

Three uses (docs/superpowers/plans/2026-10-02-native3-font-repair.md):
  * CHECKPOINT A — `--repair-report` over the production scope (development
    evidence): run agreement, pages repaired, every failure classified;
  * Task 14 — `--ids-out` lists the documents to re-ingest;
  * `--only` narrows to blob prefixes while debugging one document.

OPERATIONAL (app/nrb/dbguard.py): reads documents and blobs, writes files,
changes nothing in the database. It runs the classifier and the repair exactly
as recovery would (`recovery.page_routes`, `fontrepair.document_evidence`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.files import documents as file_documents  # noqa: E402
from app.nrb import dbguard, extraction, filestore, fontrepair, recovery  # noqa: E402

_DOCS_SQL = text("""
    SELECT d.id, d.title, d.content_hash, d.file_type
      FROM documents d JOIN departments dep ON dep.id = d.department_id
     WHERE dep.code = :code AND d.status = 'ready' AND d.metadata->>'origin' = 'nrb'
     ORDER BY d.id
""")


def _producer(path: Path) -> str:
    try:
        from pypdf import PdfReader

        meta = PdfReader(str(path)).metadata or {}
        return str(meta.get("/Producer") or meta.get("/Creator") or "")
    except Exception:  # noqa: BLE001
        return ""


def examine(path: Path, engine: fontrepair.FontRepairEngine, *, repair: bool) -> dict:
    result = extraction.extract_file(path, family="pdf", extension="pdf", extractor_version="native-2")
    plan = recovery.plan_document(family=result.family, status=result.status,
                                  reason=result.reason, metrics=result.metrics)
    pages = list(file_documents.read_pdf_pages(path).pages)
    routes = recovery.page_routes(path, plan, pages)
    native = [n for n, (route, _) in enumerate(routes, start=1) if route == recovery.ROUTE_NATIVE]
    record = {"plan": plan.plan, "page_count": len(pages)}  # "pages" is evidence's per-page list
    if repair:
        record |= fontrepair.document_evidence(engine, path, pages, native)
    else:
        record["native_pages"] = len(native)
        record["detection"] = fontrepair.detect([pages[n - 1] for n in native]).as_dict()
    return record


async def _documents(url: str, code: str) -> list[dict]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return [dict(r._mapping) for r in await conn.execute(_DOCS_SQL, {"code": code})]
    finally:
        await engine.dispose()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--department", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--repair-report", action="store_true")
    ap.add_argument("--ids-out", default=None)
    ap.add_argument("--only", nargs="*", default=None, help="blob sha prefixes")
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL", "")
    try:
        name = dbguard.require(url, dbguard.BUILD_DATABASES)
    except dbguard.RefusedDatabase as exc:
        print(f"refusing to run: {exc}", file=sys.stderr)
        return 2
    print(f"database: {name}")

    engine = fontrepair.FontRepairEngine()
    print(f"engine: {engine.version}")
    rows = asyncio.run(_documents(url, args.department))
    records = []
    for row in rows:
        sha = row["content_hash"]
        if (row["file_type"] or "").lower() != "pdf":
            continue
        if args.only and not any(sha.startswith(p) for p in args.only):
            continue
        path = filestore.resolve_path(filestore.storage_key_for(sha, row["file_type"]))
        if not path.exists():
            records.append({"document_id": row["id"], "sha": sha[:12], "missing": True})
            continue
        record = examine(path, engine, repair=args.repair_report)
        record |= {"document_id": row["id"], "sha": sha[:12], "title": row["title"],
                   "producer": _producer(path)}
        records.append(record)
        if record.get("detection", {}).get("suspect"):
            print(f"  detected {sha[:12]} {record['detection']['density'] * 1000:.2f}/1k "
                  f"{record.get('statuses', '')} {row['title'][:50]}", flush=True)

    detected = [r for r in records if r.get("detection", {}).get("suspect")]
    summary = {"documents": len(records), "detected": len(detected)}
    if args.repair_report:
        statuses = Counter()
        for r in detected:
            statuses.update(r.get("statuses", {}))
        runs = sum(r.get("runs", 0) for r in detected)
        matched = sum(r.get("runs_matched", 0) for r in detected)
        summary |= {"page_statuses": dict(statuses), "runs": runs, "runs_matched": matched,
                    "run_agreement": round(matched / runs, 4) if runs else None}
        by_producer: dict[str, Counter] = {}
        for r in detected:
            by_producer.setdefault(r["producer"][:40], Counter()).update(r.get("statuses", {}))
        summary["by_producer"] = {k: dict(v) for k, v in by_producer.items()}
    Path(args.out).write_text(json.dumps({"database": name, "engine": engine.version,
                                          "summary": summary, "documents": records},
                                         ensure_ascii=False, indent=2) + "\n")
    if args.ids_out:
        Path(args.ids_out).write_text("".join(f"{r['document_id']}\n" for r in detected))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 7: Add the script to the guard test's OPERATIONAL map**

In `tests/test_nrb_dbguard.py`, inside `OPERATIONAL = {…}`, add:
```python
    "nrb_native3_detect.py": ["--department", "nrb", "--out", "/dev/null"],
```

Run: `.venv/bin/pytest tests/test_nrb_dbguard.py -q`
Expected: all passed, including the two new parametrizations.

- [ ] **Step 8: Run the development measurement as a unit (G10)**

```bash
BUILD_URL="$(.venv/bin/python -c "from app.config import get_settings; u=get_settings().database_url; print(u.rsplit('/',1)[0]+'/local_ai_gateway_build')")"
systemd-run --user --wait --collect --unit=native3-checkpoint-a --working-directory="$PWD" \
  -E DATABASE_URL="$BUILD_URL" -p StandardOutput=file:"$SCRATCH/checkpoint-a.log" -p StandardError=file:"$SCRATCH/checkpoint-a.log" \
  .venv/bin/python scripts/nrb_native3_detect.py --department nrb --out "$SCRATCH/checkpoint-a.json" --repair-report
tail -40 "$SCRATCH/checkpoint-a.log"
```
The `-E` value carries the password, so never echo `$BUILD_URL`.
Expected: `database: local_ai_gateway_build`, then about 27–30 `detected …` lines and a summary with `run_agreement` near 0.996+.

- [ ] **Step 9: Classify every failure; fix the ones that are ours**

For each entry in `checkpoint-a.json` → `documents[].examples`:
- **`coverage` / `orphans`, or a `roundtrip` where `shaped` ≠ `drawn` because OUR text is wrong.** Reproduce it as a pure test: a `GT.derive` rule case in `tests/test_nrb_glyphtable.py`, or a visual-string case in `tests/test_nrb_reorder.py`. Watch it fail, fix `glyphtable`/`reorder`, then re-run Step 8. To see glyph names for a run, use `/home/manoj/ab-rule/font_check.py`-style inspection on that page (`fontTools.ttLib.TTFont(...).getGlyphOrder()`). The cases already known from the probe:
  - `glyph00226` "र् या" (eyelash ra; check whether Kalimati's cmap has U+200D);
  - `भित्र्याउन` (`5acd61c8e9a8` p.27);
  - fonts with no Unicode cmap (now handled; confirm they report `no_suspect_font`).
- **`roundtrip` where our text is right and Word chose a different glyph than HarfBuzz.** This is a genuine shaping difference, e.g. `परिषद्` (`2b11bf653aa2` p.21, `glyph00217` vs `glyph00622`). Record it, with the word and both glyph ids. **Do not** loosen the gate.

Keep `REPAIR_VERSION = "repair-1"` throughout (plan clarification 6).

- [ ] **Step 10: Record §31.1 in `docs/nrb-integration.md`**

Append a new top-level section at the end of the file, titled `## 31. native-3 — repairing Word text layers through their own fonts`, with `### 31.1 Development measurement (CHECKPOINT A)`. It contains:
- the date;
- the database;
- the engine string;
- the counts table: documents, detected, pages attempted, repaired, and unrepaired by reason;
- run agreement overall and by producer;
- the classified residue: a list of genuine Word-vs-HarfBuzz differences with word and gids, plus every bug fixed, each with its test name;
- the sentence "Development evidence — the production scope shaped this; it proves nothing (spec §6.1)."

- [ ] **Step 11: Run the full suite (G11), then commit**

```bash
git add app/nrb/recovery.py scripts/nrb_native3_detect.py tests/test_nrb_dbguard.py \
        tests/test_nrb_native_repair_recovery.py docs/nrb-integration.md app/nrb/glyphtable.py \
        app/nrb/reorder.py tests/test_nrb_glyphtable.py tests/test_nrb_reorder.py
git commit -m "feat(nrb): native-3 detect report, and the development measurement (§31.1)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 12: STOP — CHECKPOINT A. Report to the user and wait.**

Report:
- the §31.1 table;
- the run agreement;
- the share of attempted Word 2007–2013 pages repaired;
- every genuine Word-vs-HarfBuzz difference.

Suggested go criterion, for the user to accept or change: **≥ 90% of attempted Word 2007–2013 pages repaired, and every remaining failure classified.** Do not start Task 6 without an explicit go.

---

# Milestone 2 — integration behind the flag

### Task 6: `recovery.native_unit` and rule 6

**Files:**
- Modify: `app/nrb/recovery.py`
- Test: `tests/test_nrb_native_repair_recovery.py` (extend)

**Interfaces:**
- Consumes: `fontrepair.detect`, `fontrepair.RepairOutcome`, `fontrepair.STATUS_REPAIRED`, `fontrepair.unrepaired`, `fontrepair.ENGINE_ERROR`; a repair engine with `.version` and `.repair_page(path, page_number, native_text)`.
- Produces:
  - `recovery.native_unit(number: int, text: str, *, reason: str, path: Path, suspect: bool, repair: Any | None) -> PageText`;
  - `recovery.recover(..., repair=None)`;
  - the warning string `f"tounicode_suspected:{density:g}"` on detected documents when `repair` is not None.
  - Repaired/unrepaired pages carry `detail["repair"]` and `detail["repair_engine"]`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_nrb_native_repair_recovery.py`

```python
from pathlib import Path  # noqa: E402

from app.files import documents as file_documents  # noqa: E402
from app.nrb import fontrepair  # noqa: E402

uharfbuzz = pytest.importorskip("uharfbuzz")
from tests import nrb_font_pdf as W  # noqa: E402

LOHIT = W.LOHIT.read_bytes()


class StubRepair:
    """A repair engine that records calls and answers what it is told to."""

    def __init__(self, status=fontrepair.STATUS_REPAIRED, text="REPAIRED", raises=None):
        self.version = "native-3/stub"
        self.status, self.text, self.raises = status, text, raises
        self.calls: list[int] = []

    def repair_page(self, path, page_number, native_text):
        self.calls.append(page_number)
        if self.raises:
            raise self.raises
        return fontrepair.RepairOutcome(self.status, self.text, {"runs": 1, "runs_matched": 1})


def _classified(path: Path):
    return extraction.extract_file(path, family="pdf", extension="pdf", extractor_version="native-2")


def test_without_a_repair_engine_recovery_is_byte_identical(tmp_path):
    path = W.word_pdf(tmp_path, LOHIT)
    doc = recovery.recover(path, _classified(path))
    assert [p.text for p in doc.pages] == list(file_documents.read_pdf_pages(path).pages)
    assert all(p.detail == {} for p in doc.pages)
    assert not any(w.startswith("tounicode_suspected") for w in doc.warnings)


def test_a_detected_document_is_repaired_with_the_real_engine(tmp_path):
    path = W.word_pdf(tmp_path, LOHIT)
    doc = recovery.recover(path, _classified(path), repair=fontrepair.FontRepairEngine())
    page = doc.pages[0]
    assert page.route == recovery.ROUTE_NATIVE and page.ok and page.indexable
    assert page.detail["repair"] == fontrepair.STATUS_REPAIRED
    assert [l.strip() for l in page.text.splitlines() if l.strip()] == list(W.LINES) * W.REPEAT
    assert any(w.startswith("tounicode_suspected:") for w in doc.warnings)


def test_an_undetected_document_never_calls_the_engine(tmp_path):
    """Too little Devanagari to judge (N=500): the engine is not consulted."""
    path = W.word_pdf(tmp_path, LOHIT, repeat=1)
    stub = StubRepair()
    doc = recovery.recover(path, _classified(path), repair=stub)
    assert stub.calls == []
    assert doc.pages[0].text == file_documents.read_pdf_pages(path).pages[0]


def test_an_unrepaired_page_keeps_its_native_text_and_stays_indexable(tmp_path):
    """Rule 6 — the deliberate exception to rule 5 (spec §5.1)."""
    path = W.word_pdf(tmp_path, LOHIT)
    stub = StubRepair(status=fontrepair.unrepaired(fontrepair.COVERAGE), text="MUST NOT APPEAR")
    page = recovery.recover(path, _classified(path), repair=stub).pages[0]
    assert page.text == file_documents.read_pdf_pages(path).pages[0]
    assert page.indexable
    assert page.detail["repair"] == "unrepaired:coverage"
    assert page.detail["repair_engine"] == "native-3/stub"


def test_a_failed_legacy_conversion_still_withholds():
    """Rule 6's other half: rule 5 is unchanged for the converter."""
    page = recovery.convert_unit(1, "g]kfn /fi6« a}+s", reason="embedded_font",
                                 converter=None, lexicon=None, document_legacy_ratio=1.0)
    assert page.ok is False and page.text == ""


def test_a_repair_engine_that_raises_keeps_the_native_text(tmp_path):
    """Review Focus 3."""
    path = W.word_pdf(tmp_path, LOHIT)
    stub = StubRepair(raises=RuntimeError("boom"))
    page = recovery.recover(path, _classified(path), repair=stub).pages[0]
    assert page.text == file_documents.read_pdf_pages(path).pages[0]
    assert page.detail["repair"] == fontrepair.unrepaired(fontrepair.ENGINE_ERROR)


def test_native_unit_without_suspicion_is_plain_passthrough(tmp_path):
    stub = StubRepair()
    page = recovery.native_unit(3, "पाठ", reason="clean", path=tmp_path / "x.pdf",
                                suspect=False, repair=stub)
    assert (page.route, page.reason, page.text, page.detail) == (recovery.ROUTE_NATIVE, "clean", "पाठ", {})
    assert stub.calls == []
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_nrb_native_repair_recovery.py -v`
Expected: the new tests FAIL. `recover()` raises `TypeError: … unexpected keyword argument 'repair'`, and `native_unit` raises `AttributeError`.

- [ ] **Step 3: Implement it in `app/nrb/recovery.py`**

1. Change the import line `from . import extraction, legacy_convert, provenance, quality` to `from . import extraction, fontrepair, legacy_convert, provenance, quality`. `fontrepair`'s module scope is standard library only.
2. Add `"native_unit",` to `__all__` (after `"convert_unit",`, alphabetically before `"ocr_unit"`).
3. In the module docstring, under rule 5, add:

```text
**6. A native page the font repair could not fix keeps its native text — a
deliberate exception to rule 5.** Rule 5 withholds the input of a failed
CONVERSION because that input is glyph-mapped ASCII: unreadable, noise to
search. The input here is Unicode Devanagari that is mostly right, and the
only other source of an Act's text — OCR — loses 28-53% of it. So a page whose
`native-3` repair did not pass its gate is served exactly as before, now marked
non-authoritative (`rag._chunk_meta`) so every reader shows the VERIFY caveat.
Withholding stays the rule everywhere else (spec §5).

`ROUTE_NATIVE` therefore means "the PDF's own text layer — read through its
embedded font program when its ToUnicode is proven wrong" (spec §2, D1): the
route never changes, only what the native engine produces.
```

4. Add after `ocr_unit`:

```python
def native_unit(
    number: int,
    text: str,
    *,
    reason: str,
    path: Path,
    suspect: bool,
    repair: Any | None,
) -> PageText:
    """One native page: passthrough, or read through its embedded font (native-3).

    `suspect` is the DOCUMENT's detection over its native pages
    (`fontrepair.detect`), passed in exactly as `document_legacy_ratio` reaches
    `convert_unit`: the cold path and `recovery_cache`'s refresh compute it
    from the same pages, so they cannot disagree.

    Never raises. A repair that did not pass its gate — or that raised — keeps
    the native text (rule 6) and says why in `detail["repair"]`.
    """
    if repair is None or not suspect:
        return PageText(number, ROUTE_NATIVE, reason, text)
    try:
        outcome = repair.repair_page(path, number, text)
    except Exception as exc:  # noqa: BLE001 - a repair bug must not lose the page
        logger.warning("NRB recovery: native repair of page %d raised (%s)", number, type(exc).__name__)
        outcome = fontrepair.RepairOutcome(
            fontrepair.unrepaired(fontrepair.ENGINE_ERROR), text, {"error": type(exc).__name__}
        )
    served = outcome.text if outcome.status == fontrepair.STATUS_REPAIRED else text
    detail = {
        "repair": outcome.status,
        "repair_engine": getattr(repair, "version", "unknown"),
        **outcome.detail,
    }
    return PageText(number, ROUTE_NATIVE, reason, served, detail=detail)
```

5. Give `_recover_pdf` a `repair` parameter and use it. Its signature gains `repair: Any | None = None`. Inside, after `warnings = list(plan.warnings)` and the provenance warning, compute the routes once, detect over the native pages, and execute:

```python
    routes = _routes(prov, plan.reason, pages)
    detection = fontrepair.detect(
        [text for text, (route, _) in zip(pages, routes) if route == ROUTE_NATIVE]
    )
    if repair is not None and detection.suspect:
        warnings.append(f"tounicode_suspected:{detection.density:g}")

    out: list[PageText] = []
    for index, (text, (route, why)) in enumerate(zip(pages, routes), start=1):
        if route == ROUTE_LEGACY:
            out.append(
                convert_unit(
                    index, text, reason=why, converter=converter, lexicon=lexicon,
                    document_legacy_ratio=plan.gate_ratio or 0.0,
                )
            )
        elif route == ROUTE_OCR:
            out.append(ocr_unit(index, path, reason=why, engine=ocr))
        else:
            out.append(native_unit(index, text, reason=why, path=path,
                                   suspect=detection.suspect, repair=repair))
    return tuple(out), tuple(warnings)
```

6. In `recover`, add `repair: Any | None = None` after `ocr`, and pass `repair=repair` to `_recover_pdf(...)`. In its `PLAN_NATIVE` PDF branch, replace the generator that builds `PageText(i, ROUTE_NATIVE, plan.reason, text)` with:

```python
        if page_texts is not None:
            detection = fontrepair.detect(page_texts)
            warnings = plan.warnings
            if repair is not None and detection.suspect:
                warnings = (*plan.warnings, f"tounicode_suspected:{detection.density:g}")
            return RecoveredDocument(
                result.family, plan.plan, plan.reason, plan.gate_ratio,
                tuple(
                    native_unit(i, text, reason=plan.reason, path=path,
                                suspect=detection.suspect, repair=repair)
                    for i, text in enumerate(page_texts, start=1)
                ),
                warnings,
            )
```

7. In `recover`'s docstring, add one sentence to the "Every dependency is injected" paragraph: `` `repair` (native-3) may be None too — then native pages are passthrough, byte-identical to before. ``

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_nrb_native_repair_recovery.py tests/test_nrb_recovery.py tests/test_nrb_rag_ingest.py -q`
Expected: all passed. `test_the_routing_functions_are_unchanged` still passes.

- [ ] **Step 5: Run the full suite (G11), then commit**

```bash
git add app/nrb/recovery.py tests/test_nrb_native_repair_recovery.py
git commit -m "feat(nrb): native_unit and rule 6, a failed repair keeps the native text

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: The version split in the cache, the dependency, chunk metadata, the worker log, the flag

**Files:**
- Modify: `app/nrb/recovery_cache.py`, `app/nrb/rag.py`, `app/rag/worker.py`, `app/config.py`, `.env.example`, `scripts/nrb_recovery_cache.py`
- Test: `tests/test_nrb_native_repair_cache.py`

**Interfaces:**
- Consumes: `recovery.native_unit`, `fontrepair.detect`, `fontrepair.FontRepairEngine`, `fontrepair.STATUS_REPAIRED`, `fontrepair.NOT_APPLICABLE`, `fontrepair.UNREPAIRED_PREFIX`.
- Produces:
  - `recovery_cache.engine_versions(*, converter, lexicon, ocr, extractor_version="native-2", repair=None)`, whose native string is `repair.version` when given, else `passthrough/<extractor_version>`;
  - `recovery_cache.resolve(..., repair=None)`;
  - `CacheReport.repaired_units` / `.unrepaired_units`;
  - a `stats()["native_repair"]` list;
  - `rag.nrb_dependencies() -> (converter, lexicon, ocr, repair)` and `rag._repair_dependency(settings)`;
  - `Settings.nrb_native_repair: bool = False`;
  - `rag._chunk_meta` keys `text_repair`, `repair_engine`, `authoritative`.

- [ ] **Step 1: Write the failing tests** — `tests/test_nrb_native_repair_cache.py`

```python
"""native-3 in the recovery cache, the dependency resolver and chunk metadata (spec §2, §3.2)."""

from __future__ import annotations

import pytest

from app.nrb import fontrepair, recovery, recovery_cache
from app.nrb import rag as nrb_rag
from app.nrb.recovery import PageText, ROUTE_NATIVE, ROUTE_OCR, RecoveredDocument
from app.nrb.recovery_cache import CachedRecovery, CachedUnit
from tests.test_nrb_native_repair_recovery import StubRepair
from tests.test_nrb_recovery_cache import CountingOcr, _engines

pytest.importorskip("uharfbuzz")
from tests import nrb_font_pdf as W  # noqa: E402

LOHIT = W.LOHIT.read_bytes()


def test_flag_off_keeps_todays_native_engine_string():
    assert _engines().native == "passthrough/native-2"
    assert recovery_cache.engine_versions(converter=None, lexicon=None, ocr=None,
                                          repair=None).native == "passthrough/native-2"


def test_a_repair_engine_names_the_native_engine():
    versions = recovery_cache.engine_versions(converter=None, lexicon=None, ocr=None,
                                              repair=StubRepair())
    assert versions.native == "native-3/stub"


def test_the_base_version_is_untouched():
    assert recovery_cache.base_version() == "native-2|recovery-1|prov-1|gate=0.8|unjudged=0.8"


def _units(engine_native: str, ocr_engine: str):
    return (
        CachedUnit(1, None, ROUTE_NATIVE, "clean", engine_native, True, "old", None, {}),
        CachedUnit(2, None, ROUTE_NATIVE, "clean", engine_native, True, "old", None, {}),
        CachedUnit(3, None, ROUTE_OCR, "no_font_scan_backed", ocr_engine, True, "ओसीआर", None,
                   {"authoritative": False}),
    )


def _cached(units):
    return CachedRecovery("a" * 64, recovery_cache.base_version(), "pdf", recovery.PLAN_PAGES,
                          "legacy_font_suspected", 1.0, (), units)


def test_turning_the_repair_on_re_runs_native_units_only(tmp_path):
    path = W.word_pdf(tmp_path, LOHIT, pages=3)
    ocr = CountingOcr()
    ocr_engine = _engines(ocr=ocr).ocr
    stub = StubRepair()
    doc, report = recovery_cache.resolve(path, cached=_cached(_units("passthrough/native-2", ocr_engine)),
                                         ocr=ocr, repair=stub, cold=None)
    assert report.outcome == "partial"
    assert (report.ocr_units, report.converter_units) == (0, 0)
    assert ocr.calls == 0
    assert stub.calls == [1, 2]
    assert doc.pages[2].text == "ओसीआर"
    assert report.repaired_units == 2


def test_cold_and_refresh_agree_on_whether_the_document_is_suspect(tmp_path):
    path = W.word_pdf(tmp_path, LOHIT, pages=2)
    result = recovery.extraction.extract_file(path, family="pdf", extension="pdf",
                                              extractor_version="native-2")
    cold = StubRepair()
    recovery.recover(path, result, repair=cold)
    warm = StubRepair()
    units = (CachedUnit(1, None, ROUTE_NATIVE, "clean", "passthrough/native-2", True, "x", None, {}),
             CachedUnit(2, None, ROUTE_NATIVE, "clean", "passthrough/native-2", True, "x", None, {}))
    cached = CachedRecovery("b" * 64, recovery_cache.base_version(), "pdf", recovery.PLAN_NATIVE,
                            "clean", None, (), units)
    recovery_cache.resolve(path, cached=cached, repair=warm, cold=None)
    assert cold.calls == warm.calls == [1, 2]


def test_a_non_pdf_native_document_is_never_repaired(tmp_path):
    """Review Focus 5: a .docx unit goes cold once, and the engine is not called."""
    stub = StubRepair()
    unit = CachedUnit(1, None, ROUTE_NATIVE, "clean", "passthrough/native-2", True, "पाठ", None, {})
    cached = CachedRecovery("c" * 64, recovery_cache.base_version(), "document",
                            recovery.PLAN_NATIVE, "clean", None, (), (unit,))
    fresh = RecoveredDocument("document", recovery.PLAN_NATIVE, "clean", None,
                              (PageText(1, ROUTE_NATIVE, "clean", "पाठ"),))
    doc, report = recovery_cache.resolve(tmp_path / "x.docx", cached=cached, repair=stub,
                                         cold=lambda: fresh)
    assert report.outcome == "cold" and report.reason == "non_pdf_engine_changed"
    assert stub.calls == [] and doc.pages[0].text == "पाठ"


def test_flag_on_without_the_shaper_is_a_named_unavailable_engine(tmp_path, monkeypatch):
    """Review Focus 4."""
    monkeypatch.setattr(fontrepair, "engine_available", lambda: False)
    engine = fontrepair.FontRepairEngine()
    assert recovery_cache.engine_versions(converter=None, lexicon=None, ocr=None,
                                          repair=engine).native == fontrepair.UNAVAILABLE
    path = W.word_pdf(tmp_path, LOHIT)
    result = recovery.extraction.extract_file(path, family="pdf", extension="pdf",
                                              extractor_version="native-2")
    page = recovery.recover(path, result, repair=engine).pages[0]
    assert page.detail["repair"] == fontrepair.unrepaired(fontrepair.SHAPER_UNAVAILABLE)


def test_the_repair_dependency_follows_the_flag():
    class Off:
        nrb_native_repair = False

    class On:
        nrb_native_repair = True

    assert nrb_rag._repair_dependency(Off()) is None
    assert isinstance(nrb_rag._repair_dependency(On()), fontrepair.FontRepairEngine)


def test_the_flag_defaults_off():
    from app.config import Settings

    assert Settings.model_fields["nrb_native_repair"].default is False


@pytest.mark.parametrize("status, expected", [
    (fontrepair.STATUS_REPAIRED, "font_tables"),
    ("unrepaired:coverage", "unrepaired"),
])
def test_a_detected_page_is_non_authoritative_in_its_chunks(status, expected):
    page = PageText(1, ROUTE_NATIVE, "clean", "पाठ",
                    detail={"repair": status, "repair_engine": "native-3/x"})
    meta = nrb_rag._chunk_meta(page, "native-2")
    assert meta["text_repair"] == expected
    assert meta["repair_engine"] == "native-3/x"
    assert meta["authoritative"] is False


def test_a_not_applicable_or_plain_native_page_carries_no_caveat():
    for detail in ({"repair": fontrepair.NOT_APPLICABLE}, {}):
        meta = nrb_rag._chunk_meta(PageText(1, ROUTE_NATIVE, "clean", "Annex", detail=detail), "native-2")
        assert "text_repair" not in meta and "authoritative" not in meta
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_nrb_native_repair_cache.py -v`
Expected: FAIL, with `TypeError: engine_versions() got an unexpected keyword argument 'repair'` and `AttributeError` for `_repair_dependency`.

- [ ] **Step 3: Edit `app/nrb/recovery_cache.py`**

1. Change the import `from . import legacy_convert, provenance, recovery` to `from . import fontrepair, legacy_convert, provenance, recovery`.
2. In the module docstring, change the `native` bullet of `engine_version` to:

```text
    native              `passthrough/<classifier>` while the native-3 repair is
                        off (today's string, so nothing goes stale), or the
                        repair engine's identity (`fontrepair.engine_version`:
                        `native-3/repair-N/D=…/N=…/lang=ne/hb-…`) when it is on.
                        The classifier version stays in the BASE: the repair
                        changes what the native route produces, never which
                        route a page takes (spec §2, D1).
```

3. Give `engine_versions` the `repair` parameter:

```python
def engine_versions(
    *,
    converter: LegacyFontConverter | None,
    lexicon: Lexicon | None,
    ocr: PageOcrEngine | None,
    extractor_version: str = "native-2",
    repair: Any | None = None,
) -> EngineVersions:
```
and set the native term:
```python
    # Off (repair is None): today's string exactly, so nothing goes stale.
    native = f"passthrough/{extractor_version}" if repair is None else str(repair.version)
    return EngineVersions(native=native, legacy_conversion=legacy, ocr=ocr_version)
```

4. Add two fields to `CacheReport`, `repaired_units: int = 0` and `unrepaired_units: int = 0`, and include both in `as_dict()`.
5. Add this helper above `resolve`:

```python
def _repair_counts(document: recovery.RecoveredDocument) -> tuple[int, int]:
    repaired = unrepaired = 0
    for page in document.pages:
        status = (page.detail or {}).get("repair")
        if status == fontrepair.STATUS_REPAIRED:
            repaired += 1
        elif isinstance(status, str) and status.startswith(fontrepair.UNREPAIRED_PREFIX):
            unrepaired += 1
    return repaired, unrepaired
```

6. In `resolve`:
   - add the parameter `repair: Any | None = None` after `ocr`;
   - pass `repair=repair` to `engine_versions(...)`;
   - in `run_cold`, before `return recovered, report`, add `report.repaired_units, report.unrepaired_units = _repair_counts(recovered)`;
   - in the warm branch, build `doc = cached.as_document()`, then set the counts from `_repair_counts(doc)` and `return doc, report`;
   - in the partial branch, after the PDF re-read and before the unit loop, add:

```python
    native_numbers = [u.unit_number for u in cached.units if u.route == recovery.ROUTE_NATIVE]
    suspect = fontrepair.detect(
        [page_texts[n - 1] for n in native_numbers if 0 <= n - 1 < len(page_texts)]
    ).suspect
```
   - pass `repair=repair, suspect=suspect` to `_refresh_unit(...)`;
   - before the final return, build the `RecoveredDocument` into a variable, set the counts from it, and return it.

7. In `_refresh_unit`, add the parameters `repair: Any | None = None, suspect: bool = False`, and replace the final native return with:

```python
    return recovery.native_unit(
        unit.unit_number, native_text, reason=unit.reason, path=path,
        suspect=suspect, repair=repair,
    )
```

8. In `stats()`, add a third query and return it as `"native_repair"`:

```python
    repairs = (
        await session.execute(
            select(
                NRBRecoveryUnit.detail["repair"].astext.label("repair"),
                func.count().label("units"),
            )
            .where(NRBRecoveryUnit.route == recovery.ROUTE_NATIVE)
            .group_by(NRBRecoveryUnit.detail["repair"].astext)
        )
    ).all()
```
```python
        "native_repair": [{"repair": r or "passthrough", "units": n} for r, n in repairs],
```

- [ ] **Step 4: Edit `app/nrb/rag.py`**

1. Add the dependency factory after `reset_dependencies`:

```python
def _repair_dependency(settings):
    """The native-3 repair engine, or None while `NRB_NATIVE_REPAIR` is off.

    Off means the native engine string stays `passthrough/native-2` and no
    cached unit goes stale — the code can ship before the reader's gate passes
    (plan Task 18 flips the default). On without uharfbuzz is NOT None: the
    engine reports `native-3/repair-unavailable` and detected pages are
    recorded unrepaired, so installing it later invalidates exactly those.
    """
    if not getattr(settings, "nrb_native_repair", False):
        return None
    from .fontrepair import FontRepairEngine

    return FontRepairEngine()
```

2. `nrb_dependencies()` returns four values. Change its docstring's first line to `` `(converter, lexicon, ocr_engine, repair)`, built once per PROCESS. ``, and its final `return converter, lexicon, ocr_engine` to:

```python
    from ..config import get_settings

    return converter, lexicon, ocr_engine, _repair_dependency(get_settings())
```

3. In `resolve_dependencies`, add `"repair": injected.get("repair"),` to the injected branch, and replace its last two lines with:

```python
    converter, lexicon, ocr, repair = nrb_dependencies()
    return {"converter": converter, "lexicon": lexicon, "ocr": ocr, "repair": repair}
```

4. In `_chunk_meta`, add after the `elif page.route == recovery.ROUTE_OCR:` block, before `return`:

```python
    repair = detail.get("repair")
    if page.route == recovery.ROUTE_NATIVE and repair and repair != fontrepair.NOT_APPLICABLE:
        # Detected as garbled by native-3 (spec §5.1). Repaired text is
        # machine-recovered and unreviewed; unrepaired text is known garbled.
        # Either way every reader shows the VERIFY caveat, keyed on this flag.
        meta["text_repair"] = "font_tables" if repair == fontrepair.STATUS_REPAIRED else "unrepaired"
        meta["repair_engine"] = detail.get("repair_engine")
        meta["authoritative"] = False
```
and add `fontrepair` to the module's `from . import extraction, recovery, sniff` line.

- [ ] **Step 5: Update the other three call sites**

- `scripts/nrb_recovery_cache.py`, in `do_reuse_check`: change `converter, lexicon, ocr = nrb_rag.nrb_dependencies()` to `converter, lexicon, ocr, repair = nrb_rag.nrb_dependencies()`. Add `print(f"  repair     {getattr(repair, 'version', 'off')}")` after the `ocr` print, and add `"repair": repair,` to the `injected` dict.
- `app/rag/worker.py`, in `_load_chunks`: replace the `log.info(...)` call with:

```python
        log.info(
            "nrb recovery %s for %s: %d units (%d reused, %d recovered; "
            "converter %d, ocr %d; repaired %d, unrepaired %d)",
            report.outcome, snap.id, report.units_total, report.units_reused,
            report.units_recovered, report.converter_units, report.ocr_units,
            report.repaired_units, report.unrepaired_units,
        )
```
- `app/config.py`: after `nrb_files_dir: str = "nrb_files"`, add:

```python
    # native-3: read garbled Word text layers through their embedded font
    # (docs/superpowers/specs/2026-10-02-native3-font-repair-design.md). OFF
    # until the Nepali reader's gate passes: off, the native engine string stays
    # `passthrough/native-2` and no cached recovery goes stale. On needs
    # uharfbuzz in the worker (requirements-worker.txt).
    nrb_native_repair: bool = False
```
- `.env.example`: after `NRB_FILES_DIR=nrb_files`, add:

```text

# native-3 font repair of garbled Word text layers (worker only; needs uharfbuzz).
# Off until the reader's gate passes; on, only NATIVE recovery units re-run.
NRB_NATIVE_REPAIR=false
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_nrb_native_repair_cache.py tests/test_nrb_recovery_cache.py tests/test_nrb_rag_ingest.py tests/test_nrb_native_repair_recovery.py -q`
Expected: all passed.

- [ ] **Step 7: Run the full suite (G11), then commit**

```bash
git add app/nrb/recovery_cache.py app/nrb/rag.py app/rag/worker.py app/config.py .env.example \
        scripts/nrb_recovery_cache.py tests/test_nrb_native_repair_cache.py
git commit -m "feat(nrb): native-3 as the native route's engine, behind NRB_NATIVE_REPAIR

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: One caveat predicate for all three readers

**Files:**
- Modify: `app/rag/sources.py`, `app/tools/local/search_department_docs.py`, `app/tools/local/read_department_doc.py`
- Test: `tests/test_search_department_docs.py`, `tests/test_read_department_doc_tool.py`

**Interfaces:**
- Produces: `sources.is_machine_recovered(route: str | None, authoritative: bool | None) -> bool`. It is True when `route in RECOVERED_ROUTES` or `authoritative is False`.
- `read_department_doc._fetch_document` returns `(info, chunks, trust)`, where `trust` is `list[tuple[str, bool | None]]` of `(route, authoritative)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_search_department_docs.py`:
```python
def test_all_three_readers_share_one_caveat_predicate():
    """The model's context, the citation and the whole-document read must decide
    the caveat the same way (spec §5.2): read_department_doc used to check the
    route only, and would have handed the model a garbled Act uncaveated."""
    from app.rag import sources as rag_sources
    from app.tools.local import read_department_doc, search_department_docs as tool

    assert tool._is_machine_recovered is rag_sources.is_machine_recovered
    assert read_department_doc.is_machine_recovered is rag_sources.is_machine_recovered
    assert rag_sources.is_machine_recovered("native", False) is True
    assert rag_sources.is_machine_recovered("ocr", None) is True
    assert rag_sources.is_machine_recovered("native", None) is False
```

In `tests/test_read_department_doc_tool.py`, change `_call`'s `_fetch` so that a plain route string still works:
```python
    async def _fetch(document_id, department_id):
        if doc is None:
            return None
        trust = [r if isinstance(r, tuple) else (r, None) for r in (routes or [])]
        return doc, (chunks if chunks is not None else []), trust
```
and append:
```python
def test_a_repaired_or_garbled_native_page_carries_the_caveat(monkeypatch):
    """native-3 marks detected native pages `authoritative: false` (spec §5.1)."""
    from app.rag.sources import VERIFY_NOTE

    out = _call(
        {"document_id": "d1"},
        doc=_Doc(origin="nrb"),
        chunks=_chunks("उपभोक्ता संरक्षण ऐन"),
        routes=[("native", False)],
        monkeypatch=monkeypatch,
    )
    assert VERIFY_NOTE in out
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_search_department_docs.py tests/test_read_department_doc_tool.py -q`
Expected: FAIL, with `AttributeError: … has no attribute 'is_machine_recovered'`, and the new read test fails because it lacks the caveat.

- [ ] **Step 3: Implement**

- `app/rag/sources.py`, after `VERIFY_NOTE`:

```python
def is_machine_recovered(route: str | None, authoritative: bool | None) -> bool:
    """Does this chunk's text need the VERIFY caveat? THE one predicate.

    Three readers ask it — the model's context (`search_department_docs`), the
    citation (`verify_note`, below) and the whole-document read
    (`read_department_doc`) — and they must never answer differently, or the
    reader sees a badge that contradicts the answer. A recovered route, or a
    chunk explicitly marked non-authoritative (OCR; native-3's detected pages).
    """
    return route in RECOVERED_ROUTES or authoritative is False
```
  and in `_group`'s NRB block change `if chunk.route in RECOVERED_ROUTES or chunk.authoritative is False:` to `if is_machine_recovered(chunk.route, chunk.authoritative):`.
- `app/tools/local/search_department_docs.py`: add `is_machine_recovered as _is_machine_recovered,` to the import block that brings in `RECOVERED_ROUTES as _RECOVERED_ROUTES`, and change line 88 to `recovered = _is_machine_recovered(route, cm.get("authoritative"))`.
- `app/tools/local/read_department_doc.py`:
  - change the import to `from ...rag.sources import VERIFY_NOTE, is_machine_recovered`;
  - in `_fetch_document`, replace `routes = [str((r.meta or {}).get("route") or "") for r in rows]` with:

```python
    trust = [
        (str((r.meta or {}).get("route") or ""), (r.meta or {}).get("authoritative"))
        for r in rows
    ]
```
    and return `info, chunks, trust`; update its docstring's first line to `(document, chunks, trust)`;
  - in `_read_department_doc`, change `doc, chunks, routes = found` to `doc, chunks, trust = found`, and `recovered = any(r in RECOVERED_ROUTES for r in routes)` to `recovered = any(is_machine_recovered(r, a) for r, a in trust)`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_search_department_docs.py tests/test_read_department_doc_tool.py tests/test_ocr_api_boundaries.py -q`
Expected: all passed.

- [ ] **Step 5: Run the full suite (G11), then commit**

```bash
git add app/rag/sources.py app/tools/local/search_department_docs.py app/tools/local/read_department_doc.py \
        tests/test_search_department_docs.py tests/test_read_department_doc_tool.py
git commit -m "fix(rag): one VERIFY predicate for search, citations and whole-document reads

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: `native-3` evidence in `extraction`, and the import boundary

**Files:**
- Modify: `app/nrb/extraction.py`
- Test: `tests/test_nrb_native3_extraction.py`, `tests/test_nrb_repair_import_boundary.py`

**Interfaces:**
- Produces: `extraction.SUPPORTED_EXTRACTOR_VERSIONS` includes `"native-3"`. A native-3 PDF result has native-2's status, reason and every native-2 metric, plus `metrics["native3"] = fontrepair.document_evidence(...)`.

- [ ] **Step 1: Write the failing tests**

`tests/test_nrb_native3_extraction.py`:
```python
"""native-3 extraction rows: native-2's verdict, plus the repair's evidence (spec §3.2)."""

from __future__ import annotations

import pytest

pytest.importorskip("uharfbuzz")

from app.nrb import extraction  # noqa: E402
from tests import nrb_font_pdf as W  # noqa: E402


def test_native3_classifies_exactly_as_native2_and_adds_only_evidence(tmp_path):
    path = W.word_pdf(tmp_path, W.LOHIT.read_bytes())
    v2 = extraction.extract_file(path, family="pdf", extension="pdf", extractor_version="native-2")
    v3 = extraction.extract_file(path, family="pdf", extension="pdf", extractor_version="native-3")
    assert (v3.status, v3.reason, v3.text) == (v2.status, v2.reason, v2.text)
    for key, value in v2.metrics.items():
        if key != "duration_ms":
            assert v3.metrics[key] == value, key
    evidence = v3.metrics["native3"]
    assert evidence["detection"]["suspect"] is True
    assert evidence["statuses"] == {"font_tables": 1}


def test_native3_is_a_supported_version():
    assert "native-3" in extraction.SUPPORTED_EXTRACTOR_VERSIONS
```

`tests/test_nrb_repair_import_boundary.py`:
```python
"""fontTools and uharfbuzz must never load because the API (or recovery) did.

Subprocess on purpose: `sys.modules` is process-global and other tests import
both libraries. Same reasoning as tests/test_image_ocr_import_boundary.py.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

FORBIDDEN = ("fontTools", "uharfbuzz")


def _probe(import_line: str) -> str:
    code = (f"import sys; {import_line};"
            f"bad = [m for m in {FORBIDDEN!r} if m in sys.modules]; print(','.join(bad))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    return out.stdout.strip()


@pytest.mark.parametrize("module", [
    "app.main", "app.nrb.recovery", "app.nrb.rag", "app.nrb.fontrepair",
    "app.nrb.glyphtable", "app.nrb.shaping", "app.nrb.extraction",
])
def test_importing_does_not_load_the_repair_stack(module):
    assert _probe(f"import {module}") == ""
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_nrb_native3_extraction.py tests/test_nrb_repair_import_boundary.py -v`
Expected: the extraction tests FAIL (`KeyError: 'native3'` / the assert on the supported list). The boundary tests should already PASS; if one fails, a module imports fontTools/uharfbuzz at module scope, so fix that before going on.

- [ ] **Step 3: Implement in `app/nrb/extraction.py`**

1. `SUPPORTED_EXTRACTOR_VERSIONS = (EXTRACTOR_VERSION, "native-2", "native-3")`, with a comment above it:

```python
# native-3 is native-2's CLASSIFIER, unchanged, plus EVIDENCE of what the
# native route's font repair would do (`metrics["native3"]`). The verdict is
# computed on the UNREPAIRED text, so a native-3 row's status/reason/metrics
# equal native-2's (spec §2's condition); the repair itself lives in the native
# ENGINE (`recovery.native_unit`), never in extraction.
```

2. In `_extract_pdf`, after building the result, add the evidence for native-3:

```python
def _extract_pdf(
    path: Path, family: str, started: float,
    extractor_version: str = EXTRACTOR_VERSION,
) -> ExtractionResult:
    read = file_documents.read_pdf_pages(path)
    result = result_from_pages(
        read.pages,
        parser="pypdf",
        family=family,
        started=started,
        extra_metrics={"pages_skipped": read.skipped},
        extractor_version=extractor_version,
    )
    if extractor_version != "native-3":
        return result
    # Imported here, not at module scope: recovery imports extraction, and the
    # evidence is the only reason extraction ever looks the other way.
    from . import fontrepair, recovery

    plan = recovery.plan_document(family=result.family, status=result.status,
                                  reason=result.reason, metrics=result.metrics)
    routes = recovery.page_routes(path, plan, read.pages)
    native = [n for n, (route, _) in enumerate(routes, start=1) if route == recovery.ROUTE_NATIVE]
    evidence = fontrepair.document_evidence(fontrepair.FontRepairEngine(), path, read.pages, native)
    return replace(result, metrics={**result.metrics, "native3": evidence})
```
and add `from dataclasses import dataclass, replace` (extending the existing `dataclass` import).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_nrb_native3_extraction.py tests/test_nrb_repair_import_boundary.py tests/test_nrb_extract_pass.py -q`
Expected: all passed.

- [ ] **Step 5: Run the full suite (G11), then commit**

```bash
git add app/nrb/extraction.py tests/test_nrb_native3_extraction.py tests/test_nrb_repair_import_boundary.py
git commit -m "feat(nrb): native-3 extraction evidence; the repair stack stays out of the API

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

# Milestone 3 — the cohort and the reader

### Task 10: Draw and freeze the new cohort

**Files:**
- Create: `scripts/nrb_native3_cohort.py`
- Modify: `tests/test_nrb_dbguard.py` (add it to `EVIDENCE`)
- Test: `tests/test_nrb_native3_cohort.py`

**Interfaces:**
- Consumes: `sampling.build_candidates`, `sampling.rank_for`, `manifest.Manifest`, `manifest.MANIFEST_VERSION`, `manifest.compute_selection_sha256`, `manifest.write_new_manifest`, `catalog.load_sample_rows`, and `fontrepair.DETECT_DENSITY`/`DETECT_MIN_DEVANAGARI`.
- Produces: `docs/nrb/native3-cohort.json` (manifest-2, readable by `nrb_fetch.py --manifest` and `nrb_extract.py --manifest`). With `--top-up` it produces `docs/nrb/native3-cohort-topup.json`. Pure helpers: `spent_keys(paths)`, `stratum_of(candidate)`, `draw(candidates, *, excluded, seed, sizes, offset)`, `build(...)`.

- [ ] **Step 1: Write the failing test** — `tests/test_nrb_native3_cohort.py`

```python
"""The native-3 cohort: drawn from metadata alone, every spent key withheld (spec §6.2)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import nrb_native3_cohort as C  # noqa: E402

from app.nrb import manifest, sampling  # noqa: E402


def _cand(key, doc_type="act", year=2020, resource="pdf"):
    return sampling.Candidate(key, year, sampling.year_cohort(year), doc_type, resource,
                              "lgd", ("lgd",), 1)


def test_every_spent_set_is_withheld():
    keys = C.spent_keys([REPO / p for p in C.SPENT_SETS])
    for path in C.SPENT_SETS:
        for entry in json.loads((REPO / path).read_text())["entries"]:
            assert entry["comparison_key"] in keys


def test_the_strata_are_metadata_only():
    assert C.stratum_of(_cand("a", "act", 2015)) == "enriched"
    assert C.stratum_of(_cand("a", "directive", 2023)) == "enriched"
    assert C.stratum_of(_cand("a", "act", 2014)) == "random"
    assert C.stratum_of(_cand("a", "circular", 2023)) == "random"
    assert C.stratum_of(_cand("a", "act", None)) == "random"


def test_the_draw_is_pdf_only_excludes_spent_keys_and_is_deterministic():
    pool = [_cand(f"k{i}", "act" if i % 2 else "circular") for i in range(40)]
    pool.append(_cand("xls", resource="spreadsheet"))
    first = C.draw(pool, excluded=frozenset({"k1"}), seed="s", sizes={"enriched": 5, "random": 5})
    again = C.draw(list(reversed(pool)), excluded=frozenset({"k1"}), seed="s",
                   sizes={"enriched": 5, "random": 5})
    keys = [c.comparison_key for _, c in first]
    assert keys == [c.comparison_key for _, c in again]
    assert "k1" not in keys and "xls" not in keys
    assert [s for s, _ in first].count("enriched") == 5


def test_the_top_up_takes_the_next_ranks_of_the_same_stratum():
    pool = [_cand(f"k{i}") for i in range(20)]
    first = C.draw(pool, excluded=frozenset(), seed="s", sizes={"enriched": 5, "random": 0})
    more = C.draw(pool, excluded=frozenset(), seed="s", sizes={"enriched": 5, "random": 0}, offset=5)
    assert not {c.comparison_key for _, c in first} & {c.comparison_key for _, c in more}


def test_the_manifest_binds_the_detector_and_verifies(tmp_path):
    pool = [_cand(f"k{i}") for i in range(12)]
    built = C.build(pool, excluded=frozenset({"zz"}), seed="s",
                    sizes={"enriched": 4, "random": 4}, offset=0, drawn_at="2026-10-02T00:00:00+05:45",
                    catalog_counts={}, excluded_sets=list(C.SPENT_SETS))
    assert built.sampler["detector"] == {"density": 0.001, "min_devanagari": 500}
    assert manifest.verify_manifest(built).ok
    out = tmp_path / "m.json"
    manifest.write_new_manifest(built, out)
    assert manifest.read_manifest(out).keys() == built.keys()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_nrb_native3_cohort.py -v`
Expected: ERROR, `ModuleNotFoundError: No module named 'nrb_native3_cohort'`.

- [ ] **Step 3: Write `scripts/nrb_native3_cohort.py`**

```python
#!/usr/bin/env python
"""Draw and freeze the native-3 cohort (spec §6.2). EVIDENCE: local_ai_gateway_p4 only.

    DATABASE_URL=postgresql+asyncpg://gateway:***@127.0.0.1:5432/local_ai_gateway_p4 \\
        .venv/bin/python scripts/nrb_native3_cohort.py --out docs/nrb/native3-cohort.json
    # pre-registered top-up, only if the evidence pass says so (Task 12):
    ... scripts/nrb_native3_cohort.py --top-up --out docs/nrb/native3-cohort-topup.json

Drawn BEFORE any network access, from catalog METADATA alone, so nothing
native-3 computes can shape it. Every spent set is withheld before
stratification and the withheld set is bound by hash. The detector constants
are written INTO the frozen manifest: that is the record that D and N were
fixed before the cohort was drawn.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import localtime  # noqa: E402
from app.nrb import dbguard, fontrepair, manifest, sampling  # noqa: E402

ALGORITHM = "nrb-native3-v1"
SEED = "native3-2026-10-02"
ENRICHED_SECTIONS = frozenset({"act", "rule_bylaw", "directive"})
ENRICHED_SINCE = 2015
SIZES = {"enriched": 250, "random": 250}
TOP_UP = {"threshold_detected": 20, "size": 250, "stratum": "enriched"}
SPENT_SETS = (
    "docs/nrb/phase6a-manifest.json",
    "docs/nrb/phase6b-routing-holdout.json",
    "docs/nrb/phase7-validation-cohort.json",
    "docs/nrb/prod-corpus-scope.json",
)


def spent_keys(paths) -> frozenset[str]:
    keys: set[str] = set()
    for path in paths:
        for entry in json.loads(Path(path).read_text(encoding="utf-8"))["entries"]:
            keys.add(entry["comparison_key"])
    return frozenset(keys)


def stratum_of(c: sampling.Candidate) -> str:
    if c.document_type in ENRICHED_SECTIONS and c.year is not None and c.year >= ENRICHED_SINCE:
        return "enriched"
    return "random"


def draw(candidates, *, excluded: frozenset[str], seed: str, sizes: dict[str, int], offset: int = 0):
    pool = [c for c in candidates if c.resource_type == "pdf" and c.comparison_key not in excluded]
    out = []
    for stratum in ("enriched", "random"):
        members = sorted(
            (c for c in pool if stratum_of(c) == stratum),
            key=lambda c: sampling.rank_for(ALGORITHM, seed, c.comparison_key),
        )
        out.extend((stratum, c) for c in members[offset:offset + sizes.get(stratum, 0)])
    return out


def build(candidates, *, excluded, seed, sizes, offset, drawn_at, catalog_counts, excluded_sets):
    drawn = draw(candidates, excluded=excluded, seed=seed, sizes=sizes, offset=offset)
    entries = tuple({**c.as_entry(), "sampling_stratum": stratum} for stratum, c in drawn)
    keys = [e["comparison_key"] for e in entries]
    parameters = {
        "algorithm_version": ALGORITHM,
        "seed": seed,
        "frame": "nrb_files.resource_type = 'pdf' (the REST claim)",
        "enriched_sections": sorted(ENRICHED_SECTIONS),
        "enriched_since": ENRICHED_SINCE,
        "sizes": dict(sizes),
        "offset": offset,
        "top_up_rule": TOP_UP,
        "exclude_keys_sha256": hashlib.sha256("\n".join(sorted(excluded)).encode()).hexdigest(),
        "excluded_sets": list(excluded_sets),
        "detector": {"density": fontrepair.DETECT_DENSITY,
                     "min_devanagari": fontrepair.DETECT_MIN_DEVANAGARI},
    }
    requested = sum(sizes.values())
    strata = tuple(
        {"stratum": s, "selected": sum(1 for x, _ in drawn if x == s), "requested": sizes.get(s, 0)}
        for s in ("enriched", "random")
    )
    return manifest.Manifest(
        version=manifest.MANIFEST_VERSION,
        drawn_at=drawn_at,
        requested=requested,
        shortfall=requested - len(keys),
        sampler=parameters,
        catalog_counts=dict(catalog_counts),
        strata=strata,
        notes=("native-3 cohort; population claims from the random stratum only (spec §6.2)",),
        entries=entries,
        algorithm_version=ALGORITHM,
        seed=seed,
        selected=len(keys),
        selection_sha256=manifest.compute_selection_sha256(
            manifest_version=manifest.MANIFEST_VERSION, algorithm_version=ALGORITHM,
            seed=seed, parameters=parameters, keys=keys),
        provenance={"excluded_manifests": list(excluded_sets)},
    )


async def _catalog():
    from app.db.session import SessionLocal
    from app.nrb import catalog

    async with SessionLocal() as session:
        rows = await catalog.load_sample_rows(session)
        counts = await catalog.catalog_counts(session)
    return rows, counts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--top-up", action="store_true", help="the pre-registered next 250 enriched ranks")
    args = ap.parse_args()
    try:
        name = dbguard.require(os.environ.get("DATABASE_URL", ""), dbguard.EVIDENCE_DATABASES)
    except dbguard.RefusedDatabase as exc:
        print(f"refusing to run: {exc}", file=sys.stderr)
        return 2
    print(f"database: {name}")
    rows, counts = asyncio.run(_catalog())
    repo = Path(__file__).resolve().parents[1]
    excluded = spent_keys([repo / p for p in SPENT_SETS])
    sizes = {"enriched": TOP_UP["size"], "random": 0} if args.top_up else SIZES
    offset = SIZES["enriched"] if args.top_up else 0
    built = build(sampling.build_candidates(rows), excluded=excluded, seed=SEED, sizes=sizes,
                  offset=offset, drawn_at=localtime.now().isoformat(timespec="seconds"),
                  catalog_counts=counts, excluded_sets=list(SPENT_SETS))
    manifest.write_new_manifest(built, args.out)
    print(json.dumps({"selected": built.selected, "shortfall": built.shortfall,
                      "strata": list(built.strata), "excluded": len(excluded)}, indent=2))
    print(f"selection_sha256 {built.selection_sha256}\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Add it to the guard test's `EVIDENCE` list**

In `tests/test_nrb_dbguard.py`, add `"nrb_native3_cohort.py",` to the `EVIDENCE` list.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_nrb_native3_cohort.py tests/test_nrb_dbguard.py -q`
Expected: all passed.

- [ ] **Step 6: Run the full suite (G11), then commit the script (not the manifest yet)**

```bash
git add scripts/nrb_native3_cohort.py tests/test_nrb_native3_cohort.py tests/test_nrb_dbguard.py
git commit -m "feat(nrb): the native-3 cohort draw, metadata-only, every spent set withheld

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: Freeze the cohort, then fetch and extract it (operational)

**Files:**
- Create: `docs/nrb/native3-cohort.json` (committed BEFORE the fetch)

- [ ] **Step 1: Draw the cohort (no network)**

```bash
P4_URL="$(.venv/bin/python -c "from app.config import get_settings; u=get_settings().database_url; print(u.rsplit('/',1)[0]+'/local_ai_gateway_p4')")"
DATABASE_URL="$P4_URL" .venv/bin/python scripts/nrb_native3_cohort.py --out docs/nrb/native3-cohort.json
```
Expected: `database: local_ai_gateway_p4`, `"selected": 500` (or a stated shortfall), and a `selection_sha256 …` line. This reads the catalog only, which takes seconds and needs no unit.

- [ ] **Step 2: Commit the frozen manifest BEFORE any network access**

The full suite is unaffected by a data file, but G11 still applies: run it, then:
```bash
git add docs/nrb/native3-cohort.json
git commit -m "docs(nrb): freeze the native-3 cohort before fetching (selection_sha256 in file)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 3: Fetch it as a unit (G10)**

```bash
systemd-run --user --wait --collect --unit=native3-fetch --working-directory="$PWD" -E DATABASE_URL="$P4_URL" \
  -p StandardOutput=file:"$SCRATCH/native3-fetch.log" -p StandardError=file:"$SCRATCH/native3-fetch.log" \
  .venv/bin/python scripts/nrb_fetch.py --manifest docs/nrb/native3-cohort.json -v
tail -20 "$SCRATCH/native3-fetch.log"
```
Expected: a summary with fetched, failed and HTTP-404 counts. Honest 404s stay in the denominator (§15's convention).

- [ ] **Step 4: Extract at native-2 and at native-3 as units (G10)**

```bash
for V in native-2 native-3; do
  systemd-run --user --wait --collect --unit="native3-extract-$V" --working-directory="$PWD" -E DATABASE_URL="$P4_URL" \
    -p StandardOutput=file:"$SCRATCH/extract-$V.log" -p StandardError=file:"$SCRATCH/extract-$V.log" \
    .venv/bin/python scripts/nrb_extract.py --manifest docs/nrb/native3-cohort.json --extractor-version "$V" -v
  tail -8 "$SCRATCH/extract-$V.log"
done
```
Expected: two summaries with equal blob counts. A second invocation selects 0, because extraction is idempotent per `(content_sha256, extractor_version)`.

---

### Task 12: The cohort evidence script

**Files:**
- Create: `scripts/nrb_native3_evidence.py`
- Modify: `tests/test_nrb_dbguard.py` (`EVIDENCE`)
- Test: `tests/test_nrb_native3_evidence.py`
- Create: `docs/nrb/native3-cohort-evidence.json`, `docs/nrb/native3-cohort-evidence.txt`

**Interfaces:**
- Consumes: `manifest.read_manifest`, `recovery.recover`, `recovery.plan_document`, `recovery.page_routes`, `fontrepair.FontRepairEngine`, `fontrepair.fonts_report`, `extraction.extract_file`, `sniff`, `filestore`.
- Produces:
  - pure helpers `classification_equal(v2_row, v3_row) -> bool`, `crosstab(records) -> dict`, `wilson(k, n) -> tuple[float, float]`;
  - an evidence JSON whose `pages` list (sha, page, status, font_identities, producer, stratum) feeds Task 13.

- [ ] **Step 1: Write the failing test** — `tests/test_nrb_native3_evidence.py`

```python
"""The native-3 cohort measurements' pure half (spec §6.3)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import nrb_native3_evidence as E  # noqa: E402


def test_classification_is_equal_only_when_every_native2_field_is():
    v2 = {"status": "extracted", "reason": "clean", "metrics": {"a": 1, "b": 2}}
    v3 = {"status": "extracted", "reason": "clean", "metrics": {"a": 1, "b": 2, "native3": {}}}
    assert E.classification_equal(v2, v3)
    assert not E.classification_equal(v2, {**v3, "metrics": {"a": 1, "b": 3, "native3": {}}})
    assert not E.classification_equal(v2, {**v3, "reason": "legacy_font_suspected"})


def test_the_crosstab_splits_false_negative_and_false_positive_candidates():
    records = [
        {"sha": "a", "detected": True, "contradicts": True},
        {"sha": "b", "detected": False, "contradicts": True},
        {"sha": "c", "detected": True, "contradicts": False},
        {"sha": "d", "detected": False, "contradicts": False},
    ]
    out = E.crosstab(records)
    assert out["both"] == 1 and out["neither"] == 1
    assert out["false_negative_candidates"] == ["b"]
    assert out["false_positive_candidates"] == ["c"]


def test_wilson_interval_brackets_the_rate():
    lo, hi = E.wilson(5, 100)
    assert 0 < lo < 0.05 < hi < 0.15
    assert E.wilson(0, 0) == (0.0, 0.0)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_nrb_native3_evidence.py -v`
Expected: ERROR, module not found.

- [ ] **Step 3: Write `scripts/nrb_native3_evidence.py`**

```python
#!/usr/bin/env python
"""The native-3 cohort measurements (spec §6.3). EVIDENCE: local_ai_gateway_p4 only.

    DATABASE_URL=postgresql+asyncpg://gateway:***@127.0.0.1:5432/local_ai_gateway_p4 \\
        .venv/bin/python scripts/nrb_native3_evidence.py --manifest docs/nrb/native3-cohort.json \\
        [--manifest docs/nrb/native3-cohort-topup.json] \\
        --out-json docs/nrb/native3-cohort-evidence.json --out-txt docs/nrb/native3-cohort-evidence.txt

Measures, over every fetched PDF in the frozen cohort:
  * non-regression — every UNDETECTED document's native pages come back
    byte-identical with the repair engine on (required: 100%);
  * classifier invariance — each native-3 extraction row equals its native-2
    row on status, reason and every native-2 metric (required: 100%);
  * the detector against font contradiction, as two independent signals;
  * the gate: page statuses and run agreement by font identity x producer.
Exits 1 when either required property fails. Recovery runs with no converter
and no OCR: only native pages are compared, and both arms fail the other routes
identically.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.nrb import dbguard, extraction, filestore, fontrepair, manifest, recovery, sniff  # noqa: E402

_ROWS = text("""
    SELECT f.comparison_key, f.fetch_status, f.content_sha256, f.extension,
           e2.status AS s2, e2.reason AS r2, e2.metrics AS m2,
           e3.status AS s3, e3.reason AS r3, e3.metrics AS m3
      FROM nrb_files f
      LEFT JOIN nrb_extractions e2 ON e2.content_sha256 = f.content_sha256 AND e2.extractor_version = 'native-2'
      LEFT JOIN nrb_extractions e3 ON e3.content_sha256 = f.content_sha256 AND e3.extractor_version = 'native-3'
     WHERE f.comparison_key = ANY(:keys)
""")


def classification_equal(v2: dict, v3: dict) -> bool:
    if (v2["status"], v2["reason"]) != (v3["status"], v3["reason"]):
        return False
    m2, m3 = v2["metrics"] or {}, v3["metrics"] or {}
    return all(m3.get(k) == v for k, v in m2.items() if k != "duration_ms")


def crosstab(records: list[dict]) -> dict:
    cell = Counter((r["detected"], r["contradicts"]) for r in records)
    return {
        "both": cell[(True, True)], "neither": cell[(False, False)],
        "detected_only": cell[(True, False)], "contradiction_only": cell[(False, True)],
        "false_negative_candidates": sorted(r["sha"] for r in records if r["contradicts"] and not r["detected"]),
        "false_positive_candidates": sorted(r["sha"] for r in records if r["detected"] and not r["contradicts"]),
    }


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (round(centre - half, 4), round(centre + half, 4))


def _producer(path: Path) -> str:
    try:
        from pypdf import PdfReader

        meta = PdfReader(str(path)).metadata or {}
        return str(meta.get("/Producer") or meta.get("/Creator") or "")
    except Exception:  # noqa: BLE001
        return ""


def _measure(path: Path, engine: fontrepair.FontRepairEngine) -> dict:
    result = extraction.extract_file(path, family="pdf", extension="pdf", extractor_version="native-2")
    off = recovery.recover(path, result)
    on = recovery.recover(path, result, repair=engine)
    native_off = [p for p in off.pages if p.route == recovery.ROUTE_NATIVE]
    native_on = [p for p in on.pages if p.route == recovery.ROUTE_NATIVE]
    detected = any(w.startswith("tounicode_suspected") for w in on.warnings)
    statuses = Counter(p.detail.get("repair", "passthrough") for p in native_on)
    return {
        "detected": detected,
        "identical": [p.text for p in native_off] == [p.text for p in native_on],
        "statuses": dict(statuses),
        "runs": sum(p.detail.get("runs", 0) for p in native_on),
        "runs_matched": sum(p.detail.get("runs_matched", 0) for p in native_on),
        "pages": [{"page": p.page_number, "status": p.detail.get("repair"),
                   "font_identities": p.detail.get("font_identities", []),
                   "illegal_before": p.detail.get("illegal_before"),
                   "illegal_after": p.detail.get("illegal_after")}
                  for p in native_on if p.detail.get("repair")],
    }


async def _rows(url: str, keys: list[str]) -> list[dict]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return [dict(r._mapping) for r in await conn.execute(_ROWS, {"keys": keys})]
    finally:
        await engine.dispose()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--manifest", action="append", required=True)
    ap.add_argument("--out-json", required=True)
    ap.add_argument("--out-txt", required=True)
    args = ap.parse_args()
    url = os.environ.get("DATABASE_URL", "")
    try:
        name = dbguard.require(url, dbguard.EVIDENCE_DATABASES)
    except dbguard.RefusedDatabase as exc:
        print(f"refusing to run: {exc}", file=sys.stderr)
        return 2
    print(f"database: {name}")

    stratum: dict[str, str] = {}
    for path in args.manifest:
        for entry in manifest.read_manifest(path).entries:
            stratum[entry["comparison_key"]] = entry["sampling_stratum"]
    rows = asyncio.run(_rows(url, list(stratum)))
    engine = fontrepair.FontRepairEngine()
    records, pages_out, invariance_failures = [], [], []
    for row in rows:
        sha = row["content_sha256"]
        base = {"key": row["comparison_key"], "stratum": stratum[row["comparison_key"]],
                "fetch_status": row["fetch_status"]}
        if row["fetch_status"] != "fetched" or not sha:
            records.append({**base, "parsed": False})
            continue
        path = filestore.resolve_path(filestore.storage_key_for(sha, row["extension"]))
        with path.open("rb") as handle:
            family = sniff.family_for(sniff.sniff(handle.read(4096))[0])
        if family != "pdf":
            records.append({**base, "sha": sha[:12], "parsed": False, "family": family})
            continue
        if row["s3"] is not None and not classification_equal(
                {"status": row["s2"], "reason": row["r2"], "metrics": row["m2"]},
                {"status": row["s3"], "reason": row["r3"], "metrics": row["m3"]}):
            invariance_failures.append(sha[:12])
        measured = _measure(path, engine)
        report = fontrepair.fonts_report(path)
        producer = _producer(path)
        records.append({**base, "sha": sha[:12], "sha_full": sha, "parsed": True, "producer": producer,
                        "contradicts": report["contradicts"], "fonts": report["fonts"], **measured})
        for page in measured["pages"]:
            pages_out.append({"sha": sha, "producer": producer, "stratum": base["stratum"], **page})

    parsed = [r for r in records if r.get("parsed")]
    regressions = [r["sha"] for r in parsed if not r["detected"] and not r["identical"]]
    random_parsed = [r for r in parsed if r["stratum"] == "random"]
    random_detected = sum(1 for r in random_parsed if r["detected"])
    enriched_detected = sum(1 for r in parsed if r["stratum"] == "enriched" and r["detected"])
    gate = defaultdict(Counter)
    for page in pages_out:
        key = f"{'+'.join(page['font_identities']) or '-'} | {page['producer'][:30]}"
        gate[key][page["status"]] += 1
    runs = sum(r["runs"] for r in parsed)
    matched = sum(r["runs_matched"] for r in parsed)
    summary = {
        "database": name, "engine": engine.version,
        "entries": len(stratum), "fetched_pdfs_parsed": len(parsed),
        "detected": sum(1 for r in parsed if r["detected"]),
        "enriched_detected": enriched_detected,
        "top_up_required": enriched_detected < 20,
        "prevalence_random": {"detected": random_detected, "parsed": len(random_parsed),
                              "wilson95": wilson(random_detected, len(random_parsed))},
        "non_regression": {"undetected": sum(1 for r in parsed if not r["detected"]),
                           "regressions": regressions},
        "classifier_invariance_failures": invariance_failures,
        "crosstab": crosstab(parsed),
        "run_agreement": {"runs": runs, "matched": matched,
                          "rate": round(matched / runs, 4) if runs else None},
        "gate_by_font_and_producer": {k: dict(v) for k, v in sorted(gate.items())},
    }
    Path(args.out_json).write_text(json.dumps({"summary": summary, "documents": records,
                                               "pages": pages_out}, ensure_ascii=False, indent=2) + "\n")
    lines = [f"native-3 cohort evidence — {name}, engine {engine.version}", ""]
    lines += [f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in summary.items()
              if k not in ("database", "engine")]
    Path(args.out_txt).write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    ok = not regressions and not invariance_failures
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Add it to `EVIDENCE`, and run the tests**

Add `"nrb_native3_evidence.py",` to `EVIDENCE` in `tests/test_nrb_dbguard.py`.
Run: `.venv/bin/pytest tests/test_nrb_native3_evidence.py tests/test_nrb_dbguard.py -q`
Expected: all passed.

- [ ] **Step 5: Run the evidence pass as a unit (G10)**

```bash
systemd-run --user --wait --collect --unit=native3-evidence --working-directory="$PWD" -E DATABASE_URL="$P4_URL" \
  -p StandardOutput=file:"$SCRATCH/native3-evidence.log" -p StandardError=file:"$SCRATCH/native3-evidence.log" \
  .venv/bin/python scripts/nrb_native3_evidence.py --manifest docs/nrb/native3-cohort.json \
  --out-json docs/nrb/native3-cohort-evidence.json --out-txt docs/nrb/native3-cohort-evidence.txt
tail -20 "$SCRATCH/native3-evidence.log"
```
Expected: `RESULT: PASS`.

If it prints `"top_up_required": true`, run `scripts/nrb_native3_cohort.py --top-up --out docs/nrb/native3-cohort-topup.json`, commit that file, fetch and extract it (Task 11, Steps 3–4, with that manifest), and re-run this step with both `--manifest` flags.

**A FAIL is a stop.** Report the regressions or invariance failures to the user; do not adjust anything to make them pass.

- [ ] **Step 6: Record §31.2 and commit**

Add `### 31.2 The cohort` to `docs/nrb-integration.md`. It covers:
- the manifest fingerprint;
- fetched vs entries;
- non-regression and invariance;
- the detector cross-tab and both candidate lists;
- prevalence (random stratum, Wilson 95%);
- the gate by font × producer;
- run agreement.

Run the full suite (G11), then:
```bash
git add scripts/nrb_native3_evidence.py tests/test_nrb_native3_evidence.py tests/test_nrb_dbguard.py \
        docs/nrb/native3-cohort-evidence.json docs/nrb/native3-cohort-evidence.txt docs/nrb-integration.md
git commit -m "feat(nrb): native-3 cohort evidence (§31.2)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 13: The reader sheet, its import, and CHECKPOINT B

**Files:**
- Create: `scripts/nrb_native3_workbook.py`, `scripts/nrb_native3_review_import.py`
- Test: `tests/test_nrb_native3_workbook.py`
- Create (after the reader): `docs/nrb/native3-review.json`

**Interfaces:**
- Consumes: the evidence JSON `pages` (Task 12) and the detect JSON (Task 5's `checkpoint-a.json`, used for production QA rows); `fontrepair.FontRepairEngine.repair_page`; `file_documents.read_pdf_pages`; `pdftoppm`.
- Produces:
  - pure helpers `cluster_classes(line) -> set[str]`, `word_version(producer) -> str`, `window(lines, index) -> tuple[int, int]`, `count_wrong(verdict, words) -> int | None`, `score(rows) -> dict`;
  - a sheet `native3-repair` and a sheet `native3 how to` in `/home/manoj/nrb-cohort-review.xlsx`;
  - `docs/nrb/native3-review.json`.

- [ ] **Step 1: Write the failing test** — `tests/test_nrb_native3_workbook.py`

```python
"""The reader sheet's pure parts (spec §6.4)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import nrb_native3_review_import as I  # noqa: E402
import nrb_native3_workbook as WB  # noqa: E402


def test_cluster_classes_find_each_risky_shape():
    assert WB.cluster_classes("गर्दा") == {"reph"}
    assert WB.cluster_classes("प्रकृति") == {"rakar", "prebase"}
    assert WB.cluster_classes("क्षेत्र") == {"conjunct", "rakar"}
    assert WB.cluster_classes("नेपाल") == {"plain"}


def test_word_version_reads_the_producer():
    assert WB.word_version("Microsoft® Word 2013") == "Word 2013"
    assert WB.word_version("Microsoft® Office Word 2007") == "Word 2007"
    assert WB.word_version("Microsoft® Word for Microsoft 365") == "Word 365"
    assert WB.word_version("Acrobat Distiller 10.0") == "other"


def test_the_excerpt_window_is_at_most_eight_lines_around_the_hit():
    assert WB.window(list("abcdefghijklmnop"), 7) == (5, 13)
    assert WB.window(list("abc"), 0) == (0, 3)


def test_wrong_words_are_counted_from_the_list_not_guessed():
    assert I.count_wrong("correct", "") == 0
    assert I.count_wrong("wrong words (list them)", "अकोर्, पनेर्छ") == 2
    assert I.count_wrong("wrong throughout", "") is None
    assert I.count_wrong("", "") is None


def test_the_pass_criterion_is_wer_and_no_class_wrong_twice():
    rows = [
        {"source": "cohort", "cluster": "reph", "verdict": "correct", "wrong": "", "repaired": "क " * 400},
        {"source": "cohort", "cluster": "reph", "verdict": "wrong words (list them)", "wrong": "अकोर्",
         "repaired": "क " * 400},
        {"source": "production", "cluster": "reph", "verdict": "wrong words (list them)", "wrong": "x y",
         "repaired": "क"},
    ]
    out = I.score(rows)
    assert out["evidence_rows"] == 2 and out["wrong_words"] == 1
    assert out["wer"] == round(1 / 800, 5)
    assert out["passed"] is True
    rows.append({"source": "cohort", "cluster": "reph", "verdict": "wrong words (list them)",
                 "wrong": "पनेर्छ", "repaired": "क"})
    assert I.score(rows)["passed"] is False
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_nrb_native3_workbook.py -v`
Expected: ERROR, modules not found.

- [ ] **Step 3: Write `scripts/nrb_native3_workbook.py`**

```python
#!/usr/bin/env python
"""Append the native-3 reader sheet to the cohort review workbook (spec §6.4). No database.

    .venv/bin/python scripts/nrb_native3_workbook.py \\
        --evidence docs/nrb/native3-cohort-evidence.json \\
        --production "$SCRATCH/checkpoint-a.json" \\
        --workbook /home/manoj/nrb-cohort-review.xlsx --pages-dir /home/manoj/native3-review-pages

APPEND-ONLY. It adds the sheets `native3-repair` and `native3 how to` and
touches nothing else; it refuses if `native3-repair` exists; it writes a
timestamped backup first. openpyxl drops embedded images it did not create on a
load→save, so NOTHING may re-save this workbook after this script has run —
the importer opens it read-only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import localtime  # noqa: E402
from app.files import documents as file_documents  # noqa: E402
from app.nrb import filestore, fontrepair, sampling  # noqa: E402

CLASSES = ("reph", "rakar", "prebase", "conjunct", "plain")
_REPH = re.compile(r"र्(?=[क-हक़-य़])")
_RAKAR = re.compile(r"्र")
# A consonant + virama + consonant that is neither a reph (र् first) nor a rakar (्र second).
_CONJUNCT = re.compile(r"(?!र)[क-ह]्(?!र)[क-ह]")
VERDICTS = ("correct", "wrong words (list them)", "wrong throughout")
SEED = "native3-workbook-2026-10"
HEADERS = ("id", "source", "document", "page", "lines", "font", "Word version", "cluster",
           "page image", "native text (today)", "repaired text", "verdict", "wrong words",
           "native also wrong?", "notes")


def cluster_classes(line: str) -> set[str]:
    found = set()
    if _REPH.search(line):
        found.add("reph")
    if _RAKAR.search(line):
        found.add("rakar")
    if "ि" in line:
        found.add("prebase")
    if _CONJUNCT.search(line):
        found.add("conjunct")
    return found or {"plain"}


def word_version(producer: str) -> str:
    m = re.search(r"Word (\d{4})", producer)
    if m:
        return f"Word {m.group(1)}"
    if "Microsoft 365" in producer:
        return "Word 365"
    if "LTSC" in producer:
        return "Word LTSC"
    return "other"


def window(lines, index: int) -> tuple[int, int]:
    start = max(0, index - 2)
    return start, min(len(lines), start + 8)


def _texts(engine, sha: str, page: int):
    path = filestore.resolve_path(filestore.storage_key_for(sha, "pdf"))
    native = file_documents.read_pdf_pages(path).pages[page - 1]
    out = engine.repair_page(path, page, native)
    return path, native, out


def _render(path: Path, page: int, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["pdftoppm", "-r", "90", "-f", str(page), "-l", str(page), "-png",
                    "-singlefile", str(path), str(target.with_suffix(""))], check=True)
    return target


def _candidates(pages, source):
    return [dict(p, source=source) for p in pages]


def select_rows(evidence: dict, production: dict | None, engine) -> list[dict]:
    cohort = _candidates([p for p in evidence["pages"] if p["status"] == fontrepair.STATUS_REPAIRED],
                         "cohort")
    prod = []
    for doc in (production or {}).get("documents", []):
        for p in doc.get("pages", []):
            if p["status"] == fontrepair.STATUS_REPAIRED:
                prod.append({"sha": doc["sha_full"] if "sha_full" in doc else None,
                             "page": p["page"], "font_identities": p["font_identities"],
                             "producer": doc.get("producer", ""), "source": "production"})
    rows, per_cell = [], {}
    for cand in sorted(cohort + [p for p in prod if p["sha"]],
                       key=lambda c: sampling.rank_for("native3-workbook", SEED, f"{c['sha']}:{c['page']}")):
        path, native, out = _texts(engine, cand["sha"], cand["page"])
        native_lines, repaired_lines = native.splitlines(), out.text.splitlines()
        if out.status != fontrepair.STATUS_REPAIRED or len(native_lines) != len(repaired_lines):
            continue
        for index, line in enumerate(repaired_lines):
            for klass in cluster_classes(line):
                cell = ("+".join(cand["font_identities"]), word_version(cand["producer"]), klass,
                        cand["source"])
                quota = 2 if cand["source"] == "cohort" else 1
                if per_cell.get(cell, 0) >= quota:
                    continue
                lo, hi = window(repaired_lines, index)
                rows.append({"source": cand["source"], "sha": cand["sha"], "page": cand["page"],
                             "lines": f"{lo + 1}–{hi}", "font": cell[0], "version": cell[1],
                             "cluster": klass, "path": path,
                             "native": "\n".join(native_lines[lo:hi]),
                             "repaired": "\n".join(repaired_lines[lo:hi])})
                per_cell[cell] = per_cell.get(cell, 0) + 1
    controls = 0
    for doc in evidence["documents"]:
        if controls == 6:
            break
        if not doc.get("parsed") or doc.get("detected") or not doc.get("sha_full"):
            continue
        path = filestore.resolve_path(filestore.storage_key_for(doc["sha_full"], "pdf"))
        first = file_documents.read_pdf_pages(path).pages[0]
        if fontrepair.detect([first]).devanagari_chars < 50:
            continue  # a control must be clean NEPALI text, or it calibrates nothing
        rows.append({"source": "cohort", "control": True, "sha": doc["sha_full"], "page": 1,
                     "cluster": "control", "font": "-", "version": word_version(doc.get("producer", ""))})
        controls += 1
    unrepaired = [p for p in evidence["pages"] if str(p["status"]).startswith(fontrepair.UNREPAIRED_PREFIX)]
    for page in unrepaired[:6]:
        rows.append({"source": "cohort", "unrepaired": page["status"], "sha": page["sha"],
                     "page": page["page"], "cluster": "unrepaired",
                     "font": "+".join(page["font_identities"]), "version": word_version(page["producer"])})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--evidence", required=True)
    ap.add_argument("--production", default=None)
    ap.add_argument("--workbook", required=True)
    ap.add_argument("--pages-dir", required=True)
    args = ap.parse_args()

    from openpyxl import load_workbook
    from openpyxl.cell.rich_text import CellRichText, TextBlock
    from openpyxl.cell.text import InlineFont
    from openpyxl.drawing.image import Image
    from openpyxl.styles import Alignment
    from openpyxl.worksheet.datavalidation import DataValidation

    book = Path(args.workbook)
    wb = load_workbook(book)
    if "native3-repair" in wb.sheetnames:
        print("refusing: the workbook already has a native3-repair sheet", file=sys.stderr)
        return 2
    stamp = localtime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(book, book.with_name(f"{book.stem}.backup-{stamp}{book.suffix}"))

    engine = fontrepair.FontRepairEngine()
    evidence = json.loads(Path(args.evidence).read_text())
    production = json.loads(Path(args.production).read_text()) if args.production else None
    rows = select_rows(evidence, production, engine)

    ws = wb.create_sheet("native3-repair")
    ws.append(HEADERS)
    bold = InlineFont(b=True)
    verdicts = DataValidation(type="list", formula1='"' + ",".join(VERDICTS) + '"', allow_blank=True)
    native_also = DataValidation(type="list", formula1='"yes,no"', allow_blank=True)
    ws.add_data_validation(verdicts)
    ws.add_data_validation(native_also)
    for width, col in zip((7, 11, 30, 6, 8, 18, 12, 11, 70, 50, 50, 22, 30, 12, 30), "ABCDEFGHIJKLMNO"):
        ws.column_dimensions[col].width = width
    pages_dir = Path(args.pages_dir)
    for n, row in enumerate(rows, start=1):
        r = n + 1
        rid = f"n{n:03d}"
        path = filestore.resolve_path(filestore.storage_key_for(row["sha"], "pdf"))
        if row.get("control") or row.get("unrepaired"):
            native = file_documents.read_pdf_pages(path).pages[row["page"] - 1]
            lines = native.splitlines()[:8]
            row["native"] = row["repaired"] = "\n".join(lines)
            row["lines"] = f"1–{len(lines)}"
            if row.get("unrepaired"):
                row["repaired"] = f"(not repaired: {row['unrepaired']})"
        ws.cell(r, 1, rid)
        ws.cell(r, 2, row["source"])
        ws.cell(r, 3, row["sha"][:12])
        ws.cell(r, 4, row["page"])
        ws.cell(r, 5, row["lines"])
        ws.cell(r, 6, row["font"])
        ws.cell(r, 7, row["version"])
        ws.cell(r, 8, row["cluster"])
        ws.cell(r, 10, row["native"])
        klass = row["cluster"]
        pattern = {"reph": _REPH, "rakar": _RAKAR, "conjunct": _CONJUNCT}.get(klass)
        text = row["repaired"]
        if pattern is not None or klass == "prebase":
            blocks, cursor = [], 0
            hits = (re.finditer("ि", text) if klass == "prebase" else pattern.finditer(text))
            for m in hits:
                blocks.append(text[cursor:m.start()])
                blocks.append(TextBlock(bold, text[m.start():m.end() + (1 if klass == "reph" else 0)]))
                cursor = m.end() + (1 if klass == "reph" else 0)
            blocks.append(text[cursor:])
            ws.cell(r, 11).value = CellRichText([b for b in blocks if b != ""])
        else:
            ws.cell(r, 11, text)
        for col in (10, 11):
            ws.cell(r, col).alignment = Alignment(wrap_text=True, vertical="top")
        verdicts.add(ws.cell(r, 12))
        native_also.add(ws.cell(r, 14))
        image_path = _render(path, row["page"], pages_dir / f"{rid}.png")
        image = Image(str(image_path))
        scale = 480 / image.width
        image.width, image.height = 480, int(image.height * scale)
        ws.add_image(image, f"I{r}")
        ws.row_dimensions[r].height = 400

    how = wb.create_sheet("native3 how to")
    for line in (
        "native3-repair — about a minute per row:",
        "1. Look at the page image at the given lines. Read the 'repaired text'; the risky letters for that row are in bold.",
        "2. verdict: 'correct' if every word matches the page; 'wrong words (list them)' and type each wrong word in 'wrong words'; 'wrong throughout' if it is not the page's text at all.",
        "3. 'native also wrong?': is TODAY's text (the 'native text' column) also wrong for these lines? yes / no.",
        "4. control rows show unchanged text: mark them as you would any other row.",
        "5. unrepaired rows show text we could NOT fix; judge the native text only.",
        "Every row matters: the repair ships only if these verdicts pass (spec §6.4).",
    ):
        how.append([line])
    wb.save(book)
    print(f"appended {len(rows)} rows to {book}; images in {pages_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

The detect JSON must carry the full sha for production QA rows. In `scripts/nrb_native3_detect.py` (Task 5), add `"sha_full": sha,` to the record dict next to `"sha": sha[:12]`. That is a one-line edit; make it in this task.

- [ ] **Step 4: Write `scripts/nrb_native3_review_import.py`**

```python
#!/usr/bin/env python
"""Read the reader's verdicts back, READ-ONLY, and score the pre-registered criterion (spec §6.4).

    .venv/bin/python scripts/nrb_native3_review_import.py \\
        --workbook /home/manoj/nrb-cohort-review.xlsx --out docs/nrb/native3-review.json

PASS = repair-caused word error rate <= 0.5% over the EVIDENCE (cohort) rows,
and no cluster class with wrong words in two rows. Production rows are shipping
QA: reported, never scored. Opened with read_only=True — this script must never
save the workbook (openpyxl would drop the page images).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

WER_LIMIT = 0.005


def count_wrong(verdict: str, words: str) -> int | None:
    if verdict == "correct":
        return 0
    if verdict == "wrong words (list them)":
        return len([w for w in re.split(r"[,\s]+", words or "") if w])
    return None


def score(rows: list[dict]) -> dict:
    evidence = [r for r in rows if r["source"] == "cohort" and r["cluster"] not in ("control", "unrepaired")]
    unanswered = [r for r in evidence if count_wrong(r["verdict"], r["wrong"]) is None
                  and r["verdict"] != "wrong throughout"]
    wrong_total, words_total = 0, 0
    wrong_rows_by_class = Counter()
    throughout = 0
    for r in evidence:
        words_total += len(r["repaired"].split())
        n = count_wrong(r["verdict"], r["wrong"])
        if r["verdict"] == "wrong throughout":
            throughout += 1
            wrong_rows_by_class[r["cluster"]] += 1
            continue
        if n:
            wrong_total += n
            wrong_rows_by_class[r["cluster"]] += 1
    wer = round(wrong_total / words_total, 5) if words_total else None
    repeated = sorted(c for c, n in wrong_rows_by_class.items() if n >= 2)
    passed = (not unanswered and throughout == 0 and wer is not None
              and wer <= WER_LIMIT and not repeated)
    return {"evidence_rows": len(evidence), "unanswered": len(unanswered), "words": words_total,
            "wrong_words": wrong_total, "wrong_throughout": throughout, "wer": wer,
            "classes_wrong_twice": repeated, "passed": passed}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--workbook", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    from openpyxl import load_workbook

    wb = load_workbook(args.workbook, read_only=True, data_only=True)
    ws = wb["native3-repair"]
    it = ws.iter_rows(values_only=True)
    header = next(it)
    col = {name: i for i, name in enumerate(header)}
    rows = []
    for values in it:
        if not values or not values[col["id"]]:
            continue
        rows.append({"id": values[col["id"]], "source": values[col["source"]],
                     "cluster": values[col["cluster"]], "verdict": values[col["verdict"]] or "",
                     "wrong": values[col["wrong words"]] or "",
                     "native_also_wrong": values[col["native also wrong?"]],
                     "repaired": str(values[col["repaired text"]] or "")})
    result = {"score": score(rows), "rows": rows}
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result["score"], ensure_ascii=False, indent=2))
    return 0 if result["score"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_nrb_native3_workbook.py -v`
Expected: all passed.

- [ ] **Step 6: Run the full suite (G11), commit, then build the sheet**

```bash
git add scripts/nrb_native3_workbook.py scripts/nrb_native3_review_import.py scripts/nrb_native3_detect.py \
        tests/test_nrb_native3_workbook.py
git commit -m "feat(nrb): the native-3 reader sheet and its read-only import

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
Re-run Task 5 Step 8 so that `checkpoint-a.json` carries `sha_full`. Then:
```bash
systemd-run --user --wait --collect --unit=native3-workbook --working-directory="$PWD" \
  -p StandardOutput=file:"$SCRATCH/workbook.log" -p StandardError=file:"$SCRATCH/workbook.log" \
  .venv/bin/python scripts/nrb_native3_workbook.py --evidence docs/nrb/native3-cohort-evidence.json \
  --production "$SCRATCH/checkpoint-a.json" --workbook /home/manoj/nrb-cohort-review.xlsx \
  --pages-dir /home/manoj/native3-review-pages
tail -3 "$SCRATCH/workbook.log"
```
Expected: `appended ~50 rows …`, and a `nrb-cohort-review.backup-<stamp>.xlsx` next to the workbook.

- [ ] **Step 7: STOP — CHECKPOINT B. Hand the workbook to the reader and wait.**

Tell the user:
- the workbook path;
- the row count;
- that the two sheets `native3-repair` and `native3 how to` sit beside the 50 questions;
- that the page images are in `/home/manoj/native3-review-pages`.

**Do not continue until the user says the review is done.**

- [ ] **Step 8: Import the verdicts and record §31.3**

Run: `.venv/bin/python scripts/nrb_native3_review_import.py --workbook /home/manoj/nrb-cohort-review.xlsx --out docs/nrb/native3-review.json`
Then add `### 31.3 The reader's verdict` to `docs/nrb-integration.md`, covering:
- WER and its denominator;
- wrong words by cluster class;
- the "native also wrong?" tally, which confirms §17.6's extent;
- PASS/FAIL;
- for FAIL, the classes that failed.

**On FAIL:** each repeated class is a systematic bug.
- Fix it with a new pure test.
- Bump `REPAIR_VERSION` only if Task 14 has already run.
- The reviewed rows of that class become development evidence.
- Validate on fresh excerpts drawn from cohort documents not yet shown: re-run the builder against a copy of the workbook, with only the affected cells.
- Then repeat Checkpoint B.

Commit:
```bash
git add docs/nrb/native3-review.json docs/nrb-integration.md
git commit -m "docs(nrb): the Nepali reader's native-3 verdict (§31.3)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

# Milestone 4 — rollout

### Task 14: Repair the production corpus in `local_ai_gateway_build`

**Files:**
- Modify: `app/rag/reingest.py` (`document_ids` and `--ids-file`)
- Create: `scripts/nrb_corpus_fingerprint.py`
- Modify: `tests/test_nrb_dbguard.py` (`OPERATIONAL`)
- Test: `tests/test_rag_reingest_ids.py`, `tests/test_nrb_corpus_fingerprint.py`

**Interfaces:**
- Produces:
  - `reingest(session, *, department_code, dry_run, document_ids: Sequence[str] | None = None)`;
  - `scripts/nrb_corpus_fingerprint.py --department CODE --out FILE`, and `--compare BEFORE AFTER --expect-changed IDS_FILE`;
  - the pure `compare(before, after, expected) -> dict`.

- [ ] **Step 1: Write the failing tests**

`tests/test_rag_reingest_ids.py`:
```python
"""Re-ingest exactly the named documents (spec §8.2c)."""

from __future__ import annotations

import inspect

from app.rag import reingest


def test_reingest_accepts_document_ids():
    assert "document_ids" in inspect.signature(reingest.reingest).parameters


def test_the_cli_reads_an_ids_file(tmp_path):
    ids = tmp_path / "ids.txt"
    ids.write_text("a1\n\nb2\n")
    assert reingest.read_ids_file(ids) == ["a1", "b2"]
```

`tests/test_nrb_corpus_fingerprint.py`:
```python
"""Before/after proof that the repair changed exactly the detected documents (spec §8.2d)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import nrb_corpus_fingerprint as F  # noqa: E402


def _doc(digest, routes=("native",), status="ready", chunks=3):
    return {"status": status, "chunks": chunks, "digest": digest, "routes": list(routes)}


def test_exactly_the_expected_documents_changed():
    before = {"documents": {"a": _doc("1"), "b": _doc("2")}}
    after = {"documents": {"a": _doc("9"), "b": _doc("2")}}
    out = F.compare(before, after, {"a"})
    assert out["ok"] and out["changed"] == ["a"]


def test_an_unexpected_change_fails():
    before = {"documents": {"a": _doc("1"), "b": _doc("2")}}
    after = {"documents": {"a": _doc("9"), "b": _doc("7")}}
    out = F.compare(before, after, {"a"})
    assert not out["ok"] and out["unexpected"] == ["b"]


def test_a_route_change_fails_even_when_expected():
    before = {"documents": {"a": _doc("1", ("native",))}}
    after = {"documents": {"a": _doc("9", ("native", "ocr"))}}
    assert not F.compare(before, after, {"a"})["ok"]


def test_an_expected_document_that_did_not_change_fails():
    before = {"documents": {"a": _doc("1")}}
    after = {"documents": {"a": _doc("1")}}
    assert F.compare(before, after, {"a"})["unchanged_expected"] == ["a"]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_rag_reingest_ids.py tests/test_nrb_corpus_fingerprint.py -v`
Expected: FAIL / ERROR (no `document_ids`, no `read_ids_file`, no script).

- [ ] **Step 3: Implement `document_ids` in `app/rag/reingest.py`**

Add the parameter `document_ids: Sequence[str] | None = None` to `reingest` (import `Sequence` from `typing`). After the department filter, add:

```python
    if document_ids is not None:
        stmt = stmt.where(Document.id.in_(list(document_ids)))
```
Add the helper:
```python
def read_ids_file(path) -> list[str]:
    """One document id per line; blank lines ignored."""
    from pathlib import Path

    return [line.strip() for line in Path(path).read_text().splitlines() if line.strip()]
```
In `_main`, add `parser.add_argument("--ids-file", default=None, help="Only these document ids, one per line.")`, and pass `document_ids=read_ids_file(args.ids_file) if args.ids_file else None` to `reingest(...)`. Finally, add one line to the module docstring: `` `--ids-file FILE` limits the run to the listed document ids (native-3 re-ingests only the detected documents). ``

- [ ] **Step 4: Write `scripts/nrb_corpus_fingerprint.py`**

```python
#!/usr/bin/env python
"""Fingerprint one department's corpus, and compare two fingerprints (spec §8.2a, d).

    DATABASE_URL=postgresql+asyncpg://gateway:***@127.0.0.1:5432/local_ai_gateway_build \\
        .venv/bin/python scripts/nrb_corpus_fingerprint.py --department nrb --out "$SCRATCH/before.json"
    .venv/bin/python scripts/nrb_corpus_fingerprint.py --compare before.json after.json --expect-changed ids.txt

OPERATIONAL (app/nrb/dbguard.py). Per document: status, chunk count, the routes
its chunks carry, and a sha256 over its chunks' content AND metadata in order —
metadata, so a detected-but-unrepaired document (text unchanged, now
non-authoritative) still registers as changed. Plus the corpus checks
build_release_db.py prints. Read-only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.nrb import dbguard  # noqa: E402

_DOCS = text("""
    SELECT d.id, d.status, count(c.id) AS chunks,
           encode(sha256(convert_to(coalesce(string_agg(c.content || E'\\x1f' || c.metadata::text,
                  E'\\x1e' ORDER BY c.chunk_index), ''), 'UTF8')), 'hex') AS digest,
           coalesce(array_agg(DISTINCT coalesce(c.metadata->>'route', '-'))
                    FILTER (WHERE c.id IS NOT NULL), '{}') AS routes
      FROM documents d
      JOIN departments dep ON dep.id = d.department_id
      LEFT JOIN document_chunks c ON c.document_id = d.id
     WHERE dep.code = :code
     GROUP BY d.id, d.status ORDER BY d.id
""")
_CHECKS = {
    "chunks_without_embedding": """SELECT count(*) FROM document_chunks c JOIN departments dep
        ON dep.id = c.department_id WHERE dep.code = :code AND c.embedding IS NULL""",
    "ready_without_chunks": """SELECT count(*) FROM documents d JOIN departments dep ON dep.id = d.department_id
        WHERE dep.code = :code AND d.status = 'ready'
        AND NOT EXISTS (SELECT 1 FROM document_chunks c WHERE c.document_id = d.id)""",
}
_REPAIR_SPLIT = text("""SELECT coalesce(c.metadata->>'text_repair', '-') AS repair, count(*) AS n
    FROM document_chunks c JOIN departments dep ON dep.id = c.department_id
    WHERE dep.code = :code GROUP BY 1 ORDER BY 1""")


def compare(before: dict, after: dict, expected: set[str]) -> dict:
    b, a = before["documents"], after["documents"]
    changed = sorted(k for k in a if k in b and a[k]["digest"] != b[k]["digest"])
    unexpected = sorted(set(changed) - expected)
    unchanged_expected = sorted(expected - set(changed))
    route_changes = sorted(k for k in changed if sorted(a[k]["routes"]) != sorted(b[k]["routes"]))
    status_changes = sorted(k for k in a if k in b and a[k]["status"] != b[k]["status"])
    ok = not unexpected and not unchanged_expected and not route_changes and not status_changes \
        and set(a) == set(b)
    return {"ok": ok, "changed": changed, "unexpected": unexpected,
            "unchanged_expected": unchanged_expected, "route_changes": route_changes,
            "status_changes": status_changes}


async def _snapshot(url: str, code: str) -> dict:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            docs = {r.id: {"status": r.status, "chunks": r.chunks, "digest": r.digest,
                           "routes": list(r.routes)}
                    for r in await conn.execute(_DOCS, {"code": code})}
            checks = {name: (await conn.execute(text(sql), {"code": code})).scalar()
                      for name, sql in _CHECKS.items()}
            split = {r.repair: r.n for r in await conn.execute(_REPAIR_SPLIT, {"code": code})}
    finally:
        await engine.dispose()
    return {"documents": docs, "checks": checks, "repair_split": split}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--department")
    ap.add_argument("--out")
    ap.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"))
    ap.add_argument("--expect-changed")
    args = ap.parse_args()
    if args.compare:
        before, after = (json.loads(Path(p).read_text()) for p in args.compare)
        expected = set(Path(args.expect_changed).read_text().split()) if args.expect_changed else set()
        out = compare(before, after, expected)
        out["checks_after"] = after["checks"]
        out["repair_split_after"] = after["repair_split"]
        print(json.dumps(out, indent=2))
        healthy = all(v == 0 for v in after["checks"].values())
        return 0 if out["ok"] and healthy else 1
    url = os.environ.get("DATABASE_URL", "")
    try:
        name = dbguard.require(url, dbguard.BUILD_DATABASES)
    except dbguard.RefusedDatabase as exc:
        print(f"refusing to run: {exc}", file=sys.stderr)
        return 2
    print(f"database: {name}")
    snap = asyncio.run(_snapshot(url, args.department))
    Path(args.out).write_text(json.dumps(snap, indent=2) + "\n")
    print(json.dumps({"documents": len(snap["documents"]), **snap["checks"],
                      "repair_split": snap["repair_split"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Add to `OPERATIONAL` in `tests/test_nrb_dbguard.py`:
```python
    "nrb_corpus_fingerprint.py": ["--department", "nrb", "--out", "/dev/null"],
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_rag_reingest_ids.py tests/test_nrb_corpus_fingerprint.py tests/test_nrb_dbguard.py tests/test_rag_reingest_integration.py -q`
Expected: all passed. `test_department_filter_restricts_the_set` is known to fail on databases holding real data (CLAUDE.md); if it fails here and passed in the baseline, investigate.

- [ ] **Step 6: Run the full suite (G11), then commit**

```bash
git add app/rag/reingest.py scripts/nrb_corpus_fingerprint.py tests/test_rag_reingest_ids.py \
        tests/test_nrb_corpus_fingerprint.py tests/test_nrb_dbguard.py
git commit -m "feat(rag): re-ingest named documents; corpus fingerprints for the native-3 repair

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 7: The repair run (operational; every long step is a unit)**

```bash
# a. fingerprint before
DATABASE_URL="$BUILD_URL" .venv/bin/python scripts/nrb_corpus_fingerprint.py --department nrb --out "$SCRATCH/before.json"
# b. which documents (the real number replaces "24")
systemd-run --user --wait --collect --unit=native3-detect-build --working-directory="$PWD" -E DATABASE_URL="$BUILD_URL" \
  -p StandardOutput=file:"$SCRATCH/detect-build.log" -p StandardError=file:"$SCRATCH/detect-build.log" \
  .venv/bin/python scripts/nrb_native3_detect.py --department nrb --out "$SCRATCH/detect-build.json" \
  --ids-out "$SCRATCH/detected-ids.txt"
wc -l "$SCRATCH/detected-ids.txt"
# c. queue them, then run the worker WITH the flag on
DATABASE_URL="$BUILD_URL" .venv/bin/python -m app.rag.reingest --department nrb --ids-file "$SCRATCH/detected-ids.txt"
systemd-run --user --collect --unit=native3-worker --working-directory="$PWD" -E DATABASE_URL="$BUILD_URL" \
  -E NRB_NATIVE_REPAIR=true -p StandardOutput=file:"$SCRATCH/worker.log" -p StandardError=file:"$SCRATCH/worker.log" \
  .venv/bin/python -m app.rag.worker
```
Watch the queue drain with Monitor, using an until-loop on:
```bash
PGPASSWORD=postgres psql -h 127.0.0.1 -U postgres -d local_ai_gateway_build -tAc "SELECT count(*) FROM ingest_jobs WHERE status IN ('queued','running')"
```
When it reports 0, run `systemctl --user stop native3-worker`. Then check that the worker log shows `converter 0, ocr 0; repaired N, unrepaired M` on every document:
```bash
grep -c "nrb recovery" "$SCRATCH/worker.log"
grep "nrb recovery" "$SCRATCH/worker.log" | grep -v "converter 0, ocr 0"
```
The second command must print nothing.

Then verify:
```bash
DATABASE_URL="$BUILD_URL" .venv/bin/python scripts/nrb_corpus_fingerprint.py --department nrb --out "$SCRATCH/after.json"
.venv/bin/python scripts/nrb_corpus_fingerprint.py --compare "$SCRATCH/before.json" "$SCRATCH/after.json" \
  --expect-changed "$SCRATCH/detected-ids.txt"
```
Expected: `"ok": true`, `unexpected: []`, `route_changes: []`, `chunks_without_embedding: 0`, `ready_without_chunks: 0`, and a repair split with both `font_tables` and `unrepaired` counts. Anything else is a stop: report it.

- [ ] **Step 8: Record §31.4 (the production repair) and commit**

The record covers:
- the detected count;
- pages repaired and unrepaired by reason;
- the chunk-level repair split;
- the fingerprint comparison;
- the worker evidence (`converter 0, ocr 0`).

From this point `REPAIR_VERSION` is load-bearing (plan clarification 6).

```bash
git add docs/nrb-integration.md
git commit -m "docs(nrb): native-3 repair of the production corpus in the build database (§31.4)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 15: Rebuild the release database, smoke-test it, PG15 re-dump

**Files:**
- Modify: `scripts/build_release_db.py`
- Test: `tests/test_build_release_db.py`

**Interfaces:**
- Produces: `build_release_db.foreign_ids(target_ids: set[str], source_ids: set[str]) -> list[str]`. `--replace` refuses when that list is non-empty.

- [ ] **Step 1: Write the failing test** — `tests/test_build_release_db.py`

```python
"""build_release_db --replace must never delete a document the source does not hold (spec §8.2)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_release_db as B  # noqa: E402


def test_documents_only_in_the_target_are_foreign():
    assert B.foreign_ids({"a", "b", "x"}, {"a", "b"}) == ["x"]
    assert B.foreign_ids({"a"}, {"a", "b"}) == []
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_build_release_db.py -v`
Expected: FAIL, `AttributeError: … has no attribute 'foreign_ids'`.

- [ ] **Step 3: Implement the guard in `scripts/build_release_db.py`**

Add above `main`:
```python
def foreign_ids(target_ids: set[str], source_ids: set[str]) -> list[str]:
    """Document ids the target's department holds that the source does not.

    `--replace` deletes the department's documents in the target before copying.
    Once a deployment has users, an admin may have uploaded their own files into
    that department; deleting them because they are not in the build would be
    silent data loss. Refuse instead and name them (spec §8.2).
    """
    return sorted(target_ids - source_ids)
```
In `main`, inside `if args.replace:` and before the first `DELETE`, add:
```python
        def _ids(pg: Pg) -> set[str]:
            raw = pg.scalar(f"SELECT coalesce(string_agg(id, ','), '') FROM documents WHERE {dept_docs}")
            return set(filter(None, raw.split(",")))

        foreign = foreign_ids(_ids(dst), _ids(src))
        if foreign:
            raise SystemExit(
                f"refusing --replace: {len(foreign)} document(s) in the target's "
                f"{args.department} department are not in the source: {', '.join(foreign[:10])}"
            )
```

- [ ] **Step 4: Run the test to verify it passes, run the full suite (G11), then commit**

Run: `.venv/bin/pytest tests/test_build_release_db.py -v` → passed.
```bash
git add scripts/build_release_db.py tests/test_build_release_db.py
git commit -m "fix(release): --replace refuses to delete documents the source does not hold

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 5: Smoke questions BEFORE (on the delivered `ai_gateway`)**

```bash
AIG_URL="$(.venv/bin/python -c "from app.config import get_settings; u=get_settings().database_url; print(u.rsplit('/',1)[0]+'/ai_gateway')")"
systemd-run --user --wait --collect --unit=native3-smoke-before --working-directory=/home/manoj/title-channel -E DATABASE_URL="$AIG_URL" \
  -E PYTHONPATH=/home/manoj/newlaptop/projects/python/local-ai-model-gateway \
  -p StandardOutput=file:"$SCRATCH/smoke-before.log" -p StandardError=file:"$SCRATCH/smoke-before.log" \
  /home/manoj/newlaptop/projects/python/local-ai-model-gateway/.venv/bin/python measure_title_channel.py "$SCRATCH/smoke-before.json"
```
The script imports `app`, hence `PYTHONPATH`, and needs local Ollama with `qwen3-embedding:4b-q8_0` for the query embeddings, as when §9.11 ran it. It carries the nine §9.11 questions. If the six §9.8 Acts questions are not among them, add them in a copy, `measure_native3_smoke.py`, beside it, and say in §31.5 exactly which questions ran.

- [ ] **Step 6: Rebuild `ai_gateway` from the repaired build database**

```bash
systemd-run --user --wait --collect --unit=native3-release --working-directory="$PWD" \
  -p StandardOutput=file:"$SCRATCH/release.log" -p StandardError=file:"$SCRATCH/release.log" \
  .venv/bin/python scripts/build_release_db.py --target ai_gateway --replace
tail -20 "$SCRATCH/release.log"
```
Expected: every table `ok` and every check `ok`, with no `MISMATCH`.

- [ ] **Step 7: Smoke questions AFTER, and compare**

Re-run Step 5 with the unit name `native3-smoke-after` and output `$SCRATCH/smoke-after.json`. Put the before/after ranks per question in a table in §31.5. It is a smoke test, not evidence (spec §8.2e).

- [ ] **Step 8: The PG15 re-dump (§9.9's path, now scripted for repeatability)**

```bash
mv /home/manoj/nrb-release/ai_gateway.pg15.dump /home/manoj/nrb-release/ai_gateway.pg15.2026-09-29.dump
docker run -d --name nrb-pg15 -e POSTGRES_PASSWORD=pg15 pgvector/pgvector:pg15
until docker exec nrb-pg15 pg_isready -U postgres; do sleep 1; done   # inside a Monitor until-loop
docker exec nrb-pg15 psql -U postgres -c "CREATE ROLE gateway LOGIN PASSWORD 'gateway'" -c "CREATE DATABASE ai_gateway OWNER gateway"
PGPASSWORD=postgres pg_dump -h 127.0.0.1 -U postgres -Fp ai_gateway > "$SCRATCH/ai_gateway.plain.sql"
docker exec -i nrb-pg15 psql -U postgres -d ai_gateway < "$SCRATCH/ai_gateway.plain.sql" 2> "$SCRATCH/pg15-load.err"
grep -c ERROR "$SCRATCH/pg15-load.err"
docker exec nrb-pg15 pg_dump -U postgres -Fc ai_gateway > /home/manoj/nrb-release/ai_gateway.pg15.dump
docker exec nrb-pg15 psql -U postgres -c "CREATE DATABASE verify OWNER gateway"
docker exec -i nrb-pg15 pg_restore -U postgres -d verify < /home/manoj/nrb-release/ai_gateway.pg15.dump && echo RESTORE-OK
```
Expected: exactly **1** ERROR in `pg15-load.err` (the PG17+ `SET transaction_timeout`, harmless as in §9.9), and `RESTORE-OK`. Run the long pipes as units (G10) if they exceed a minute. Then compare the document and chunk counts and the repair split in `verify` with `ai_gateway`. **Keep the `nrb-pg15` container**, because Task 16 uses it.

- [ ] **Step 9: Record §31.5 and commit**

Write the release rebuild, the smoke table and the dump checksum into §31.5. Run `sha256sum ai_gateway.pg15.dump` and record the value; `SHA256SUMS` is updated in Task 17.
```bash
git add docs/nrb-integration.md
git commit -m "docs(nrb): native-3 release rebuild and smoke ranks (§31.5)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 16: The bank's data-only update, and its rehearsal

**Files:**
- Create: `scripts/build_nrb_update_sql.py`, `scripts/nrb_update_rehearsal.py`
- Test: `tests/test_nrb_update_sql.py`

**Interfaces:**
- Consumes: `build_release_db.PLAN`, `build_release_db.Pg`, and `models.PIPELINE_ACTIVE_STATUSES`.
- Produces:
  - pure `replaced_tables() -> list[str]`, `prologue(meta) -> str`, `epilogue(meta) -> str`, `check_sql(meta) -> str`;
  - files `/home/manoj/nrb-release/update.sql` and `/home/manoj/nrb-release/check.sql`;
  - a rehearsal that exits 0 only when every assertion holds.

- [ ] **Step 1: Write the failing test** — `tests/test_nrb_update_sql.py`

```python
"""The bank's update script is generated, so its safety properties are tested as text (spec §8.3)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_nrb_update_sql as U  # noqa: E402

META = {"department_id": 1, "department_code": "nrb", "document_ids": ["a1", "b2"],
        "max_sync_run": 5, "max_fetch_run": 4,
        "expected": {"documents": 2, "document_chunks": 10, "repair": {"font_tables": 3}}}


def test_departments_is_never_replaced_and_the_rest_follow_build_release_db():
    tables = U.replaced_tables()
    assert "departments" not in tables
    assert tables[-2:] == ["documents", "document_chunks"]


def test_every_stop_check_is_in_the_prologue_and_raises():
    sql = U.prologue(META)
    for needle in ("STOP: the nrb department id", "STOP: documents in the nrb department",
                   "STOP: sync runs newer", "STOP: a pipeline run is active"):
        assert needle in sql
    assert sql.count("RAISE EXCEPTION") >= 4


def test_the_prologue_deletes_only_the_one_department():
    sql = U.prologue(META)
    assert "DELETE FROM document_chunks WHERE department_id = 1;" in sql
    assert "DELETE FROM documents WHERE department_id = 1;" in sql
    assert "TRUNCATE" in sql and "departments" not in sql.split("TRUNCATE", 1)[1].split(";")[0]


def test_the_epilogue_verifies_inside_the_transaction():
    sql = U.epilogue(META)
    assert "STOP: verification failed" in sql
    assert "chunks without an embedding" in sql


def test_the_check_script_changes_nothing():
    sql = U.check_sql(META).upper()
    for verb in ("DELETE", "TRUNCATE", "INSERT", "UPDATE ", "COPY"):
        assert verb not in sql
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_nrb_update_sql.py -v`
Expected: ERROR, module not found.

- [ ] **Step 3: Write `scripts/build_nrb_update_sql.py`**

```python
#!/usr/bin/env python
"""Generate the bank's data-only NRB update: update.sql (atomic) and check.sql (read-only).

    # 1. data, dumped by PG15's own pg_dump from the PG15 copy of the release (Task 15):
    docker exec nrb-pg15 pg_dump -U postgres -d ai_gateway --data-only --format=plain \\
        $(.venv/bin/python scripts/build_nrb_update_sql.py --print-table-flags) \\
        > "$SCRATCH/update-data.sql" 2> "$SCRATCH/update-data.err"
    # 2. the scripts:
    .venv/bin/python scripts/build_nrb_update_sql.py --release-db ai_gateway \\
        --data "$SCRATCH/update-data.sql" --out-dir /home/manoj/nrb-release

update.sql is run with `psql -1 -v ON_ERROR_STOP=1`: ONE transaction. Its
prologue raises (and so rolls back) on any stop check; its epilogue re-counts
and raises if the result is not what we shipped. Either way nothing changes.
`departments` is never replaced — their users' grants and chats reference it.
Reads the release database through psql (build_release_db.Pg); refuses any
database `build_release_db.NEVER_TARGET` names.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import build_release_db as B  # noqa: E402

from app.nrb.models import PIPELINE_ACTIVE_STATUSES  # noqa: E402


def replaced_tables() -> list[str]:
    return [table for table, _ in B.PLAN if table != "departments"]


def _nrb_tables() -> list[str]:
    return [t for t in replaced_tables() if t.startswith("nrb_")]


def prologue(meta: dict) -> str:
    dept = int(meta["department_id"])
    code = meta["department_code"]
    shipped = ",".join("'" + i.replace("'", "''") + "'" for i in meta["document_ids"])
    active = ",".join(f"'{s}'" for s in PIPELINE_ACTIVE_STATUSES)
    return f"""\\set ON_ERROR_STOP on
-- NRB corpus update (native-3). Run ONLY as: psql -1 -v ON_ERROR_STOP=1 -f update.sql
-- One transaction: any STOP below, or the verification at the end, rolls everything back.
DO $$
BEGIN
  IF (SELECT id FROM departments WHERE code = '{code}') IS DISTINCT FROM {dept} THEN
    RAISE EXCEPTION 'STOP: the {code} department id is not {dept} on this database';
  END IF;
  IF EXISTS (SELECT 1 FROM documents WHERE department_id = {dept} AND id NOT IN ({shipped})) THEN
    RAISE EXCEPTION 'STOP: documents in the {code} department that this update did not ship (an admin upload?) - nothing changed';
  END IF;
  IF (SELECT coalesce(max(id), 0) FROM nrb_sync_runs) > {int(meta["max_sync_run"])}
     OR (SELECT coalesce(max(id), 0) FROM nrb_fetch_runs) > {int(meta["max_fetch_run"])} THEN
    RAISE EXCEPTION 'STOP: sync runs newer than the delivery exist (their runner synced) - nothing changed';
  END IF;
  IF EXISTS (SELECT 1 FROM nrb_pipeline_runs WHERE status IN ({active})) THEN
    RAISE EXCEPTION 'STOP: a pipeline run is active - stop the nrb-runner and settle it first';
  END IF;
END $$;

DELETE FROM document_chunks WHERE department_id = {dept};
DELETE FROM ingest_jobs WHERE document_id IN (SELECT id FROM documents WHERE department_id = {dept});
DELETE FROM documents WHERE department_id = {dept};
TRUNCATE {", ".join(reversed(_nrb_tables()))};

-- ===== data (PG15 pg_dump --data-only) =====
"""


def epilogue(meta: dict) -> str:
    dept = int(meta["department_id"])
    exp = meta["expected"]
    repair = exp.get("repair", {})
    repair_checks = "\n".join(
        f"  IF (SELECT count(*) FROM document_chunks WHERE department_id = {dept} "
        f"AND coalesce(metadata->>'text_repair', '-') = '{k}') <> {int(v)} THEN "
        f"RAISE EXCEPTION 'STOP: verification failed (repair split {k})'; END IF;"
        for k, v in sorted(repair.items())
    )
    return f"""
-- ===== verification, inside the same transaction =====
DO $$
BEGIN
  IF (SELECT count(*) FROM documents WHERE department_id = {dept}) <> {int(exp["documents"])} THEN
    RAISE EXCEPTION 'STOP: verification failed (documents)'; END IF;
  IF (SELECT count(*) FROM document_chunks WHERE department_id = {dept}) <> {int(exp["document_chunks"])} THEN
    RAISE EXCEPTION 'STOP: verification failed (document_chunks)'; END IF;
  IF EXISTS (SELECT 1 FROM document_chunks WHERE department_id = {dept} AND embedding IS NULL) THEN
    RAISE EXCEPTION 'STOP: verification failed (chunks without an embedding)'; END IF;
  IF EXISTS (SELECT 1 FROM documents d WHERE d.department_id = {dept} AND d.status = 'ready'
             AND NOT EXISTS (SELECT 1 FROM document_chunks c WHERE c.document_id = d.id)) THEN
    RAISE EXCEPTION 'STOP: verification failed (ready documents without chunks)'; END IF;
{repair_checks}
END $$;
SELECT 'documents' AS what, count(*) FROM documents WHERE department_id = {dept}
UNION ALL SELECT 'document_chunks', count(*) FROM document_chunks WHERE department_id = {dept}
UNION ALL SELECT 'repaired chunks', count(*) FROM document_chunks WHERE department_id = {dept}
                 AND metadata->>'text_repair' = 'font_tables';
"""


def check_sql(meta: dict) -> str:
    dept = int(meta["department_id"])
    code = meta["department_code"]
    shipped = ",".join("'" + i.replace("'", "''") + "'" for i in meta["document_ids"])
    active = ",".join(f"'{s}'" for s in PIPELINE_ACTIVE_STATUSES)
    return f"""-- read-only: what the update's stop checks would say. Changes nothing.
SELECT CASE WHEN (SELECT id FROM departments WHERE code = '{code}') = {dept}
            THEN 'OK   department id' ELSE 'STOP department id differs' END
UNION ALL SELECT CASE WHEN EXISTS (SELECT 1 FROM documents WHERE department_id = {dept} AND id NOT IN ({shipped}))
            THEN 'STOP documents we did not ship are present' ELSE 'OK   no foreign documents' END
UNION ALL SELECT CASE WHEN (SELECT coalesce(max(id), 0) FROM nrb_sync_runs) > {int(meta["max_sync_run"])}
                 OR (SELECT coalesce(max(id), 0) FROM nrb_fetch_runs) > {int(meta["max_fetch_run"])}
            THEN 'STOP newer sync runs exist' ELSE 'OK   no newer sync runs' END
UNION ALL SELECT CASE WHEN EXISTS (SELECT 1 FROM nrb_pipeline_runs WHERE status IN ({active}))
            THEN 'STOP a pipeline run is active' ELSE 'OK   no active pipeline run' END;
"""


def _meta(pg: "B.Pg") -> dict:
    dept = int(pg.scalar("SELECT id FROM departments WHERE code = 'nrb'"))
    ids = sorted(filter(None, pg.scalar(
        f"SELECT coalesce(string_agg(id, ','), '') FROM documents WHERE department_id = {dept}").split(",")))
    split = pg.scalar(
        "SELECT coalesce(json_object_agg(k, n), '{}') FROM (SELECT coalesce(metadata->>'text_repair','-') k, "
        f"count(*) n FROM document_chunks WHERE department_id = {dept} GROUP BY 1) s")
    return {
        "department_id": dept, "department_code": "nrb", "document_ids": ids,
        "max_sync_run": int(pg.scalar("SELECT coalesce(max(id), 0) FROM nrb_sync_runs")),
        "max_fetch_run": int(pg.scalar("SELECT coalesce(max(id), 0) FROM nrb_fetch_runs")),
        "expected": {
            "documents": len(ids),
            "document_chunks": int(pg.scalar(f"SELECT count(*) FROM document_chunks WHERE department_id = {dept}")),
            "repair": json.loads(split),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--print-table-flags", action="store_true")
    ap.add_argument("--release-db", default="ai_gateway")
    ap.add_argument("--data")
    ap.add_argument("--out-dir")
    args = ap.parse_args()
    if args.print_table_flags:
        print(" ".join(f"-t {t}" for t in replaced_tables()))
        return 0
    if args.release_db in B.NEVER_TARGET:
        raise SystemExit(f"refusing to read {args.release_db!r}: not a release database")
    from app.config import get_settings

    pg = B.Pg(get_settings().database_url, args.release_db)
    meta = _meta(pg)
    data = Path(args.data).read_text(encoding="utf-8")
    err = Path(args.data).with_suffix(".err")
    if err.exists() and "circular" in err.read_text():
        raise SystemExit("pg_dump reported circular foreign keys: use --disable-triggers (plan Task 16)")
    out = Path(args.out_dir)
    (out / "update.sql").write_text(prologue(meta) + data + epilogue(meta), encoding="utf-8")
    (out / "check.sql").write_text(check_sql(meta), encoding="utf-8")
    (out / "update-meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(json.dumps({k: v for k, v in meta.items() if k != "document_ids"}, indent=2))
    print(f"wrote {out / 'update.sql'} and {out / 'check.sql'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Write `scripts/nrb_update_rehearsal.py`**

```python
#!/usr/bin/env python
"""Rehearse the bank's update on a PG15 copy of what they hold (spec §8.3). Docker; no app DB.

    .venv/bin/python scripts/nrb_update_rehearsal.py --container nrb-pg15 \\
        --delivered /home/manoj/nrb-release/ai_gateway.pg15.2026-09-29.dump \\
        --update /home/manoj/nrb-release/update.sql --check /home/manoj/nrb-release/check.sql \\
        --meta /home/manoj/nrb-release/update-meta.json

Restores the DELIVERED dump (what the bank restored on 2026-09-29), seeds what
they have added since — users, an nrb grant, a chat citing an NRB document, an
upload in another department, a finished pipeline run linked to an NRB job —
then proves:
  * the update loads with foreign keys ENFORCED and every FK has 0 orphans;
  * their users, grant, chat and foreign upload survive, untouched;
  * the NRB counts and repair split equal what was shipped;
  * each stop check, triggered deliberately, halts with the database unchanged.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

SEED = """
INSERT INTO users (email, auth_provider, role, is_active) VALUES
  ('reader1@bank.test', 'ad', 'admin', true), ('reader2@bank.test', 'ad', 'member', true);
INSERT INTO user_departments (user_id, department_id, role)
  SELECT id, (SELECT id FROM departments WHERE code = 'nrb'), 'viewer' FROM users WHERE email = 'reader2@bank.test';
INSERT INTO chat_sessions (id, user_id, department_id)
  SELECT 'rehearsal-session', id, (SELECT id FROM departments WHERE code = 'nrb') FROM users WHERE email = 'reader2@bank.test';
INSERT INTO chat_messages (id, session_id, seq, role, content, sources)
  SELECT 'rehearsal-msg', 'rehearsal-session', 1, 'assistant', 'cites a document',
         jsonb_build_array(jsonb_build_object('document_id', (SELECT min(id) FROM documents)));
INSERT INTO departments (code, name) VALUES ('hr', 'HR');
INSERT INTO documents (id, department_id, title, source, file_type, content_hash, status)
  VALUES ('rehearsalforeignupload0000000001', (SELECT id FROM departments WHERE code = 'hr'),
          'Leave policy', 'upload', 'pdf', repeat('f', 64), 'ready');
INSERT INTO ingest_jobs (id, document_id, status) SELECT 'rehearsaljob00000000000000000001', min(id), 'succeeded'
  FROM documents WHERE department_id = (SELECT id FROM departments WHERE code = 'nrb');
INSERT INTO nrb_pipeline_runs (trigger, status, stage, finished_at) VALUES ('cli', 'succeeded', 'done', now());
INSERT INTO nrb_pipeline_run_jobs (run_id, job_id, document_id, reason)
  SELECT (SELECT max(id) FROM nrb_pipeline_runs), 'rehearsaljob00000000000000000001', document_id, 'created'
  FROM ingest_jobs WHERE id = 'rehearsaljob00000000000000000001';
"""

SURVIVORS = """
SELECT (SELECT count(*) FROM users WHERE email LIKE '%@bank.test')
     || ',' || (SELECT count(*) FROM user_departments)
     || ',' || (SELECT count(*) FROM chat_messages WHERE id = 'rehearsal-msg')
     || ',' || (SELECT count(*) FROM documents WHERE id = 'rehearsalforeignupload0000000001')
"""

ORPHANS = """
SELECT coalesce(sum(n), 0) FROM (
  SELECT count(*) n FROM nrb_files f WHERE f.last_sync_run_id IS NOT NULL
    AND NOT EXISTS (SELECT 1 FROM nrb_sync_runs r WHERE r.id = f.last_sync_run_id)
  UNION ALL SELECT count(*) FROM nrb_files f WHERE f.last_fetch_run_id IS NOT NULL
    AND NOT EXISTS (SELECT 1 FROM nrb_fetch_runs r WHERE r.id = f.last_fetch_run_id)
  UNION ALL SELECT count(*) FROM nrb_sources s WHERE s.last_sync_run_id IS NOT NULL
    AND NOT EXISTS (SELECT 1 FROM nrb_sync_runs r WHERE r.id = s.last_sync_run_id)
  UNION ALL SELECT count(*) FROM nrb_source_files sf WHERE NOT EXISTS (SELECT 1 FROM nrb_files f WHERE f.id = sf.file_id)
     OR NOT EXISTS (SELECT 1 FROM nrb_sources s WHERE s.id = sf.source_id)
  UNION ALL SELECT count(*) FROM nrb_recovery_units u WHERE NOT EXISTS (SELECT 1 FROM nrb_recoveries r WHERE r.id = u.recovery_id)
  UNION ALL SELECT count(*) FROM document_chunks c WHERE NOT EXISTS (SELECT 1 FROM documents d WHERE d.id = c.document_id)
  UNION ALL SELECT count(*) FROM ingest_jobs j WHERE NOT EXISTS (SELECT 1 FROM documents d WHERE d.id = j.document_id)
) s
"""


class Box:
    def __init__(self, container: str) -> None:
        self.container = container

    def psql(self, db: str, sql: str | None = None, *, file: Path | None = None,
             single: bool = False) -> subprocess.CompletedProcess:
        argv = ["docker", "exec", "-i", self.container, "psql", "-U", "postgres", "-d", db,
                "-v", "ON_ERROR_STOP=1", "-X", "-q", "-tA"]
        if single:
            argv.append("-1")
        if file is not None:
            return subprocess.run(argv + ["-f", "-"], input=file.read_bytes(), capture_output=True)
        return subprocess.run(argv + ["-c", sql], capture_output=True)

    def value(self, db: str, sql: str) -> str:
        out = self.psql(db, sql)
        if out.returncode:
            raise SystemExit(f"[{db}] {sql[:60]}… failed: {out.stderr.decode()[:400]}")
        return out.stdout.decode().strip()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--container", default="nrb-pg15")
    ap.add_argument("--delivered", required=True)
    ap.add_argument("--update", required=True)
    ap.add_argument("--check", required=True)
    ap.add_argument("--meta", required=True)
    args = ap.parse_args()
    box = Box(args.container)
    meta = json.loads(Path(args.meta).read_text())
    failures: list[str] = []

    def expect(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'ok  ' if ok else 'FAIL'} {name} {detail}")
        if not ok:
            failures.append(name)

    for db in ("bank_seeded", "bank_copy", "stop_dept", "stop_foreign", "stop_sync", "stop_active"):
        box.value("postgres", f"DROP DATABASE IF EXISTS {db}")
    box.value("postgres", "CREATE DATABASE bank_seeded OWNER gateway")
    restore = subprocess.run(["docker", "exec", "-i", args.container, "pg_restore", "-U", "postgres",
                              "-d", "bank_seeded"], input=Path(args.delivered).read_bytes(), capture_output=True)
    expect("delivered dump restores", restore.returncode == 0, restore.stderr.decode()[:300])
    seeded = box.psql("bank_seeded", SEED)
    expect("bank state seeded", seeded.returncode == 0, seeded.stderr.decode()[:300])
    before = box.value("bank_seeded", SURVIVORS)

    box.value("postgres", "CREATE DATABASE bank_copy TEMPLATE bank_seeded")
    check = box.psql("bank_copy", file=Path(args.check))
    expect("check.sql reports all OK", b"STOP" not in check.stdout, check.stdout.decode())
    update = box.psql("bank_copy", file=Path(args.update), single=True)
    expect("update.sql applies in one transaction", update.returncode == 0, update.stderr.decode()[:600])
    expect("their users, grant, chat and upload survive", box.value("bank_copy", SURVIVORS) == before)
    expect("0 foreign-key orphans", box.value("bank_copy", ORPHANS) == "0")
    dept = meta["department_id"]
    expect("documents match", box.value("bank_copy", f"SELECT count(*) FROM documents WHERE department_id = {dept}")
           == str(meta["expected"]["documents"]))
    expect("chunks match", box.value("bank_copy", f"SELECT count(*) FROM document_chunks WHERE department_id = {dept}")
           == str(meta["expected"]["document_chunks"]))

    stops = {
        "stop_dept": "UPDATE departments SET code = 'nrb_old' WHERE code = 'nrb'; "
                     "INSERT INTO departments (code, name) VALUES ('nrb', 'NRB again')",
        "stop_foreign": f"INSERT INTO documents (id, department_id, title, source, file_type, content_hash, status) "
                        f"VALUES ('adminupload00000000000000000001', {dept}, 'their own', 'upload', 'pdf', "
                        f"repeat('e', 64), 'ready')",
        "stop_sync": "INSERT INTO nrb_sync_runs (status) VALUES ('running')",
        "stop_active": "INSERT INTO nrb_pipeline_runs (trigger, status, stage) VALUES ('api', 'queued', 'queued')",
    }
    count_sql = f"SELECT count(*) FROM document_chunks WHERE department_id = {dept}"
    for db, mutate in stops.items():
        box.value("postgres", f"CREATE DATABASE {db} TEMPLATE bank_seeded")
        box.psql(db, mutate)
        chunks_before = box.value(db, count_sql)
        out = box.psql(db, file=Path(args.update), single=True)
        halted = out.returncode != 0 and b"STOP:" in out.stderr
        expect(f"{db} halts", halted, out.stderr.decode()[:200])
        expect(f"{db} leaves the database unchanged", box.value(db, count_sql) == chunks_before)

    print("REHEARSAL:", "PASS" if not failures else f"FAIL {failures}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

Checked against the schema on 2026-10-02:
- every NOT NULL column of `nrb_sync_runs` has a default (so `INSERT … (status) VALUES ('running')` is valid);
- the foreign-key columns are `nrb_files.last_sync_run_id`, `nrb_files.last_fetch_run_id`, `nrb_sources.last_sync_run_id`, `nrb_source_files.{file_id,source_id}` and `nrb_recovery_units.recovery_id`.

- [ ] **Step 5: Run the unit tests, the full suite (G11), and commit**

Run: `.venv/bin/pytest tests/test_nrb_update_sql.py -v` → all passed.
```bash
git add scripts/build_nrb_update_sql.py scripts/nrb_update_rehearsal.py tests/test_nrb_update_sql.py
git commit -m "feat(release): the bank's atomic data-only NRB update, and its rehearsal

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 6: Generate the update and rehearse it (G10)**

```bash
docker exec nrb-pg15 pg_dump -U postgres -d ai_gateway --data-only --format=plain \
  $(.venv/bin/python scripts/build_nrb_update_sql.py --print-table-flags) \
  > "$SCRATCH/update-data.sql" 2> "$SCRATCH/update-data.err"; cat "$SCRATCH/update-data.err"
.venv/bin/python scripts/build_nrb_update_sql.py --release-db ai_gateway --data "$SCRATCH/update-data.sql" \
  --out-dir /home/manoj/nrb-release
systemd-run --user --wait --collect --unit=native3-rehearsal --working-directory="$PWD" \
  -p StandardOutput=file:"$SCRATCH/rehearsal.log" -p StandardError=file:"$SCRATCH/rehearsal.log" \
  .venv/bin/python scripts/nrb_update_rehearsal.py --container nrb-pg15 \
  --delivered /home/manoj/nrb-release/ai_gateway.pg15.2026-09-29.dump \
  --update /home/manoj/nrb-release/update.sql --check /home/manoj/nrb-release/check.sql \
  --meta /home/manoj/nrb-release/update-meta.json
cat "$SCRATCH/rehearsal.log"
```
Expected: `update-data.err` is empty (no "circular"), every line `ok`, and `REHEARSAL: PASS`.

- If pg_dump reports circular foreign keys, re-dump with `--disable-triggers`. That emits `ALTER TABLE … DISABLE TRIGGER ALL` around each COPY, which needs `postgres`, and the bank runs as `postgres`. Re-run; the orphan count is then the proof.
- **A FAIL is a stop:** report it.

- [ ] **Step 7: Record §31.6 and commit**

Add §31.6, covering:
- the rehearsal's lines;
- the FK list;
- whether `--disable-triggers` was needed;
- the four stop checks proven.

```bash
git add docs/nrb-integration.md
git commit -m "docs(nrb): the bank update rehearsal (§31.6)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 17: `UPDATE.md`: the bank's copy-paste steps

**Files:**
- Create: `/home/manoj/nrb-release/UPDATE.md` (outside the repo, shipped with the update)
- Modify: `/home/manoj/nrb-release/SHA256SUMS`

- [ ] **Step 1: Write `/home/manoj/nrb-release/UPDATE.md`**

Fill each `«…»` from `update-meta.json` and the Task 15/16 outputs; the bracketed `<…>` values are theirs to fill in. Use this content:

````markdown
# NRB knowledge-base update (native-3 text repair)

Send these steps ONE AT A TIME. Each shows the output to expect; stop and reply
with the actual output if it differs. Nothing here deletes your users, grants,
chats or other departments' documents. If any check fails, the update changes
nothing (it runs as one transaction).

**Values you need (find each once):**
- `<postgres-container>` — run `docker ps --format '{{.Names}}\t{{.Image}}'`; it is the
  name on the row whose image contains `pgvector` or `postgres`.
- `<worker-container>`, `<nrb-runner-container>` — the same list; the rows for the
  ingest worker and the NRB runner.
- `<gateway-container>` — the same list; the gateway API.
- `<database>` — the database the gateway uses (the one restored on 2026-09-29).
- `<nrb_files host path>` — run
  `docker inspect <gateway-container> --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'`;
  it is the left side of the line ending `/app/nrb_files`.

## 1. Stop the worker and the NRB runner
    docker stop <worker-container> <nrb-runner-container>
Expected: the two names printed back. (The gateway itself stays up.)

## 2. Back up the database first
    docker exec <postgres-container> pg_dump -U postgres -Fc <database> > before-nrb-update.dump
    ls -lh before-nrb-update.dump
Expected: a file of several hundred MB. This is how to undo the update.

## 3. Copy the two scripts into the database container
    docker cp update.sql <postgres-container>:/tmp/update.sql
    docker cp check.sql <postgres-container>:/tmp/check.sql
Expected: no output.

## 4. Read-only check
    docker exec <postgres-container> psql -U postgres -d <database> -tA -f /tmp/check.sql
Expected exactly:
    OK   department id
    OK   no foreign documents
    OK   no newer sync runs
    OK   no active pipeline run
If any line says STOP, do not continue — send that line.

## 5. Apply the update (one transaction)
    docker exec <postgres-container> psql -U postgres -d <database> -1 -v ON_ERROR_STOP=1 -q -f /tmp/update.sql
Expected, at the end:
    documents|«documents»
    document_chunks|«document_chunks»
    repaired chunks|«font_tables»
An error containing `STOP:` means nothing was changed — send it.

## 6. Confirm the files are all present
    cd <nrb_files host path> && for h in $(docker exec <postgres-container> psql -U postgres -d <database> -tA \
      -c "SELECT content_hash FROM documents WHERE metadata->>'origin'='nrb'"); do ls ${h:0:2}/$h.* >/dev/null 2>&1 || echo MISSING $h; done; echo done
Expected: `done` and no MISSING lines (this update ships no new files).

## 7. Start the worker and the NRB runner again
    docker start <worker-container> <nrb-runner-container>

## 8. Smoke question
In the `nrb` tab, ask: «a §9.8 question about the Consumer Protection Act». The answer should cite the Act,
with a "machine-recovered — VERIFY" note on its source.

## Important until your gateway code is updated
Your current gateway code does not know about the repair. If a repaired document
is re-ingested on your side (a republished NRB file, or a manual retry), your
worker will rebuild it from the PDF's original, garbled text. Avoid re-ingesting
NRB documents until the code update that includes the repair is deployed.
````

- [ ] **Step 2: Update the checksums**

```bash
cd /home/manoj/nrb-release && sha256sum ai_gateway.pg15.dump update.sql check.sql UPDATE.md > SHA256SUMS.native3 && cat SHA256SUMS.native3
```
Keep the 2026-09-29 `SHA256SUMS` untouched; the new file covers this delivery.

- [ ] **Step 3: STOP — show the user `UPDATE.md`.** Sending it to the bank is the user's action, not ours.

---

### Task 18: Docs, the flag's default, and the record

**Files:**
- Modify: `app/config.py`, `.env.example` (the default flips to `true`)
- Modify: `docs/nrb-integration.md` (§31.7, Evaluation & Improvement), `docs/nrb-usage.md`, `docs/prod-incident-2026-09-20.md` (§9.12), `CLAUDE.md`
- Test: `tests/test_nrb_native_repair_cache.py`

- [ ] **Step 1: Change the default-flag test first**

In `tests/test_nrb_native_repair_cache.py`, change `test_the_flag_defaults_off` to:
```python
def test_the_flag_defaults_on_after_the_readers_gate():
    """Flipped in plan Task 18, after §31.3 recorded a PASS (spec §8.1)."""
    from app.config import Settings

    assert Settings.model_fields["nrb_native_repair"].default is True
```
Run: `.venv/bin/pytest tests/test_nrb_native_repair_cache.py::test_the_flag_defaults_on_after_the_readers_gate -v` → FAIL.

- [ ] **Step 2: Flip the default**

In `app/config.py` set `nrb_native_repair: bool = True`, and change the comment's "OFF until the Nepali reader's gate passes" to "ON since the Nepali reader's gate passed (docs/nrb-integration.md §31.3)". In `.env.example` set `NRB_NATIVE_REPAIR=true`, with the matching comment. Re-run the test → PASS.

- [ ] **Step 3: Write the documentation**

- `docs/nrb-integration.md` §31.7, **Evaluation & Improvement**, the four org-required questions:
  1. **Success metric:** the share of served chunks still carrying the ToUnicode garbling, by SQL over `text_repair`. Give the before/after numbers from §31.4.
  2. **Eval:** the 8 labelled cases in `tests/test_nrb_fontrepair_eval.py` (written in this step, below), the cohort (§31.2) and the reader (§31.3), with their current pass rates.
  3. **Feedback capture:** unit `detail`, chunk `text_repair`/`repair_engine`/`authoritative`, the worker log line, `nrb_recovery_cache.py --stats`, `docs/nrb/native3-review.json`.
  4. **Review loop:** on every repair-version bump, re-run the evidence pass and give the reader a session on changed cells; before each bank delivery and monthly while their files arrive, run the detector over new documents.
  Also record the open decision on lifting `authoritative: false` for repaired pages, and the licence note (spec §3.4: fontTools MIT; uharfbuzz Apache-2.0 bundling HarfBuzz "Old MIT"; keep the NOTICE in the worker image) beside §12's npttf2utf note.
- `tests/test_nrb_fontrepair_eval.py`: spec §9.2's 8 cases against real blobs in the filestore. Each case is skipped when its blob is absent, via `pytest.mark.skipif(not path.exists())`.
  - Cases 1–3: `2b11bf653aa2` p.14, the NI Act p.3 (`35445ee37706`) and the §9.8 directive page, each expecting `font_tables`. The exact-match reference lines come from the reader-confirmed rows in `docs/nrb/native3-review.json`, matched by sha and page. Only lines the reader marked `correct` are used.
  - Case 4: `075bf12eb087`, expecting `font_tables` or the recorded finding from §31.1.
  - Case 5: a Word-365 page (`009878a64157` p.1), expecting `unrepaired:coverage` in Phase 1.
  - Cases 6–7: a clean Distiller page and an English native page, from §31.2's undetected list, expecting passthrough via `recover` with `repair=None` vs the engine.
  - Case 8: a Preeti page (`e08988860534` p.2), routed `legacy_conversion`, so `native_unit` is never called.
- `docs/nrb-usage.md`: a "native-3 text repair" section covering the flag, the detect script, re-ingesting by id, verifying by repair split, and the warning that it needs uharfbuzz in the worker image.
- `docs/prod-incident-2026-09-20.md` §9.12: B is built and delivered as an update (when sent), with pointers to §31.
- `CLAUDE.md`:
  - one "Conventions / gotchas" bullet: *native-3 is the native route's ENGINE (no base change, no migration); density detects, the HarfBuzz round trip accepts; rule 6 keeps an unrepaired page's text non-authoritative; one caveat predicate (`sources.is_machine_recovered`); old-spec `deva` fonts form the below-base ra from [र, ्]*;
  - in the status paragraph, point "Still open before it ships" at §31.

- [ ] **Step 4: Run the full suite (G11), then commit**

```bash
git add app/config.py .env.example tests/test_nrb_native_repair_cache.py tests/test_nrb_fontrepair_eval.py \
        docs/nrb-integration.md docs/nrb-usage.md docs/prod-incident-2026-09-20.md CLAUDE.md
git commit -m "feat(nrb): native-3 on by default after the reader's gate; docs and E&I (§31.7)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 5: Hand over**

Report to the user:
- the branch and its commits;
- §31's numbers;
- the release artefacts in `/home/manoj/nrb-release/`;
- that pushing, merging and sending `UPDATE.md` to the bank are theirs to decide.

Phase 2 (donor GSUB, spec §7) gets its own plan.
````

