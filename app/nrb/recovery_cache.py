"""The versioned recovery cache: recover a blob once, reuse it thereafter.

Recovery is the expensive stage and the only one that used to repeat. Sync is
all-zero on a second run, fetch selects `pending` only, extract selects blobs
with no row at this `extractor_version` — but `rag.parse_nrb_to_chunks` re-ran
`extraction.extract_file` and then npttf2utf and PP-OCRv5 from scratch on every
ingest, at ~2-4 s/page (`docs/nrb-integration.md` §19.2). Fine over 8 blobs, not
fine over 18,266. This module is the reuse boundary.

**It is not `nrb_extractions`, and that is a decision, not an accident.** That
table is Phase 6 evidence: written by a measurement pass an operator may never
have run, storing no text, and profiling a corpus rather than serving one. If
ingestion read it, every future ingest would depend on whether someone had
profiled first (§19.3, §20.1). Nothing here imports it, joins it or queries it.

THE TWO VERSION DOMAINS, AND WHY THERE ARE TWO
----------------------------------------------
One monolithic cache key would work and would be wrong in a specific, expensive
way: bumping the OCR model would invalidate every deterministic legacy
conversion in the corpus, and bumping npttf2utf would re-run every scan. So the
key is split along the only line that matters — *did the ROUTE change, or did
what the route PRODUCES change?*

**`BASE_VERSION`** (on `nrb_recoveries`) is the routing identity. It covers
everything that could change WHICH route a unit receives:

    native-2                 the classifier whose status/reason/metrics feed
                             `plan_document`
    recovery-1               `recovery.RECOVERY_ROUTING_VERSION` — the plan
                             ordering and `route_page`'s rules
    prov-1                   `provenance.PAGE_PROVENANCE_VERSION` — how "does
                             this page carry a font" is answered
    gate=0.80 unjudged=0.80  the two gate constants, read live so editing either
                             cannot be forgotten

A change here invalidates the WHOLE document, correctly: the routes themselves
may now differ, so no unit's cached answer is still about the right question.

**`engine_version`** (on `nrb_recovery_units`, per unit) is the identity of
whatever produced that unit's text, and it depends on the unit's route:

    native              the extractor identity. Parser changes are what
                        `extraction.EXTRACTOR_VERSION` is documented to be
                        bumped for, so it is reused rather than tracking pypdf,
                        python-docx and openpyxl versions separately — which
                        would make an openpyxl release invalidate every PDF.
    legacy_conversion   npttf2utf version + mapping + lexicon fingerprint. The
                        lexicon belongs HERE, not in the base version: it is a
                        conversion guard, so it changes what conversion
                        produces, never where a page is routed.
    ocr                 PP-OCR model + language + backend + package versions.

An ABSENT dependency renders as `unavailable`, which is a version like any
other. That is what makes fail-closed and selectivity the same mechanism: a page
recorded `conversion_unavailable` on a deployment without npttf2utf can never be
served once npttf2utf is installed, because the current engine version no longer
matches — while its OCR'd and native neighbours in the same document are still
reused.

WHAT IS AND IS NOT INVALIDATED
------------------------------
    embedding model change    nothing — recovery does not embed
    chunker change            nothing — the cache stores TEXT, not chunks
    OCR model change          OCR units only
    converter/lexicon change  legacy_conversion units only
    routing/classifier change the whole document (base version)
    blob bytes change         a different `content_sha256`, so a different row

FAIL-CLOSED IS PRESERVED, NOT RE-IMPLEMENTED
--------------------------------------------
`recovery.py` stays the semantic owner. A stale unit is refreshed by calling
`recovery.convert_unit` / `recovery.ocr_unit` — the same functions a cold run
calls, with the same withholding, the same "a failed OCR page never falls back
to its junk text layer", the same "a conversion that does not succeed withholds
its input". This module chooses what to SKIP; it never chooses differently.

Three further protections, because a cache is exactly where trust decays:

  1. Only post-`_withhold` text is ever written, so the glyph-mapped original of
     an unresolved unit is not in the database and cannot be resurrected from it.
  2. `indexable` is NOT a stored column. It is recomputed by `PageText` from
     `(ok, text)` on read, so a row cannot assert a trust state the current
     rules would refuse.
  3. A reused unit is rebuilt into a real `PageText` and goes through the same
     `rag.chunks_from_recovery` a cold run does. There is one chunking path, fed
     from two sources.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..files import documents as file_documents
from . import legacy_convert, provenance, recovery
from .legacy_font import LegacyFontConverter
from .lexicon import Lexicon
from .models import NRBRecovery, NRBRecoveryUnit
from .ocr import PageOcrEngine

logger = logging.getLogger("app.nrb.recovery_cache")

__all__ = [
    "CacheReport",
    "CachedRecovery",
    "CachedUnit",
    "EngineVersions",
    "UNAVAILABLE",
    "base_version",
    "engine_versions",
    "load",
    "purge",
    "resolve",
    "save",
    "stats",
]

# The engine version of a route whose dependency is not installed. A real
# version string, deliberately: it makes "recovered without a converter" a
# distinct cache state rather than an absence, so installing the converter
# invalidates exactly those units and nothing else.
UNAVAILABLE = "unavailable"


def base_version(extractor_version: str = "native-2") -> str:
    """The ROUTING identity. See the module docstring for what belongs here.

    Assembled from live constants rather than restated, so that editing
    `recovery.CONVERSION_GATE` or `legacy_convert.UNJUDGED_MIN_LEGACY_RATIO`
    changes the key whether or not anyone remembered to bump a version string.
    The two are separate terms because they decide different things (§16) and a
    single combined number would hide a change to one of them.
    """
    return (
        f"{extractor_version}"
        f"|{recovery.RECOVERY_ROUTING_VERSION}"
        f"|{provenance.PAGE_PROVENANCE_VERSION}"
        f"|gate={recovery.CONVERSION_GATE:g}"
        f"|unjudged={legacy_convert.UNJUDGED_MIN_LEGACY_RATIO:g}"
    )


@dataclass(frozen=True)
class EngineVersions:
    """The current identity of each route's engine, one string per route."""

    native: str
    legacy_conversion: str
    ocr: str

    def for_route(self, route: str) -> str:
        if route == recovery.ROUTE_LEGACY:
            return self.legacy_conversion
        if route == recovery.ROUTE_OCR:
            return self.ocr
        return self.native

    def as_dict(self) -> dict[str, str]:
        return {
            recovery.ROUTE_NATIVE: self.native,
            recovery.ROUTE_LEGACY: self.legacy_conversion,
            recovery.ROUTE_OCR: self.ocr,
        }


