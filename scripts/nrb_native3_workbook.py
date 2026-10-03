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
_REPH = re.compile("र्(?=[\u0915-\u0939\u0958-\u095f])")  # consonants + precomposed nukta forms
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


_PAGES: dict = {}  # path -> native pages; each PDF is parsed once, not once per candidate page


def _native_pages(path: Path):
    if path not in _PAGES:
        _PAGES[path] = file_documents.read_pdf_pages(path).pages
    return _PAGES[path]


def _texts(engine, sha: str, page: int):
    path = filestore.resolve_path(filestore.storage_key_for(sha, "pdf"))
    native = _native_pages(path)[page - 1]
    out = engine.repair_page(path, page, native)
    return path, native, out


def _render(path: Path, page: int, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["pdftoppm", "-r", "90", "-f", str(page), "-l", str(page), "-png",
                    "-singlefile", str(path), str(target.with_suffix(""))], check=True)
    return target


def highlight(text: str, klass: str, bold):
    """The repaired text with the row's risky letters in bold (CellRichText), or the plain str."""
    from openpyxl.cell.rich_text import CellRichText, TextBlock

    pattern = {"reph": _REPH, "rakar": _RAKAR, "conjunct": _CONJUNCT}.get(klass)
    if pattern is None and klass != "prebase":
        return text
    extra = 1 if klass == "reph" else 0  # the reph's lookahead consonant is bolded too
    hits = list(re.finditer("ि", text) if klass == "prebase" else pattern.finditer(text))
    if not hits:
        return text
    blocks, cursor = [], 0
    for m in hits:
        blocks.append(text[cursor:m.start()])
        blocks.append(TextBlock(bold, text[m.start():m.end() + extra]))
        cursor = m.end() + extra
    blocks.append(text[cursor:])
    # never `b != ""`: TextBlock.__eq__ assumes its operand is a TextBlock
    return CellRichText([b for b in blocks if not (isinstance(b, str) and b == "")])


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
        text = row["repaired"]
        ws.cell(r, 11).value = highlight(text, klass, bold)
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
