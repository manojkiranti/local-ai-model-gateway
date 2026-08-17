#!/usr/bin/env python
"""Inspect, verify and purge the NRB recovery cache (Phase 7 step 2).

    DATABASE_URL=postgresql+asyncpg://gateway:***@127.0.0.1:5432/local_ai_gateway_p4 \
        .venv/bin/python scripts/nrb_recovery_cache.py --stats

    ... scripts/nrb_recovery_cache.py --reuse-check e08988860534 3d2eca8b9f95
    ... scripts/nrb_recovery_cache.py --purge --stale-only

THREE MODES, AND `--reuse-check` IS THE ONE THAT MATTERS
    `--stats` is §18's verification query in SQL: the route split and the engine
    versions actually in the cache. "The worker is verified by its route split
    on known blobs, never by whether ingestion succeeded" — and now that split
    is a GROUP BY rather than a JSONB unnest over chunks.

    `--reuse-check` runs recovery TWICE over named blobs, through the cache,
    counting converter and OCR invocations by wrapping the real dependencies.
    The second pass must report zero of each. That is the property; equal
    output would not prove it, because a converter that ran again and produced
    the same answer looks identical.

    `--purge` is the explicit refresh. A unit whose engine errored transiently
    is cached under a version that has not moved, so it is reused until someone
    decides otherwise — and deciding is a command, not a heuristic. There is no
    transient-vs-permanent classifier here. `--stale-only` keeps the current
    base version and drops superseded ones.

SCRATCH DATABASE ONLY
    Refuses unless `DATABASE_URL` names `local_ai_gateway_p4`, and prints the
    resolved database name before touching anything.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path  # noqa: E402

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.nrb import filestore, recovery_cache  # noqa: E402
from app.nrb import rag as nrb_rag  # noqa: E402

SCRATCH_DB = "local_ai_gateway_p4"


def _guard() -> str:
    url = os.environ.get("DATABASE_URL", "")
    name = url.rsplit("/", 1)[-1].split("?")[0]
    if name != SCRATCH_DB:
        print(
            f"refusing to run: DATABASE_URL resolves to database {name!r}, "
            f"but NRB work runs only against {SCRATCH_DB!r}.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    print(f"database: {name}")
    return url


class _CountingConverter:
    """The real converter, wrapped so its calls can be counted.

    Delegation rather than a subclass: `LegacyFontConverter` is a protocol here
    and the concrete class comes from `legacy_font`, which is the only module
    allowed to know npttf2utf exists.
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls = 0

    def __getattr__(self, item):
        return getattr(self._inner, item)

    def convert(self, text_in: str) -> str:
        self.calls += 1
        return self._inner.convert(text_in)


class _CountingOcr:
    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls = 0

    def __getattr__(self, item):
        return getattr(self._inner, item)

    def ocr_page(self, path, page_number: int) -> str:
        self.calls += 1
        return self._inner.ocr_page(path, page_number)


async def do_stats(Session) -> None:
    async with Session() as session:
        payload = await recovery_cache.stats(session)
        await session.rollback()

    print(f"\ncurrent base version: {recovery_cache.base_version()}")
    print("\n--- cached documents by base version ---")
    for row in payload["versions"]:
        mark = "*" if row["base_version"] == recovery_cache.base_version() else " "
        print(f" {mark} {row['base_version']:<52} "
              f"{row['documents']:>6} docs  {row['units']:>7} units")
    print("\n--- units by route / engine version ---")
    for row in sorted(payload["routes"], key=lambda r: (r["route"], r["engine_version"])):
        state = "ok" if row["ok"] else "WITHHELD"
        print(f"   {row['route']:<20} {row['engine_version']:<46} "
              f"{state:<9} {row['units']:>7}")
    if not payload["versions"]:
        print("   (empty)")


async def do_purge(Session, *, sha: str | None, stale_only: bool) -> None:
    async with Session() as session:
        removed = await recovery_cache.purge(
            session, content_sha256=sha, keep_current=stale_only
        )
        await session.commit()
    scope = sha[:12] if sha else ("superseded versions" if stale_only else "EVERYTHING")
    print(f"\npurged {removed} cached recoveries ({scope})")


async def _resolve_blob(session, prefix: str) -> tuple[str, Path] | None:
    """A blob's full sha and on-disk path, from a sha prefix or comparison key."""
    row = (
        await session.execute(
            text(
                "SELECT content_sha256, storage_key FROM nrb_files "
                " WHERE fetch_status = 'fetched' "
                "   AND (content_sha256 LIKE :p || '%' OR comparison_key = :k) "
                " LIMIT 1"
            ),
            {"p": prefix, "k": prefix},
        )
    ).first()
    if row is None:
        return None
    return row[0], filestore.resolve_path(row[1])