def engine_versions(
    *,
    converter: LegacyFontConverter | None,
    lexicon: Lexicon | None,
    ocr: PageOcrEngine | None,
    extractor_version: str = "native-2",
) -> EngineVersions:
    """Per-route engine identities for the dependencies actually in hand.

    Every term is read off the dependency itself — `converter.name`/`.version`
    /`.mapping`, `lexicon.fingerprint`, the engine's `model`/`lang`/`backend`
    /`version` — rather than restated as constants, because a cache that named
    a version the installed package does not have would serve stale text with a
    fresh-looking key.

    The converter needs BOTH a converter and a lexicon to run at all
    (`recovery.convert_unit` refuses without either, and a converter without its
    guards is worse than no converter — §12.2), so one missing makes the route
    `unavailable`.
    """
    if converter is None or lexicon is None:
        legacy = UNAVAILABLE
    else:
        legacy = (
            f"{getattr(converter, 'name', 'converter')} "
            f"{getattr(converter, 'version', '?')}"
            f"/{getattr(converter, 'mapping', '?')}"
            f"/lexicon {lexicon.fingerprint[:12]}"
        )

    if ocr is None:
        ocr_version = UNAVAILABLE
    else:
        ocr_version = (
            f"{getattr(ocr, 'model', 'ocr')}"
            f"/{getattr(ocr, 'lang', '?')}"
            f"/{getattr(ocr, 'backend', '?')}"
            f"/{getattr(ocr, 'version', '') or 'unknown'}"
        )

    return EngineVersions(
        native=f"passthrough/{extractor_version}",
        legacy_conversion=legacy,
        ocr=ocr_version,
    )


# --------------------------------------------------------------------------- #
# What a cached row looks like in memory. Pure — no session, no file.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CachedUnit:
    unit_number: int
    label: str | None
    route: str
    reason: str
    engine_version: str
    ok: bool
    text: str
    error: str | None
    detail: dict[str, Any]

    def as_page(self) -> recovery.PageText:
        """Rebuild the real `PageText`. Note what is NOT restored: `indexable`.

        It is a property over `(ok, text)`, so a reused unit is judged by today's
        rule rather than by a boolean someone stored in August.
        """
        return recovery.PageText(
            page_number=self.unit_number,
            route=self.route,
            reason=self.reason,
            text=self.text,
            ok=self.ok,
            label=self.label,
            error=self.error,
            detail=dict(self.detail),
        )


