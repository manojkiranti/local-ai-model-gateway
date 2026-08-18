"""Deterministic eval for image OCR — 9 labelled cases, target 9/9.

Same role as tests/test_document_eval.py: a small, fixed set that says whether
the feature still works, rather than exhaustive unit coverage.

Two rules about what is asserted, both learned by measurement:

  * ENGLISH is asserted on exact figures. Measured, PP-OCRv5 returns
    "45,320.75" and "0123456789" verbatim at mean confidence >= 0.99 across
    clean, rotated, low-contrast and small renderings.
  * DEVANAGARI is asserted on aggregates plus an ANY-OF word set, never on a
    fixed transcription. The same fixture returned 'नेपाल राषट्र बैंक' on one run
    and 'h राष्ट्र नंक' on another; the engine also renders राष्ट्र as राष्टर on
    a real scan. §16.6 states this outright — OCR output is retrieval text, not
    a transcription — and Devanagari CORRECTNESS is still the open §15 Nepali
    review. Pinning a transcription would encode a bug as an expectation and
    would fail on an upgrade that improved the average.

Thresholds sit below what was measured (2026-08-18: 18/30/37 lines and
506/881/1423 Devanagari characters on the three real pages) so an engine
refresh does not turn a small regression in one line into a red build. They are
headroom, not tuning: every case passed before the numbers were written down.

The three Devanagari fixtures are REAL scanned NRB pages committed at
docs/nrb/ocr-spike-pages/ — the same population §16.6 measured.
"""

from __future__ import annotations

import asyncio
import io
import pathlib
from dataclasses import dataclass, field

import pytest

from app.files import image_ocr
from app.files.store import PNG_MEDIA_TYPE, file_store
from app.tools.local import read_image

pytestmark = pytest.mark.skipif(
    not image_ocr.available(), reason="rapidocr not installed"
)

DEJAVU = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
# Repo-anchored, not CWD-relative: a relative path here would make the three
# real-scan cases SKIP silently when pytest runs from anywhere but the repo
# root, and a skipped eval case reads exactly like a passing one in the count.
SPIKE_PAGES = pathlib.Path(__file__).resolve().parent.parent / "docs" / "nrb" / "ocr-spike-pages"


@pytest.fixture(autouse=True)
def _configure_store(tmp_path):
    file_store.configure(str(tmp_path))
    yield


@dataclass
class Case:
    name: str
    # exact substrings that must ALL appear
    expect_all: tuple[str, ...] = ()
    # substrings of which at least `expect_any_min` must appear
    expect_any: tuple[str, ...] = ()
    expect_any_min: int = 0
    min_lines: int = 0
    min_devanagari: int = 0
    expect_error: str = ""
    render: dict = field(default_factory=dict)
    fixture: str = ""


CASES = [
    Case(
        name="english_clean",
        expect_all=("45,320.75", "0123456789"),
        render={"lines": ["Total Amount: 45,320.75", "Account: 0123456789"]},
    ),
    Case(
        name="english_rotated_3deg",
        expect_all=("45,320.75",),
        render={"lines": ["Total Amount: 45,320.75"], "rotate": 3},
    ),
    Case(
        name="english_low_contrast",
        expect_all=("45,320.75",),
        render={
            "lines": ["Total Amount: 45,320.75"],
            "fg": (150, 150, 150),
            "bg": (225, 225, 225),
        },
    ),
    Case(
        name="english_small_type",
        expect_all=("45,320.75",),
        render={"lines": ["Total Amount: 45,320.75"], "size": 16, "width": 500},
    ),
    Case(
        name="nepali_scan_tender_notice",
        fixture="276b2eb62802-p001.jpg",
        min_lines=12,
        min_devanagari=400,
        expect_any=("सामान्य", "सेवा", "विभाग", "सूचना"),
        expect_any_min=2,
    ),
    Case(
        name="nepali_scan_hr_circular",
        fixture="438c55304da5-p001.jpg",
        min_lines=20,
        min_devanagari=700,
        expect_any=("नेपाल", "बैंक", "कार्यालय", "विभाग"),
        expect_any_min=2,
    ),
    Case(
        name="nepali_scan_investment_notice",
        fixture="c298efaf1f16-p001.jpg",
        min_lines=25,
        min_devanagari=1000,
        expect_any=("अनुसूची", "लगानी", "सूचना", "सम्बन्धी"),
        expect_any_min=2,
    ),
    Case(
        name="blank_image_has_no_text",
        expect_error="no text was detected",
        render={"lines": [], "blank": True},
    ),
    Case(
        name="not_really_an_image",
        expect_error="could not read the image",
        fixture="__not_an_image__",
    ),
]