async def do_reuse_check(Session, prefixes: list[str], settings) -> int:
    """Recover each blob twice through the cache; report the work each pass did.

    Both passes use the SAME wrapped dependencies, so the counters measure this
    process's invocations rather than two different engines' behaviour.
    """
    converter, lexicon, ocr = nrb_rag.nrb_dependencies()
    print("\ndependencies:")
    print(f"  converter  {getattr(converter, 'name', None)} "
          f"{getattr(converter, 'version', '')} / "
          f"{getattr(converter, 'mapping', '')}"
          if converter else "  converter  MISSING (legacy pages will be withheld)")
    print(f"  lexicon    {lexicon.fingerprint[:12] if lexicon else 'MISSING'}")
    print(f"  ocr        {getattr(ocr, 'model', 'MISSING')} "
          f"{getattr(ocr, 'version', '')}")
    print(f"  base       {recovery_cache.base_version()}")

    counted_converter = _CountingConverter(converter) if converter else None
    counted_ocr = _CountingOcr(ocr) if ocr else None
    injected = {
        "converter": counted_converter, "lexicon": lexicon, "ocr": counted_ocr,
    }

    targets: list[tuple[str, str, Path]] = []
    async with Session() as session:
        for prefix in prefixes:
            found = await _resolve_blob(session, prefix)
            if found is None or not found[1].exists():
                print(f"  !! {prefix}: not a fetched blob on this machine")
                continue
            targets.append((prefix, found[0], found[1]))
        await session.rollback()
    if not targets:
        print("\nnothing to check")
        return 2

    results = []
    for pass_no in (1, 2):
        print(f"\n=== pass {pass_no} ===")
        for prefix, sha, path in targets:
            before_conv = counted_converter.calls if counted_converter else 0
            before_ocr = counted_ocr.calls if counted_ocr else 0
            started = time.perf_counter()
            chunks, report = await recovery_cache.chunks_for_blob(
                Session,
                path,
                content_sha256=sha,
                max_chars=settings.rag_chunk_max_chars,
                overlap_chars=settings.rag_chunk_overlap_chars,
                **injected,
            )
            elapsed = time.perf_counter() - started
            conv = (counted_converter.calls if counted_converter else 0) - before_conv
            ocr_calls = (counted_ocr.calls if counted_ocr else 0) - before_ocr
            results.append(
                {
                    "pass": pass_no, "blob": prefix, "outcome": report.outcome,
                    "units": report.units_total, "reused": report.units_reused,
                    "recovered": report.units_recovered,
                    "converter_units": report.converter_units,
                    "ocr_units": report.ocr_units,
                    "converter_calls": conv, "ocr_calls": ocr_calls,
                    "chunks": len(chunks), "seconds": round(elapsed, 2),
                }
            )
            print(
                f"  {prefix:<14} {report.outcome:<8} "
                f"{report.units_total:>3} units "
                f"({report.units_reused} reused / {report.units_recovered} run)  "
                f"converter pages {report.converter_units:>2} calls {conv:>5}  "
                f"ocr pages {report.ocr_units:>2} calls {ocr_calls:>3}  "
                f"{len(chunks):>4} chunks  {elapsed:6.1f}s"
            )

    second = [r for r in results if r["pass"] == 2]
    conv_total = sum(r["converter_calls"] for r in second)
    ocr_total = sum(r["ocr_calls"] for r in second)
    print("\n=== verdict ===")
    print(f"  second pass npttf2utf calls : {conv_total}  (expected 0)")
    print(f"  second pass PP-OCR calls    : {ocr_total}  (expected 0)")
    ok = conv_total == 0 and ocr_total == 0 and all(
        r["outcome"] == "warm" for r in second
    )
    print("  REUSE VERIFIED" if ok else "  REUSE NOT VERIFIED")

    first = [r for r in results if r["pass"] == 1]
    same = all(
        f["chunks"] == s["chunks"] for f, s in zip(first, second)
    )
    print(f"  chunk counts identical      : {same}")
    print(json.dumps(results, indent=2))
    return 0 if (ok and same) else 1


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--reuse-check", nargs="+", metavar="SHA_PREFIX",
                    help="recover these blobs twice and count engine calls")
    ap.add_argument("--purge", action="store_true")
    ap.add_argument("--sha", help="limit --purge to one blob")
    ap.add_argument("--stale-only", action="store_true",
                    help="with --purge: keep the CURRENT base version")
    args = ap.parse_args()

    url = _guard()
    settings = get_settings()
    engine = create_async_engine(url)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        if args.reuse_check:
            return await do_reuse_check(Session, args.reuse_check, settings)
        if args.purge:
            if not args.sha and not args.stale_only:
                print("refusing to purge the whole cache without --stale-only "
                      "or --sha. Say which.", file=sys.stderr)
                return 2
            await do_purge(Session, sha=args.sha, stale_only=args.stale_only)
            return 0
        await do_stats(Session)
        return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