@dataclass(frozen=True)
class CachedRecovery:
    content_sha256: str
    base_version: str
    family: str
    plan: str
    plan_reason: str
    gate_ratio: float | None
    warnings: tuple[str, ...]
    units: tuple[CachedUnit, ...]

    def stale(self, engines: EngineVersions) -> tuple[int, ...]:
        """Unit numbers whose route engine has moved on since they were stored."""
        return tuple(
            u.unit_number
            for u in self.units
            if u.engine_version != engines.for_route(u.route)
        )

    def as_document(self) -> recovery.RecoveredDocument:
        return recovery.RecoveredDocument(
            family=self.family,
            plan=self.plan,
            plan_reason=self.plan_reason,
            gate_ratio=self.gate_ratio,
            pages=tuple(u.as_page() for u in self.units),
            warnings=self.warnings,
        )


@dataclass
class CacheReport:
    """What one `resolve` did. The counters the small real-data check reports.

    `converter_units` and `ocr_units` are counts of units actually EXECUTED, not
    of units on that route — a warm pass must show zero for both, and that is
    the property worth asserting rather than inferring from equal output.
    """

    outcome: str = "cold"          # cold | warm | partial
    base_version: str = ""
    units_total: int = 0
    units_reused: int = 0
    units_recovered: int = 0
    converter_units: int = 0
    ocr_units: int = 0
    reparsed: bool = False         # did the blob have to be opened again?
    reason: str = ""               # why it was not a warm hit

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "base_version": self.base_version,
            "units_total": self.units_total,
            "units_reused": self.units_reused,
            "units_recovered": self.units_recovered,
            "converter_units": self.converter_units,
            "ocr_units": self.ocr_units,
            "reparsed": self.reparsed,
            "reason": self.reason,
        }


# --------------------------------------------------------------------------- #
# The resolution. Sync and database-free: the caller loads the row and stores
# the result, so this stays testable with a hand-built `CachedRecovery` and
# runs inside `asyncio.to_thread` without a session crossing the boundary.
# --------------------------------------------------------------------------- #
def resolve(
    path: Path,
    *,
    cached: CachedRecovery | None,
    converter: LegacyFontConverter | None = None,
    lexicon: Lexicon | None = None,
    ocr: PageOcrEngine | None = None,
    extractor_version: str = "native-2",
    cold: Callable[[], recovery.RecoveredDocument] | None = None,
) -> tuple[recovery.RecoveredDocument, CacheReport]:
    """One blob → its recovered document, doing only the work that is stale.

    Three outcomes:

    **cold** — no usable cached row, or the routing version moved. The blob is
    classified and routed from scratch. `cold` is the callable that does it
    (`rag.recover_blob`, injected so this module does not need to know how a
    blob is classified).

    **warm** — every unit's engine version still matches. Nothing is opened, no
    converter runs, no OCR runs. The document is rebuilt from the rows.

    **partial** — some units are stale. Only those are re-executed, through
    `recovery`'s own per-unit functions; the rest are reused as they are. For a
    PDF the page texts are re-read (cheap, pypdf) because a conversion needs its
    input, but the CLASSIFICATION is not redone — the route, the reason and the
    document's `gate_ratio` all come from the cached header, which is why the
    header stores `gate_ratio` at all.

    Non-PDF documents are all-or-nothing by construction: every unit of a
    workbook (or a `.docx`/`.txt`) shares one route, so "some units stale" cannot
    happen there and a stale one falls back to `cold`.
    """
    engines = engine_versions(
        converter=converter, lexicon=lexicon, ocr=ocr,
        extractor_version=extractor_version,
    )
    base = base_version(extractor_version)
    report = CacheReport(base_version=base)

    def run_cold(reason: str) -> tuple[recovery.RecoveredDocument, CacheReport]:
        if cold is None:  # pragma: no cover - the caller always supplies one
            raise ValueError("a cold recovery callable is required")
        recovered = cold()
        report.outcome = "cold"
        report.reason = reason
        report.reparsed = True
        report.units_total = len(recovered.pages)
        report.units_recovered = len(recovered.pages)
        counts = recovered.route_counts
        report.converter_units = counts.get(recovery.ROUTE_LEGACY, 0)
        report.ocr_units = counts.get(recovery.ROUTE_OCR, 0)
        return recovered, report

    if cached is None:
        return run_cold("miss")
    if cached.base_version != base:
        # The ROUTES may differ now, so no unit's answer is still about the
        # right question. Nothing is salvaged from a superseded routing version.
        return run_cold("base_version_changed")

    stale = set(cached.stale(engines))
    report.units_total = len(cached.units)
    if not stale:
        report.outcome = "warm"
        report.units_reused = len(cached.units)
        report.reason = "all_units_current"
        return cached.as_document(), report

    if cached.family != "pdf":
        # One route per document, so a stale unit means the whole thing is
        # stale. Re-classifying is the honest way to rebuild it.
        return run_cold("non_pdf_engine_changed")

    try:
        page_texts = list(file_documents.read_pdf_pages(path).pages)
    except Exception as exc:  # noqa: BLE001 - it parsed once; a failure now is real
        logger.warning(
            "NRB recovery cache: re-read failed for a partial refresh (%s); "
            "falling back to a cold recovery", type(exc).__name__,
        )
        return run_cold("reread_failed")

    pages: list[recovery.PageText] = []
    for unit in cached.units:
        if unit.unit_number not in stale:
            pages.append(unit.as_page())
            report.units_reused += 1
            continue

        native_text = (
            page_texts[unit.unit_number - 1]
            if 0 <= unit.unit_number - 1 < len(page_texts)
            else ""
        )
        pages.append(
            _refresh_unit(
                unit,
                path,
                native_text=native_text,
                gate_ratio=cached.gate_ratio,
                converter=converter,
                lexicon=lexicon,
                ocr=ocr,
                report=report,
            )
        )
        report.units_recovered += 1

    report.outcome = "partial"
    report.reparsed = True
    report.reason = f"{len(stale)} of {len(cached.units)} units stale"
    return (
        recovery.RecoveredDocument(
            family=cached.family,
            plan=cached.plan,
            plan_reason=cached.plan_reason,
            gate_ratio=cached.gate_ratio,
            pages=tuple(pages),
            warnings=cached.warnings,
        ),
        report,
    )


