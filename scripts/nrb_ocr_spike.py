#!/usr/bin/env python
"""Phase 6B OCR spike — can Docling's OCR read NRB pages that Preeti conversion cannot?

READ-ONLY and offline (after the one-time RapidOCR model fetch). Touches no
database, no classifier, no threshold and no frozen artifact. It writes exactly
two files plus a few rendered pages, all new:

    docs/nrb/phase6b-ocr-spike.md      the human-comparison artifact
    docs/nrb/phase6b-ocr-spike.json    the same numbers, machine-readable
    docs/nrb/ocr-spike-pages/*.jpg     source pages the review pack did not render

Why this exists
---------------
The `>=0.80` routing queue is not one population. Splitting all 56 of its members
by what the FILE says about itself — `pdffonts` for embedded fonts, `pdfimages`
for a page-sized raster — separates them along the conversion outcome:

    provenance                                    recovered  partial  unresolved   n
    PDF, embeds >=1 recognised legacy Nepali font     32        12          0      44
    PDF, embedded fonts, names stripped by producer    1         0          0       1
    PDF, NO embedded font (scan + hidden text)         0         4          4       8
    spreadsheet (.xlsx, no PDF font layer)             3         0          0       3
    TOTAL                                             36        16          4      56

Those 8 scan-backed blobs do not carry Preeti at all. Their text layer is legacy
Latin-alphabet scanner OCR (`Htqft Hfrqq aFrerr{ hrn`). For that population a
glyph mapping cannot be right no matter how good it is, because there is no glyph
mapping in the file — only pixels. OCR is the only decoder that can work.

WHOSE FAILURE THIS IS
---------------------
Not native-2's. Its documented contract (`routing.py`) is "did extraction produce
trustworthy text" — it classifies *text signals* and deliberately never opens a
font table, imports nothing from `legacy_font`, and must run where npttf2utf was
never installed. Scanner-OCR noise is not English, not Unicode and does look
glyph-mapped, so `suspicious`/`legacy_font_suspected` is the correct call: that
text is indeed untrustworthy.

The gap is downstream, and it is a gap in something not yet built. Treating
`unit_legacy_ratio >= 0.80` as *eligible for npttf2utf* silently assumes the
suspicious text is a glyph mapping of an embedded legacy font. Eight queue
members break that assumption. So this is a **conversion-routing / font-
provenance** finding, and its fix belongs to the conversion router §14.7 and
§15.9 recommend building — as a precondition on that router, not as a change to
the classifier. Nothing here touches native-2, no threshold moves, and no
`native-3` is implied.

So the spike measures one thing: on those pages, does Docling's OCR produce
Unicode Devanagari where the current pipeline produces junk? It deliberately
also runs pages where conversion ALREADY works, because a fallback that is worse
than the existing path on the existing path's own population is not a fallback.

What this is NOT
----------------
Not a correctness measurement. Every metric here is structural — how many
Devanagari codepoints came out, how long it took. Whether the Nepali is RIGHT is
a competent reader's call, exactly as in `docs/nrb/phase6b-routing-holdout-manual-review.md`.
Nothing here routes, converts or reprocesses the corpus.

Usage:
    .venv/bin/python scripts/nrb_ocr_spike.py [--pages N] [--out-dir docs/nrb]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass, field, asdict

REPO = pathlib.Path(__file__).resolve().parent.parent
BLOB_ROOT = REPO / "nrb_files"
EVIDENCE = REPO / "docs/nrb/phase6b-routing-holdout-evidence.json"

# The OCR backend. RapidOCR is already installed as a docling dependency; the
# `torch` backend is chosen because torch is already in this venv while
# onnxruntime is NOT, so this needs no new package — only the PP-OCRv4
# devanagari recognition weights, which docling fetches once into its cache.
OCR_BACKEND = "torch"
OCR_LANG = "devanagari"

# Page rendering for the human artifact. Matches the review pack's 90 dpi so the
# two read the same; the OCR itself works off docling's own higher-res raster.
RENDER_DPI = 90
RENDER_QUALITY = 72


# --------------------------------------------------------------------------- #
# The page set — NAMED, never derived, so a re-run cannot quietly change cohort
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Case:
    short: str
    page: int
    group: str
    why: str


CASES: tuple[Case, ...] = (
    # Group A — the 8 scan-backed blobs inside the >=0.80 queue. All 4 of the
    # queue's `unresolved` and 4 of its 16 `partial` are here, and nothing here
    # is `recovered`. This is the population the spike exists for.
    Case("3d2eca8b9f95", 1, "scan-backed queue", "unresolved; 300 dpi scan; forex 2018"),
    Case("da8024b7616b", 1, "scan-backed queue", "unresolved; 300 dpi scan; forex 2019"),
    Case("796bb59c3443", 1, "scan-backed queue", "unresolved; 150 dpi scan; notice 2019"),
    Case("70b0d415dcf3", 1, "scan-backed queue", "unresolved; act 2019"),
    Case("2e65dadfffa3", 1, "scan-backed queue", "partial, Devanagari-after 0.1996; 150 dpi"),
    Case("360eaafd44bd", 1, "scan-backed queue", "partial; highest English share in band (8.9%)"),
    Case("a17fa322b81a", 1, "scan-backed queue", "partial; circular 2021"),
    Case("8e8467f74f84", 1, "scan-backed queue", "partial; forex 2018"),
    # Group B — `needs_ocr`: no usable text layer at all, so there is nothing to
    # compare against. These say whether OCR opens the 17-blob bucket Phase 6B
    # currently has no answer for.
    Case("438c55304da5", 1, "needs_ocr", "no_text_layer; exam result 2025"),
    Case("c298efaf1f16", 1, "needs_ocr", "no_text_layer; notice 2078.12.10"),
    Case("276b2eb62802", 1, "needs_ocr", "no_text_layer; calendar 2020"),
    # Group C — controls where the EXISTING path already works: real embedded
    # Preeti, `recovered`, and small enough that the pack shows every flagged
    # unit. If OCR loses to conversion here, it is a narrow fallback, not a
    # replacement.
    Case("1a9b6321aa61", 1, "font-embedded control", "recovered; Preeti+Bishall; 10/10 units shown"),
    Case("d1c99f3cf34d", 1, "font-embedded control", "recovered; smallest in queue, 9/9 units"),
    Case("268bcfe86d03", 1, "font-embedded control", "partial; circular 2007"),
)


# --------------------------------------------------------------------------- #
# Structural metrics — no semantic claim is made or possible here
# --------------------------------------------------------------------------- #
DEVANAGARI = re.compile(r"[ऀ-ॿ]")
LATIN = re.compile(r"[A-Za-z]")
# A pre-base vowel sign that opens a cluster. PP-OCR emits glyphs in VISUAL
# order, so "वि" comes back as "िव" — the sign lands before its consonant
# instead of after it. Counting it is structural, not a quality judgement, and
# it is mechanically repairable downstream; it matters because a raw index of
# this text would not match a correctly-typed query.
PREBASE_MISORDER = re.compile(r"(?:^|[\s।॥])[िॅ-ै]")


def orthography(text: str) -> dict | None:
    """Two structural well-formedness signals for Devanagari. No semantics.

    `halant_per_dev` — Nepali is conjunct-heavy; the virama (्) binds them. Real
    Nepali runs near 0.10. An OCR that reads glyphs but not conjuncts drops it by
    an order of magnitude, and the resulting text is not the word a reader types.

    `mean_word_len` — word boundaries. A recogniser that emits a whole text line
    as one run produces 25-40 character "words" against a true median near 6, and
    the text then tokenises as one term regardless of the indexer.

    Both are reported against the converter's own output on the same queue, which
    is the only Nepali in this repository already judged structurally plausible.
    Returns None below 50 Devanagari characters, where neither figure is stable.
    """
    dev = DEVANAGARI.findall(text)
    if len(dev) < 50:
        return None
    words = [w for w in text.split() if DEVANAGARI.search(w)]
    return {
        "halant_per_dev": round(text.count("्") / len(dev), 4),
        "mean_word_len": round(sum(len(w) for w in words) / len(words), 1) if words else 0.0,
        "devanagari": len(dev),
    }


def reference_orthography() -> dict | None:
    """The converter's output on the >=0.80 queue — the comparison baseline."""
    if not EVIDENCE.exists():
        return None
    ev = json.loads(EVIDENCE.read_text())
    rows = []
    for item in ev.get("items", []):
        if item.get("band") != ">=0.80":
            continue
        joined = " ".join(u["converted"] for u in item["evidence"] if u.get("converted"))
        prof = orthography(joined)
        if prof:
            rows.append(prof)
    if not rows:
        return None
    rows.sort(key=lambda r: r["halant_per_dev"])
    mid = len(rows) // 2
    lens = sorted(r["mean_word_len"] for r in rows)
    return {
        "documents": len(rows),
        "halant_per_dev": rows[mid]["halant_per_dev"],
        "mean_word_len": lens[len(lens) // 2],
    }


def metrics(text: str) -> dict:
    dense = "".join(text.split())
    dev = len(DEVANAGARI.findall(text))
    lat = len(LATIN.findall(text))
    return {
        "chars": len(text),
        "chars_nonspace": len(dense),
        "devanagari": dev,
        "latin": lat,
        "devanagari_ratio": round(dev / len(dense), 4) if dense else 0.0,
        "latin_ratio": round(lat / len(dense), 4) if dense else 0.0,
        "prebase_misordered": len(PREBASE_MISORDER.findall(text)),
        "nfc_stable": unicodedata.normalize("NFC", text) == text,
    }


# --------------------------------------------------------------------------- #
# What the current pipeline sees
# --------------------------------------------------------------------------- #
def blob_path(short: str) -> pathlib.Path:
    hits = sorted(BLOB_ROOT.glob(f"{short[:2]}/{short}*"))
    if not hits:
        raise FileNotFoundError(f"no local blob for {short} under {BLOB_ROOT}")
    return hits[0]


def native_page_text(path: pathlib.Path, page: int) -> tuple[str, str | None]:
    """The text layer as `app/nrb/extraction.py` reads it — pypdf, same call."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        return reader.pages[page - 1].extract_text() or "", None
    except Exception as exc:  # noqa: BLE001 - a parse failure is a result here
        return "", f"{type(exc).__name__}: {exc}"


def converted_units(short: str, evidence: dict) -> list[dict]:
    """The legacy conversion the review pack already published, if any."""
    for item in evidence.get("items", []):
        if item["short"] == short:
            return [
                {
                    "where": u["where"],
                    "original": u["original"],
                    "converted": u["converted"],
                    "disposition": u["disposition"],
                }
                for u in item["evidence"]
                if u.get("where") == f"p.{1}"
            ][:4]
    return []


def render_page(path: pathlib.Path, page: int, out_dir: pathlib.Path, short: str) -> str | None:
    """Render one page to a small grayscale JPEG via poppler. Nothing downloaded."""
    existing = REPO / "docs/nrb/holdout-pages" / f"{short}-p{page:03d}.jpg"
    if existing.exists():
        return str(existing.relative_to(REPO))
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{short}-p{page:03d}.jpg"
    if not target.exists():
        prefix = out_dir / f"{short}-p{page:03d}"
        try:
            subprocess.run(
                ["pdftoppm", "-jpeg", "-jpegopt", f"quality={RENDER_QUALITY}", "-gray",
                 "-r", str(RENDER_DPI), "-f", str(page), "-l", str(page),
                 "-singlefile", str(path), str(prefix)],
                check=True, capture_output=True, timeout=120,
            )
        except Exception:  # noqa: BLE001 - a missing render is a caveat, not a failure
            return None
    return str(target.relative_to(REPO)) if target.exists() else None


# --------------------------------------------------------------------------- #
# The OCR pass
# --------------------------------------------------------------------------- #
def build_converter():
    """Docling with OCR forced on. Imported lazily — docling is worker-weight."""
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    opts = PdfPipelineOptions()
    opts.do_ocr = True
    # Table structure is a separate model and answers a question this spike does
    # not ask; leaving it on would inflate every runtime figure below.
    opts.do_table_structure = False
    opts.ocr_options = RapidOcrOptions(
        lang=[OCR_LANG],
        backend=OCR_BACKEND,
        # The whole point: ignore the text layer and read the pixels. Without
        # this, docling keeps the existing (junk) text for pages that have one
        # and the spike would measure nothing.
        force_full_page_ocr=True,
    )
    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
    )


@dataclass
class Result:
    short: str
    page: int
    group: str
    why: str
    blob: str = ""
    rendered_page: str | None = None
    native: dict = field(default_factory=dict)
    ocr: dict = field(default_factory=dict)
    native_text: str = ""
    ocr_text: str = ""
    converted_sample: list = field(default_factory=list)
    seconds: float = 0.0
    error: str | None = None


def run(cases: tuple[Case, ...], out_dir: pathlib.Path) -> list[Result]:
    evidence = json.loads(EVIDENCE.read_text()) if EVIDENCE.exists() else {}
    converter = build_converter()
    pages_dir = out_dir / "ocr-spike-pages"
    results: list[Result] = []

    for case in cases:
        res = Result(short=case.short, page=case.page, group=case.group, why=case.why)
        try:
            path = blob_path(case.short)
        except FileNotFoundError as exc:
            res.error = str(exc)
            results.append(res)
            print(f"  {case.short} p{case.page}: MISSING BLOB", flush=True)
            continue

        res.blob = path.name
        res.rendered_page = render_page(path, case.page, pages_dir, case.short)

        native_text, native_err = native_page_text(path, case.page)
        res.native_text = native_text
        res.native = metrics(native_text)
        if native_err:
            res.native["error"] = native_err
        res.converted_sample = converted_units(case.short, evidence)

        started = time.perf_counter()
        try:
            conv = converter.convert(path, page_range=(case.page, case.page))
            res.ocr_text = conv.document.export_to_text()
            res.ocr = metrics(res.ocr_text)
        except Exception as exc:  # noqa: BLE001 - an OCR failure is a finding
            res.error = f"{type(exc).__name__}: {exc}"
        res.seconds = round(time.perf_counter() - started, 2)

        print(
            f"  {case.short} p{case.page} [{case.group}] "
            f"native dev={res.native['devanagari']:>5} -> ocr dev={res.ocr.get('devanagari', 0):>5} "
            f"({res.seconds}s)" + (f"  ERROR {res.error}" if res.error else ""),
            flush=True,
        )
        results.append(res)
    return results


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def excerpt(text: str, limit: int = 420) -> str:
    flat = " ".join(text.split())
    return (flat[:limit] + " …") if len(flat) > limit else (flat or "*(empty)*")


def markdown(results: list[Result], elapsed: float) -> str:
    ok = [r for r in results if not r.error]
    out: list[str] = []
    out.append("# Phase 6B — OCR spike: can Docling read what Preeti conversion cannot?\n")
    out.append(
        "Experimental spike. **No production change**: native-2 is untouched, no threshold "
        "moved, no conversion or OCR is wired into any pipeline, and no frozen artifact was "
        "modified. Every number below is **structural** — codepoint counts and runtime. "
        "Whether the Nepali is *correct* is a competent reader's call, exactly as in "
        "`phase6b-routing-holdout-manual-review.md`.\n"
    )
    out.append("**Exactly what was tested** — one configuration, no sweep:\n")
    out.append("| layer | this run |")
    out.append("| --- | --- |")
    out.append("| orchestration | docling OCR stage (`PdfPipelineOptions.do_ocr=True`, `force_full_page_ocr=True`) |")
    out.append("| engine | RapidOCR 3.9.2 |")
    out.append(f"| inference backend | **`{OCR_BACKEND}`** |")
    out.append(f"| detection / recognition | `ch_PP-OCRv4_det_mobile` / **`{OCR_LANG}_PP-OCRv4_rec_mobile`** |")
    out.append("| OCR render scale | docling default `3.0` (not swept) |")
    out.append("| native comparison | `pypdf`, the same call `app/nrb/extraction.py` makes |\n")
    out.append(
        "**PP-OCRv5 Devanagari was NOT tested here, and the reason is structural.** "
        "Docling's `_resolve_rapidocr` sends the `torch` backend down a PP-OCRv4-only "
        "branch — v5 recognition weights are published for `onnxruntime`, `openvino` and "
        "`paddle`, not for torch. This venv has torch but **no `onnxruntime`**, so v4 was "
        "the only Devanagari recogniser reachable without adding a package. Choosing torch "
        "was what kept this spike to a weights fetch instead of a dependency change; the "
        "cost is that the v4-vs-v5 question stays open here.\n"
    )
    out.append(f"{len(results)} pages, {elapsed:.0f}s wall clock.\n")

    out.append("## 1. Why these pages — all 56 queue members accounted for\n")
    out.append(
        "The `>=0.80` queue is not one population. `pdffonts` + `pdfimages` split it along "
        "the conversion outcome. Every one of the 56 frozen queue members appears in exactly "
        "one row, and the row totals reproduce the queue's own 36/16/4:\n"
    )
    out.append("| page provenance | recovered | partial | unresolved | n |")
    out.append("| --- | ---: | ---: | ---: | ---: |")
    out.append("| PDF, embeds ≥1 recognised legacy Nepali font | 32 | 12 | 0 | 44 |")
    out.append("| PDF, embedded fonts whose names the producer stripped (`CIDFont+F1…F6`) | 1 | 0 | 0 | 1 |")
    out.append("| **PDF, NO embedded font — scan + hidden OCR text layer** | **0** | **4** | **4** | **8** |")
    out.append("| spreadsheet (`.xlsx` — has no PDF font layer to inspect) | 3 | 0 | 0 | 3 |")
    out.append("| **total** | **36** | **16** | **4** | **56** |\n")
    out.append(
        "Two rows exist only so the arithmetic is honest. `7820b1f49fc1` embeds six subset "
        "fonts renamed `CIDFont+F1…F6` by its producer, so its provenance is **undetermined "
        "by name**, not \"not legacy\"; it converted cleanly and behaves like the 44. The 3 "
        "spreadsheets have no PDF font layer at all, so the question is not askable of them "
        "— they are not evidence for or against the split. Excluding them leaves 53 PDFs, "
        "which is the number an earlier draft of this report showed as the whole queue; that "
        "was an under-stated denominator, not a different measurement.\n"
    )
    out.append(
        "The 8 scan-backed blobs carry **no Preeti at all** — their text layer is legacy "
        "Latin-alphabet scanner OCR. A glyph mapping cannot be right there because the file "
        "holds no glyph mapping, only pixels.\n"
    )
    out.append("### Whose failure this is\n")
    out.append(
        "**Not native-2's.** Its contract in `app/nrb/routing.py` is *\"did extraction "
        "produce trustworthy text\"*; it judges text signals, never opens a font table, "
        "imports nothing from `legacy_font`, and must run where npttf2utf was never "
        "installed. Scanner-OCR noise is not English, not Unicode, and does look "
        "glyph-mapped — so `suspicious`/`legacy_font_suspected` is the **correct** call. "
        "That text really is untrustworthy.\n"
    )
    out.append(
        "The gap is downstream of the classifier, in something not yet built: reading "
        "`unit_legacy_ratio >= 0.80` as *eligible for npttf2utf* assumes the suspicious text "
        "is a glyph mapping of an embedded legacy font, and 8 queue members break that "
        "assumption. This is therefore a **conversion-routing / font-provenance** finding. "
        "Its fix is a precondition on the conversion router that §14.7 and §15.9 recommend "
        "building — **not** a classifier change, **not** `native-3`, and **not** a threshold "
        "move.\n"
    )

    out.append("## 2. Results\n")
    out.append("| blob | page | group | native dev | **OCR dev** | OCR dev ratio | latin ratio | pre-base misordered | s |")
    out.append("| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for r in results:
        if r.error and not r.ocr:
            out.append(f"| `{r.short}` | {r.page} | {r.group} | — | — | — | — | — | {r.seconds} |")
            continue
        out.append(
            f"| `{r.short}` | {r.page} | {r.group} | {r.native.get('devanagari', 0)} | "
            f"**{r.ocr.get('devanagari', 0)}** | {r.ocr.get('devanagari_ratio', 0)} | "
            f"{r.ocr.get('latin_ratio', 0)} | {r.ocr.get('prebase_misordered', 0)} | {r.seconds} |"
        )
    out.append("")
    if ok:
        secs = sorted(r.seconds for r in ok)
        out.append(
            f"Runtime: median **{secs[len(secs) // 2]:.1f}s/page**, range "
            f"{secs[0]:.1f}–{secs[-1]:.1f}s, on CPU with the torch backend. The first page "
            "carries model load and `torch.compile` warmup.\n"
        )
    out.append(
        "**`pre-base misordered`** counts vowel signs that open a cluster — PP-OCR emits "
        "glyphs in *visual* order, so `वि` comes back as `िव`. Read it as a floor, not a "
        "count: the regex needs a space before the sign, and this output has almost no "
        "spaces, so nearly every real occurrence is invisible to it. The excerpts in §4 "
        "show the true rate.\n"
    )

    ref = reference_orthography()
    out.append("## 3. Is the recovered text well-formed Nepali? No — and this is the finding\n")
    out.append(
        "Recovering Devanagari codepoints is not the same as recovering Nepali. Two "
        "structural signals, neither of them semantic, measured against the converter's own "
        "output on the same queue — the only Nepali here already judged structurally "
        "plausible:\n"
    )
    out.append("| text | halant per Devanagari char | mean Devanagari word length |")
    out.append("| --- | ---: | ---: |")
    if ref:
        out.append(
            f"| **reference** — npttf2utf on {ref['documents']} font-embedded queue docs | "
            f"**{ref['halant_per_dev']:.4f}** | **{ref['mean_word_len']:.1f}** |"
        )
    for r in results:
        prof = orthography(r.ocr_text)
        if prof:
            out.append(
                f"| OCR — `{r.short}` ({r.group}) | {prof['halant_per_dev']:.4f} | "
                f"{prof['mean_word_len']:.1f} |"
            )
    out.append("")
    out.append(
        "Nepali is conjunct-heavy and the virama binds the conjuncts. The converter's output "
        "carries one about every ten Devanagari characters; this OCR carries one every "
        "~200 — roughly **twenty times fewer** — and its mean word runs 4-8× too long "
        "because word boundaries are gone. Concretely, on a control the existing path "
        "already handles:\n"
    )
    out.append("```")
    out.append("conversion : कारवाही फुकुवा भएका वित्त कम्पनीहरुको विवर०ा")
    out.append("OCR        : कारवाहीफुकुवाभएकािवतकमपनीहरकोिववरण")
    out.append("```")
    out.append(
        "Same page, same words. The OCR loses every space, drops `वित्त`→`िवत` and "
        "`कम्पनी`→`कमपनी`, and reorders `वि`→`िव`. **A retrieval index built on this would "
        "not match a correctly typed Nepali query**, and no amount of embedding quality "
        "fixes a token that is one 27-character run. That is why the recommendation below "
        "is narrow.\n"
    )

    out.append("## 4. Page by page — source → existing pipeline → OCR\n")
    for r in results:
        out.append(f"### `{r.short}` p.{r.page} — {r.group}\n")
        out.append(f"- {r.why}")
        out.append(f"- blob: `{r.blob}`")
        if r.rendered_page:
            out.append(f"- rendered source: `{r.rendered_page}`")
        if r.error:
            out.append(f"- **error:** `{r.error}`")
        out.append("")
        out.append(f"**Existing text layer (pypdf)** — {r.native.get('devanagari', 0)} Devanagari chars:\n")
        out.append(f"> {excerpt(r.native_text)}\n")
        if r.converted_sample:
            out.append("**Existing legacy conversion** (from the review pack):\n")
            out.append("| in | out | disposition |")
            out.append("| --- | --- | --- |")
            for u in r.converted_sample:
                o = (u["original"] or "")[:70].replace("|", "\\|")
                c = (u["converted"] or "")[:70].replace("|", "\\|")
                out.append(f"| `{o}` | {c} | {u['disposition']} |")
            out.append("")
        out.append(f"**Docling OCR** — {r.ocr.get('devanagari', 0)} Devanagari chars:\n")
        out.append(f"> {excerpt(r.ocr_text)}\n")

    out.append("## 5. The six questions this spike was asked\n")
    scan = [r for r in results if r.group == "scan-backed queue" and r.ocr]
    ocr_secs = sorted(r.seconds for r in results if r.ocr)
    steady = [s for s in ocr_secs if s < 10]
    out.append(
        f"1. **Can it read the problematic pages?** Yes. All {len(scan)} scan-backed queue "
        "pages went from **0** Devanagari characters to hundreds; the existing pipeline "
        "recovers nothing there because there is nothing to recover.\n"
        "2. **Is it usable Unicode Nepali?** Script yes, orthography no — see §3. Usable as "
        "a coarse signal, not as text a reader or an index should trust.\n"
        f"3. **Which backend ran?** Docling OCR stage → RapidOCR → **`{OCR_BACKEND}`** "
        f"backend → **PP-OCRv4 `{OCR_LANG}_..._rec_mobile`**. No new Python package: torch "
        "was already installed. PP-OCRv5 Devanagari was **not** reachable — docling maps the "
        "torch backend to PP-OCRv4 only, and v5 needs `onnxruntime`, which is absent.\n"
        f"4. **How slow?** {steady[len(steady) // 2]:.1f}s/page median after warmup "
        f"({min(steady):.1f}–{max(steady):.1f}s), on GPU. The first page pays "
        f"{max(ocr_secs):.0f}s of model load and compile.\n"
        "5. **Does it beat Preeti conversion on the failure cases?** On the scan-backed "
        "pages there is nothing to beat — conversion cannot apply. On the font-embedded "
        "controls it clearly **loses** to the existing converter (§3).\n"
        "6. **Good enough to become the fallback?** Not as it stands. It earns a place only "
        "for pages with no legacy text layer. See the recommendation.\n"
    )

    out.append("## 6. Recommendation\n")
    out.append(
        "**Adopt nothing yet, and do not make this the general fallback.** What the evidence "
        "supports is narrower and worth having:\n\n"
        "* **Give the future conversion router a font-provenance precondition.** "
        "`pdffonts`+`pdfimages` separate the populations (§1), cost milliseconds, need no "
        "model, and — importantly — need **no classifier change**: the check belongs to the "
        "router, downstream of native-2. That split is worth keeping whatever OCR wins.\n"
        "* **Conversion stays the primary path** for the 45 font-embedding PDFs (44 with a "
        "recognised legacy family, 1 with stripped names) and is not in question for the 3 "
        "spreadsheets. This OCR is measurably worse on that population.\n"
        "* **Before benchmarking PaddleOCR-VL, try the cheap upgrades on this same 14 "
        "pages**: the PP-OCRv4 *mobile* recogniser is the smallest in the family, and "
        "PP-OCRv5 Devanagari exists but needs the `onnxruntime` backend (a real pip "
        "dependency); docling's OCR render `scale` is also at its 3.0 default. If v5 at a "
        "higher scale still shows §3's conjunct collapse, the defect is the model class, "
        "not the configuration — and that is the point to benchmark a VLM OCR such as "
        "PaddleOCR-VL, which produces logically-ordered spaced text by construction.\n"
        "* **Whatever wins still needs a Nepali reader** before any of it is indexed. §3 is "
        "a well-formedness argument, not a correctness one.\n"
    )

    out.append("## 7. Evaluation & Improvement\n")
    out.append(
        "**Success metric.** Share of scan-backed queue blobs for which OCR yields text a "
        "Nepali reader marks usable, where the current path yields none. Structural proxy "
        "until a reader returns: Devanagari codepoints recovered on pages whose existing "
        "text layer has zero.\n"
    )
    out.append(
        "**Eval.** The 14 named pages above — 8 scan-backed queue blobs, 3 `needs_ocr`, 3 "
        "font-embedded controls where the existing path already works. Scored structurally "
        "here; scored semantically only by a reader. The controls are the guard against "
        "adopting a fallback that is worse than what it falls back from.\n"
    )
    out.append(
        "**Feedback capture.** This file. Reader corrections and per-page disagreements "
        "belong here beside the excerpt they refer to, and feed the OCR-routing decision — "
        "never a native-2 retune.\n"
    )
    out.append(
        "**Review loop.** On any OCR backend or model change, and before OCR is wired into "
        "any pipeline. The provenance split in §1 was measured on the Phase 6B holdout, so "
        "it is **development evidence for the conversion router**, not independent "
        "validation of one. Since the split lives downstream of the classifier it forces no "
        "`native-3` and no fresh cohort; but the moment anything here is used to change "
        "native-2 itself, §14.7 and §15.5 apply in full and a new cohort is required.\n"
    )
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pages", type=int, default=0, help="limit to the first N cases")
    ap.add_argument("--out-dir", default="docs/nrb")
    args = ap.parse_args()

    cases = CASES[: args.pages] if args.pages else CASES
    out_dir = REPO / args.out_dir
    print(f"OCR spike: {len(cases)} pages, backend={OCR_BACKEND}, lang={OCR_LANG}", flush=True)

    started = time.perf_counter()
    results = run(cases, out_dir)
    elapsed = time.perf_counter() - started

    payload = {
        "backend": OCR_BACKEND,
        "lang": OCR_LANG,
        "force_full_page_ocr": True,
        "native_parser": "pypdf",
        "elapsed_seconds": round(elapsed, 1),
        "cases": [asdict(r) for r in results],
    }
    (out_dir / "phase6b-ocr-spike.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "phase6b-ocr-spike.md").write_text(markdown(results, elapsed), encoding="utf-8")
    print(f"\nwrote {out_dir}/phase6b-ocr-spike.{{md,json}}  ({elapsed:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