def _render(**kw) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    lines = kw.get("lines", [])
    size = kw.get("size", 34)
    width = kw.get("width", 900)
    fg = kw.get("fg", (0, 0, 0))
    bg = kw.get("bg", (255, 255, 255))
    height = 60 + size * 2 * max(1, len(lines))
    img = Image.new("RGB", (width, height), bg)
    if lines:
        draw = ImageDraw.Draw(img)
        font = ImageFont.truetype(DEJAVU, size)
        y = 25
        for line in lines:
            draw.text((30, y), line, fill=fg, font=font)
            y += size * 2
    if kw.get("rotate"):
        img = img.rotate(kw["rotate"], expand=True, fillcolor=bg)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _payload(case: Case) -> tuple[bytes, str]:
    if case.fixture == "__not_an_image__":
        return b"MZ\x90\x00 definitely not an image", "a.png"
    if case.fixture:
        path = SPIKE_PAGES / case.fixture
        if not path.exists():
            pytest.skip(f"fixture missing: {path}")
        return path.read_bytes(), case.fixture
    return _render(**case.render), f"{case.name}.png"


def _run(case: Case) -> str:
    raw, filename = _payload(case)
    media = PNG_MEDIA_TYPE if filename.endswith(".png") else "image/jpeg"
    record = asyncio.run(file_store.save(raw, filename=filename, media_type=media))
    return asyncio.run(read_image.SPEC.func({"file_id": record.id}))


def _judge(case: Case, out: str) -> list[str]:
    """Return the reasons this case failed (empty list = passed)."""
    problems: list[str] = []
    if case.expect_error:
        if case.expect_error not in out:
            problems.append(f"expected error {case.expect_error!r}, got {out[:120]!r}")
        return problems
    if out.startswith("ERROR:"):
        return [f"unexpected error: {out[:160]}"]

    body = "\n".join(out.splitlines()[1:])
    for needle in case.expect_all:
        if needle not in body:
            problems.append(f"missing {needle!r}")
    if case.expect_any:
        hits = [n for n in case.expect_any if n in body]
        if len(hits) < case.expect_any_min:
            problems.append(
                f"only {len(hits)}/{case.expect_any_min} of {case.expect_any} found"
            )
    if case.min_lines:
        # Body lines minus the caveat/engine header block already stripped above;
        # count non-empty content lines.
        count = len([ln for ln in body.splitlines() if ln.strip()])
        if count < case.min_lines:
            problems.append(f"{count} lines < {case.min_lines}")
    if case.min_devanagari:
        deva = sum(1 for ch in body if "ऀ" <= ch <= "ॿ")
        if deva < case.min_devanagari:
            problems.append(f"{deva} devanagari chars < {case.min_devanagari}")
    return problems


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_case(case: Case):
    problems = _judge(case, _run(case))
    assert not problems, f"{case.name}: " + "; ".join(problems)


def test_the_whole_eval_set_passes():
    """The headline number. Target 9/9 — if this drops, §Evaluation's review loop
    says escalate rather than adjust a threshold."""
    failures = {}
    for case in CASES:
        problems = _judge(case, _run(case))
        if problems:
            failures[case.name] = problems
    assert not failures, f"{len(CASES) - len(failures)}/{len(CASES)} passed; {failures}"