def _refresh_unit(
    unit: CachedUnit,
    path: Path,
    *,
    native_text: str,
    gate_ratio: float | None,
    converter: LegacyFontConverter | None,
    lexicon: Lexicon | None,
    ocr: PageOcrEngine | None,
    report: CacheReport,
) -> recovery.PageText:
    """Re-execute ONE stale unit on its cached route.

    The route is not re-decided. It was decided under this `base_version`, which
    has not changed, so re-deriving it here would be a second routing
    implementation with an opportunity to disagree — the thing this module
    exists not to be. `reason` is passed through for the same reason: a
    refreshed page must record the rule that routed it, not a new guess.
    """
    if unit.route == recovery.ROUTE_LEGACY:
        report.converter_units += 1
        return recovery.convert_unit(
            unit.unit_number,
            native_text,
            reason=unit.reason,
            converter=converter,
            lexicon=lexicon,
            document_legacy_ratio=gate_ratio or 0.0,
        )
    if unit.route == recovery.ROUTE_OCR:
        report.ocr_units += 1
        return recovery.ocr_unit(
            unit.unit_number, path, reason=unit.reason, engine=ocr
        )
    return recovery.PageText(
        unit.unit_number, recovery.ROUTE_NATIVE, unit.reason, native_text
    )


# --------------------------------------------------------------------------- #
# Persistence. Core statements, the `catalog.py` convention.
# --------------------------------------------------------------------------- #
async def load(
    session: AsyncSession, content_sha256: str, base: str
) -> CachedRecovery | None:
    """The cached recovery for these bytes under this routing version, or None."""
    header = (
        await session.execute(
            select(NRBRecovery).where(
                NRBRecovery.content_sha256 == content_sha256,
                NRBRecovery.base_version == base,
            )
        )
    ).scalar_one_or_none()
    if header is None:
        return None

    rows = (
        await session.execute(
            select(NRBRecoveryUnit)
            .where(NRBRecoveryUnit.recovery_id == header.id)
            .order_by(NRBRecoveryUnit.unit_number)
        )
    ).scalars().all()

    if len(rows) != header.unit_count:
        # A half-written cache entry is not a cache entry. Reporting it as a
        # miss re-runs the recovery and rewrites the row, which is the only
        # outcome that cannot serve an incomplete document.
        logger.warning(
            "NRB recovery cache: %s has %d units, header says %d — treating as "
            "a miss", content_sha256[:12], len(rows), header.unit_count,
        )
        return None

    return CachedRecovery(
        content_sha256=header.content_sha256,
        base_version=header.base_version,
        family=header.family,
        plan=header.plan,
        plan_reason=header.plan_reason,
        gate_ratio=header.gate_ratio,
        warnings=tuple(header.warnings or ()),
        units=tuple(
            CachedUnit(
                unit_number=r.unit_number,
                label=r.label,
                route=r.route,
                reason=r.reason,
                engine_version=r.engine_version,
                ok=r.ok,
                text=r.content,
                error=r.error,
                detail=dict(r.detail or {}),
            )
            for r in rows
        ),
    )


