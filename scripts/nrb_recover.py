#!/usr/bin/env python
"""Route ONE (or a few) already-fetched NRB blobs through the production recovery path.

Reads blobs that are already on disk, classifies them with `native-2`, routes
each page to native text / the guarded converter / PP-OCRv5, and prints what it
decided. It touches **no database**, downloads nothing, chunks nothing, embeds
nothing and writes nothing unless `--json` names a file.

    # what would happen, without loading a converter or an OCR model
    .venv/bin/python scripts/nrb_recover.py e08988860534 --plan-only

    # the real thing (needs requirements-nrb.txt for the converter and
    # requirements-worker.txt for OCR)
    .venv/bin/python scripts/nrb_recover.py e08988860534 --text

WHY A BLOB AT A TIME, AND NO --all
    The corpus is 18,263 files. A corpus pass is Phase 7's, it needs a place to
    put its output, and OCR costs seconds per page — so this deliberately has no
    "run everything" mode. It exists to see the router's decision on a named
    file, which is what a reviewer needs and what the next step (a small scratch
    DB ingest) has to be built on.

Exit codes: 0 every blob was routed, 1 something failed (a page could not be
recovered), 2 it could not start (no blob named, or the blob is not on disk).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path  # noqa: E402

from app.nrb import filestore, lexicon as LX, recovery, sniff  # noqa: E402
from app.nrb.extraction import extract_file  # noqa: E402
from app.nrb.legacy_font import ConverterUnavailable, converter_for  # noqa: E402
from app.nrb.ocr import DoclingRapidOcrEngine  # noqa: E402

LEXICON = Path("docs/nrb/phase6b-lexicon.json")


def _find(token: str) -> Path | None:
    """A blob path, from a full path or a content-hash prefix.

    Prefix lookup mirrors the store's own layout (`<sha[:2]>/<sha>.<ext>`), so a
    short hash out of the holdout evidence can be pasted straight in.
    """
    direct = Path(token)
    if direct.is_file():
        return direct
    if len(token) < 4:
        return None
    base = filestore.base_dir()
    matches = sorted((base / token[:2]).glob(f"{token}*"))
    return matches[0] if matches else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("blobs", nargs="*", help="content hash (or prefix), or a path")
    ap.add_argument("--plan-only", action="store_true",
                    help="decide routes; load no converter and no OCR model")
    ap.add_argument("--no-ocr", action="store_true",
                    help="convert, but record OCR pages as unavailable")
    ap.add_argument("--mapping", default="Preeti", help="legacy font mapping")
    ap.add_argument("--lexicon", default=str(LEXICON))
    ap.add_argument("--text", action="store_true", help="print the recovered text")
    ap.add_argument("--json", help="write the full record here")
    args = ap.parse_args()

    if not args.blobs:
        ap.error("name at least one blob (content hash, prefix, or path)")

    converter = lexicon = engine = None
    if not args.plan_only:
        try:
            converter = converter_for(args.mapping)
            lexicon = LX.load_lexicon(args.lexicon)
        except (ConverterUnavailable, LX.LexiconError) as exc:
            print(f"converter unavailable: {exc}", file=sys.stderr)
        if not args.no_ocr:
            engine = DoclingRapidOcrEngine()
            ok, evidence = engine.open()
            print(f"ocr: {evidence}" if ok else f"ocr unavailable: {evidence}")
            if not ok:
                engine = None

    records = []
    failures = 0
    for token in args.blobs:
        path = _find(token)
        if path is None:
            print(f"{token}: not on disk", file=sys.stderr)
            return 2

        family = sniff.family_for(sniff.sniff(path.read_bytes()[:4096])[0])
        result = extract_file(
            path, family=family, extension=path.suffix.lstrip("."),
            extractor_version="native-2",
        )
        recovered = recovery.recover(
            path, result, converter=converter, lexicon=lexicon, ocr=engine
        )

        print(
            f"\n=== {path.name}  [{family}]  {result.status}/{result.reason}  "
            f"unit_legacy_ratio={result.metrics.get('unit_legacy_ratio')}"
        )
        print(f"    plan: {recovered.plan} ({recovered.plan_reason})  "
              f"routes: {recovered.route_counts}")
        for page in recovered.pages:
            flag = "" if page.ok else f"  !! {page.error}"
            label = f" {page.label}" if page.label else ""
            print(f"    p{page.page_number}{label}: {page.route:18} {page.reason:24}"
                  f" {len(page.text):>6} chars{flag}")
            if args.text and page.text:
                print("      " + " ".join(page.text.split())[:200])
        # `--plan-only` deliberately loads neither dependency, so every routed
        # page fails closed. That is the correct behaviour, not a run failure.
        failures += 0 if (recovered.ok or args.plan_only) else 1
        records.append({"blob": path.name, **recovered.as_dict()})

    if engine is not None:
        engine.close()
    if args.json:
        Path(args.json).write_text(json.dumps(records, ensure_ascii=False, indent=2))
        print(f"\nwrote {args.json}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
