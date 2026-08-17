#!/usr/bin/env python
"""Draw the Phase 7 validation cohort and freeze it to JSON.

    DATABASE_URL=postgresql+asyncpg://gateway:***@127.0.0.1:5432/local_ai_gateway_p4 \
        .venv/bin/python scripts/nrb_p7_cohort.py --write

This is a ONE-TIME act. The cohort it produces
(`docs/nrb/phase7-validation-cohort.json`) is committed and the ingest driver
reads only that file — re-running this script is for auditing the draw, not for
re-drawing it. `--write` refuses to overwrite an existing cohort unless
`--force` is given, and prints the old fingerprint if it does.

WHY THIS IS NOT A MANIFEST
    `app/nrb/manifest.py` freezes a `Sample` and certifies *sampling
    reproducibility* — its `selection_sha256` covers the sampler's algorithm,
    seed and parameters, and `build_manifest` deliberately admits no second path
    by which a key can enter. Half of this cohort is hand-picked, so it has no
    sampling provenance to certify and cannot honestly go through that door.
    What it gets instead is a plain ordered key list with its own sha256, which
    is enough to prove the ingest driver ran on the cohort that was committed.

THREE ROLES, DRAWN THREE DIFFERENT WAYS
    anchor       8 blobs whose chunk counts and route splits are recorded in
                 `docs/nrb-integration.md` §17/§18.7. Hand-picked, and the ONLY
                 route-aware part of the cohort. They are the regression check
                 and they are what guarantees the run covers native, legacy
                 conversion, OCR, a mixed document and a spreadsheet.
    unknown      22 blobs drawn BLIND with respect to route. Nothing about how
                 they extract was consulted — not `nrb_extractions`, not a
                 preview, not a page count. They exist to reveal behaviour the
                 eight familiar blobs cannot.
    unsupported  exactly 1 OLE2 file (`.doc`/`.xls`), which has no parser at
                 all. It is here to prove one thing — that a document which
                 cannot be parsed fails its own job and the batch continues —
                 and it is counted separately from the 30 supported documents
                 everywhere in the report.

WHAT "BLIND" DOES AND DOES NOT MEAN
    Blind with respect to ROUTE, not unrestricted. The draw is scoped by
    `extension IN ('pdf','xlsx','docx')`, which is catalog data (what NRB
    served), never extraction data (what came out). Without that scope a random
    draw would pull images and OLE2 files, and "exactly one unsupported file"
    would stop being true.

    It is also drawn from the blobs that HAPPEN TO BE FETCHED — 570 of 18,266,
    put on disk by the Phase 6A benchmark, the 6B holdout and the core fetch.
    That pool is not a random sample of the corpus, so this cohort cannot
    support a population claim about NRB and the report must not make one. Most
    of its members do have extraction rows from those earlier passes; "unknown"
    means *this cohort did not look*, not that nobody ever has.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

SCRATCH_DB = "local_ai_gateway_p4"
COHORT_PATH = Path("docs/nrb/phase7-validation-cohort.json")
SCHEMA_VERSION = 1

# The draw is a deterministic ordering, not an RNG: rank each candidate by
# sha256(SEED + content_sha256) and take the top N. Same seed + same pool =>
# same cohort, on any machine, with no dependence on Python's RNG version.
SEED = "nrb-phase7-validation-2026-08-17"

UNKNOWN_COUNT = 22
UNSUPPORTED_COUNT = 1
SUPPORTED_EXTENSIONS = ("pdf", "xlsx", "docx")
UNSUPPORTED_EXTENSIONS = ("doc", "xls")

# The eight anchors, with what §18.7 measured for each through a deployed
# worker. `chunks` is the regression target; a different number on this cohort
# means the ingest path changed, and that is the point of carrying them.
ANCHORS: tuple[tuple[str, int, str, str], ...] = (
    ("075bf12eb087", 4, "native", "clean native Unicode PDF, 2 pages (§17.6: its own text layer is corrupt at the codepoint level — recorded, not fixed)"),
    ("1a9b6321aa61", 1, "legacy_conversion", "embedded Preeti+Bishall, deterministic conversion"),
    ("268bcfe86d03", 1, "legacy_conversion", "embedded Preeti circular 2007, PARTIAL recovery"),
    ("3d2eca8b9f95", 2, "ocr", "300 dpi scan, no embedded font, PP-OCRv5"),
    ("c298efaf1f16", 4, "ocr", "no text layer at all, PP-OCRv5, 3 pages"),
    ("e08988860534", 75, "ocr+legacy_conversion", "THE mixed document: p1 OCR, p2-50 conversion"),
    ("7820b1f49fc1", 9, "legacy_conversion", "stripped font names /CIDFont+F1..F6, stays eligible"),
    ("8df7b02f8a13", 154, "legacy_conversion", "Preeti-encoded workbook, per-CELL conversion"),
)


def _guard() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if SCRATCH_DB not in url:
        print(
            f"refusing to run: DATABASE_URL must name {SCRATCH_DB}. "
            "NRB work never touches the dev database.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    print(f"database: {url.rsplit('/', 1)[-1]}")
    return url


def _rank(sha: str) -> str:
    return hashlib.sha256(f"{SEED}:{sha}".encode()).hexdigest()


def cohort_sha256(entries: list[dict]) -> str:
    """Fingerprint of the cohort as an ORDERED list of (role, key) pairs.

    Over `comparison_key` rather than `content_sha256` because the key is what
    the driver scopes on; over the role too, so moving a blob between anchor and
    unknown changes the identity even though the key set did not.
    """
    canonical = json.dumps(
        [[e["role"], e["comparison_key"]] for e in entries],
        ensure_ascii=False, separators=(",", ":"), sort_keys=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _resolve_anchors(session) -> list[dict]:
    out: list[dict] = []
    for prefix, chunks, route, why in ANCHORS:
        row = (
            await session.execute(
                text(
                    """
                    SELECT content_sha256, comparison_key, extension, filename
                      FROM nrb_files
                     WHERE fetch_status = 'fetched'
                       AND content_sha256 LIKE :p
                     ORDER BY id
                     LIMIT 1
                    """
                ),
                {"p": f"{prefix}%"},
            )
        ).mappings().first()
        if row is None:
            raise SystemExit(f"anchor {prefix} is not a fetched blob in this database")
        out.append(
            {
                "role": "anchor",
                "content_sha256": row["content_sha256"],
                "comparison_key": row["comparison_key"],
                "extension": row["extension"],
                "filename": row["filename"],
                "why": why,
                "expected": {"chunks": chunks, "route": route, "source": "§18.7"},
            }
        )
    return out


async def _draw(session, *, extensions: tuple[str, ...], exclude: set[str], count: int,
                role: str, why: str) -> list[dict]:
    """Rank the eligible blobs by `_rank` and take the top `count`.

    One row per blob: `DISTINCT ON (content_sha256)` with the lowest id as the
    representative, because two catalog entries sharing bytes are one document
    and must not both enter the cohort.
    """
    rows = (
        await session.execute(
            text(
                """
                SELECT DISTINCT ON (content_sha256)
                       content_sha256, comparison_key, extension, filename
                  FROM nrb_files
                 WHERE fetch_status = 'fetched'
                   AND extension = ANY(:exts)
                 ORDER BY content_sha256, id
                """
            ),
            {"exts": list(extensions)},
        )
    ).mappings().all()
    candidates = [r for r in rows if r["content_sha256"] not in exclude]
    candidates.sort(key=lambda r: _rank(r["content_sha256"]))
    if len(candidates) < count:
        raise SystemExit(
            f"{role}: pool has {len(candidates)} eligible blobs, need {count}"
        )
    return [
        {
            "role": role,
            "content_sha256": r["content_sha256"],
            "comparison_key": r["comparison_key"],
            "extension": r["extension"],
            "filename": r["filename"],
            "why": why,
            "expected": None,
        }
        for r in candidates[:count]
    ]


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--write", action="store_true", help="write the cohort file")
    ap.add_argument("--force", action="store_true", help="overwrite an existing cohort")
    ap.add_argument("--path", default=str(COHORT_PATH))
    args = ap.parse_args()

    url = _guard()
    engine = create_async_engine(url)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with Session() as session:
            anchors = await _resolve_anchors(session)
            taken = {e["content_sha256"] for e in anchors}
            unknowns = await _draw(
                session, extensions=SUPPORTED_EXTENSIONS, exclude=taken,
                count=UNKNOWN_COUNT, role="unknown",
                why="drawn blind: ranked by sha256(seed+content_sha256), no "
                    "extraction evidence consulted",
            )
            taken |= {e["content_sha256"] for e in unknowns}
            unsupported = await _draw(
                session, extensions=UNSUPPORTED_EXTENSIONS, exclude=taken,
                count=UNSUPPORTED_COUNT, role="unsupported",
                why="OLE2, no parser (§15.2) — proves a failed job isolates and "
                    "the batch continues",
            )
            pool = (
                await session.execute(
                    text(
                        """
                        SELECT count(DISTINCT content_sha256) AS supported,
                               (SELECT count(DISTINCT content_sha256) FROM nrb_files
                                 WHERE fetch_status='fetched'
                                   AND extension = ANY(:unsup)) AS unsupported,
                               (SELECT count(*) FROM nrb_files
                                 WHERE fetch_status='fetched') AS fetched_rows
                          FROM nrb_files
                         WHERE fetch_status = 'fetched' AND extension = ANY(:sup)
                        """
                    ),
                    {"sup": list(SUPPORTED_EXTENSIONS),
                     "unsup": list(UNSUPPORTED_EXTENSIONS)},
                )
            ).mappings().one()
            await session.rollback()
    finally:
        await engine.dispose()

    entries = anchors + unknowns + unsupported
    cohort = {
        "schema_version": SCHEMA_VERSION,
        "purpose": "Phase 7 step 1 — validation of the corpus ingest driver. "
                   "NOT a benchmark: it measures whether the driver works, not "
                   "how well extraction or retrieval performs, and its pool is "
                   "the fetched blobs rather than a corpus sample.",
        "drawn_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seed": SEED,
        "draw_rule": "rank by sha256(seed + ':' + content_sha256), ascending, "
                     "over DISTINCT ON (content_sha256) fetched rows",
        "pool": {
            "definition": "nrb_files.fetch_status = 'fetched'",
            "supported_extensions": list(SUPPORTED_EXTENSIONS),
            "unsupported_extensions": list(UNSUPPORTED_EXTENSIONS),
            "supported_blobs": pool["supported"],
            "unsupported_blobs": pool["unsupported"],
            "fetched_rows": pool["fetched_rows"],
            "caveat": "the fetched set was assembled by the Phase 6A benchmark, "
                      "the 6B holdout and the core fetch. It is not a random "
                      "sample of the 18,266-file corpus and supports no "
                      "population claim.",
        },
        "counts": {
            "anchor": len(anchors),
            "unknown": len(unknowns),
            "unsupported": len(unsupported),
            "supported_total": len(anchors) + len(unknowns),
            "total": len(entries),
        },
        "cohort_sha256": "",
        "entries": entries,
    }
    cohort["cohort_sha256"] = cohort_sha256(entries)

    print(f"\nanchors      {len(anchors):>3}   (route-aware, §18.7 regression targets)")
    print(f"unknowns     {len(unknowns):>3}   (blind, pool {pool['supported']} blobs)")
    print(f"unsupported  {len(unsupported):>3}   (OLE2, pool {pool['unsupported']} blobs)")
    print(f"cohort_sha256 {cohort['cohort_sha256']}")
    for e in entries:
        print(f"  {e['role']:<12} {e['content_sha256'][:12]} .{e['extension']:<5} "
              f"{(e['filename'] or '')[:56]}")

    if not args.write:
        print("\n(dry run — pass --write to freeze this cohort)")
        return 0

    target = Path(args.path)
    if target.exists() and not args.force:
        old = json.loads(target.read_text()).get("cohort_sha256", "(none)")
        print(f"\nrefusing to overwrite {target} (cohort_sha256 {old}); "
              f"pass --force if you really mean to re-draw", file=sys.stderr)
        return 3
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(cohort, ensure_ascii=False, indent=2) + "\n")
    print(f"\nwrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