async def save(
    session: AsyncSession,
    *,
    content_sha256: str,
    recovered: recovery.RecoveredDocument,
    engines: EngineVersions,
    extractor_version: str = "native-2",
) -> int:
    """Write (or replace) this blob's recovery under the current routing version.

    Replace rather than merge: the unit set is rewritten wholesale, so a
    document whose page count or routes changed cannot leave an orphan unit
    behind claiming to be page 51. Rows under OTHER `base_version`s are
    untouched — those are kept side by side, exactly as native-1 and native-2
    extraction rows are, and only `purge` removes them.

    A `no_recovery` document (an OLE2 file, an image) has zero units and is
    still cached: "there is nothing this pipeline can do with these bytes" is a
    deterministic answer worth not re-deriving, and the empty unit set makes it
    a warm hit that produces no chunks and the same `NrbParseError`.
    """
    base = base_version(extractor_version)
    header_id = (
        await session.execute(
            pg_insert(NRBRecovery.__table__)
            .values(
                content_sha256=content_sha256,
                base_version=base,
                family=recovered.family,
                plan=recovered.plan,
                plan_reason=recovered.plan_reason,
                gate_ratio=recovered.gate_ratio,
                warnings=list(recovered.warnings),
                unit_count=len(recovered.pages),
            )
            .on_conflict_do_update(
                index_elements=["content_sha256", "base_version"],
                set_={
                    "family": recovered.family,
                    "plan": recovered.plan,
                    "plan_reason": recovered.plan_reason,
                    "gate_ratio": recovered.gate_ratio,
                    "warnings": list(recovered.warnings),
                    "unit_count": len(recovered.pages),
                },
            )
            .returning(NRBRecovery.__table__.c.id)
        )
    ).scalar_one()

    await session.execute(
        delete(NRBRecoveryUnit.__table__).where(
            NRBRecoveryUnit.__table__.c.recovery_id == header_id
        )
    )
    rows = [
        {
            "recovery_id": header_id,
            "unit_number": page.page_number,
            "label": page.label,
            "route": page.route,
            "reason": page.reason[:64],
            "engine_version": engines.for_route(page.route),
            "ok": page.ok,
            # `page.text` is already post-`_withhold`: `recovery` blanked the
            # unresolved units before this document existed. Nothing here has to
            # filter, and nothing here is able to reintroduce the original.
            "content": page.text,
            "error": (page.error or None) and page.error[:2000],
            "detail": page.detail or {},
        }
        for page in recovered.pages
    ]
    if rows:
        await session.execute(NRBRecoveryUnit.__table__.insert(), rows)
    return header_id


async def purge(
    session: AsyncSession,
    *,
    content_sha256: str | None = None,
    base: str | None = None,
    keep_current: bool = False,
    extractor_version: str = "native-2",
) -> int:
    """Delete cached recoveries. Explicit operator action, never automatic.

    Units go with their header (`ON DELETE CASCADE`). This is the "refresh"
    half of the failure semantics: a unit whose engine ERRORED transiently
    (`conversion_failed`, an OCR page that raised) is cached under a version
    that has not moved, so it will be reused until someone decides otherwise.
    Deciding otherwise is this function, and it is deliberately a command rather
    than a heuristic — there is no transient-vs-permanent classifier here.
    """
    stmt = delete(NRBRecovery.__table__)
    if content_sha256:
        stmt = stmt.where(NRBRecovery.__table__.c.content_sha256 == content_sha256)
    if base:
        stmt = stmt.where(NRBRecovery.__table__.c.base_version == base)
    if keep_current:
        stmt = stmt.where(
            NRBRecovery.__table__.c.base_version != base_version(extractor_version)
        )
    result = await session.execute(stmt)
    return result.rowcount or 0


async def stats(session: AsyncSession) -> dict[str, Any]:
    """Route split and version census — §18's verification query, in SQL.

    "Verify a worker image by its route split on a known blob, never by whether
    ingestion succeeded" (§18). With the cache in place that split is a GROUP BY
    over `nrb_recovery_units` rather than a JSONB unnest over chunks.
    """
    versions = (
        await session.execute(
            select(
                NRBRecovery.base_version,
                func.count().label("documents"),
                func.sum(NRBRecovery.unit_count).label("units"),
            ).group_by(NRBRecovery.base_version)
        )
    ).all()
    routes = (
        await session.execute(
            select(
                NRBRecoveryUnit.route,
                NRBRecoveryUnit.engine_version,
                NRBRecoveryUnit.ok,
                func.count().label("units"),
            ).group_by(
                NRBRecoveryUnit.route, NRBRecoveryUnit.engine_version,
                NRBRecoveryUnit.ok,
            )
        )
    ).all()
    return {
        "versions": [
            {"base_version": v, "documents": d, "units": int(u or 0)}
            for v, d, u in versions
        ],
        "routes": [
            {"route": r, "engine_version": e, "ok": ok, "units": n}
            for r, e, ok, n in routes
        ],
    }


# --------------------------------------------------------------------------- #
# The async coordinator the worker calls. The only place the two halves meet.
# --------------------------------------------------------------------------- #
async def chunks_for_blob(
    Session,
    path: Path,
    *,
    content_sha256: str,
    max_chars: int,
    overlap_chars: int,
    extractor_version: str = "native-2",
    **injected: Any,
):
    """Load → recover (off the event loop) → store. Returns `(chunks, report)`.

    Three properties this shape exists for:

    **The slow work still runs in a thread.** `resolve` and everything under it
    is synchronous and CPU-bound (pypdf, npttf2utf, ONNX), so it goes through
    `asyncio.to_thread` exactly as the generic parser does — otherwise a 50-page
    OCR would starve the heartbeat and the stale sweep would kill a healthy job.

    **No session is open while it runs.** The cached row is read and the
    connection released before the work starts, and a fresh session writes the
    result afterwards. Holding a transaction across a multi-minute recovery is
    the mistake `DocSnapshot` already exists to avoid.

    **A warm hit is not rewritten.** There is nothing to write — the rows are
    already current — so a repeat ingest of an unchanged corpus performs one
    SELECT and no INSERT. A partial refresh does rewrite, because some units
    changed.

    A cache write that fails is logged and swallowed. The chunks are correct
    either way; losing the cache costs time on the next ingest, and failing a
    job that produced good text because a cache INSERT lost a race would be a
    strictly worse outcome.
    """
    import asyncio

    from .rag import recover_and_chunk, resolve_dependencies

    base = base_version(extractor_version)
    # No identity, no cache. A blank hash would make every such document share
    # one row, which is worse than not caching at all. `documents.content_hash`
    # is NOT NULL, so this is a guard rather than an expected path.
    cached = None
    if content_sha256:
        async with Session() as session:
            cached = await load(session, content_sha256, base)
            await session.rollback()  # read-only: end the transaction at once

    result = await asyncio.to_thread(
        recover_and_chunk,
        path,
        max_chars=max_chars,
        overlap_chars=overlap_chars,
        extractor_version=extractor_version,
        cached=cached,
        **injected,
    )
    report: CacheReport = result.report

    if content_sha256 and report is not None and report.outcome != "warm":
        # The SAME triple the recovery just used — `resolve_dependencies` is
        # shared with `recover_blob` precisely so the version written here
        # cannot name a converter the run did not have.
        engines = engine_versions(
            extractor_version=extractor_version,
            **resolve_dependencies(injected),
        )
        try:
            async with Session() as session:
                await save(
                    session,
                    content_sha256=content_sha256,
                    recovered=result.recovered,
                    engines=engines,
                    extractor_version=extractor_version,
                )
                await session.commit()
        except Exception:  # noqa: BLE001 - a cache miss is not a failed ingest
            logger.warning(
                "NRB recovery cache: could not store %s", content_sha256[:12],
                exc_info=True,
            )
    return result.chunks, report
