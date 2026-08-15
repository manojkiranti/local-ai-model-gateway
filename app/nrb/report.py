"""Aggregation and rendering of the NRB inventories. Pure — no I/O.

Two inventories, one module:

  * `summarize` / `render` — the **Phase 2 sitemap** inventory (what URLs exist).
  * `summarize_documents` / `render_documents` — the **Phase 3 document**
    inventory (what those URLs actually are, and where their files live).

Split out of the CLIs so the numbers are testable: both summarizers are
deterministic functions of their input, which is what makes "the counts are
stable" something a unit test can assert rather than something a developer
eyeballs.

Report choices worth naming, all about not flattering the extractor:

  * Counts are ordered by size then name, so re-running produces a
    byte-identical report and a diff between two runs is a real change.
  * `unmapped_categories` and `unrecognised_path_roots` are broken out from the
    `other` bucket. `other` is a number; those two are the actual to-do list for
    improving classification, and they are what the next phase should be reviewed
    against.
  * The Phase 3 report leads with what would block Phase 4: failures by kind,
    posts with **no** attachment, off-host attachments, and the share of posts
    whose document type is still unknown. A report that opens with "18,000
    documents found" and buries those is the wrong report.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Sequence

from .attachments import RESOURCE_TYPES as ATTACHMENT_TYPES
from .classify import RESOURCE_TYPES, SECTIONS
from .http import FETCH_KINDS

__all__ = [
    "summarize", "render", "SAMPLE_SIZE",
    "summarize_documents", "render_documents",
    "summarize_sync", "render_sync",
    "summarize_fetch", "render_fetch",
    "summarize_sample", "render_sample",
    "summarize_extraction", "render_extraction", "LEGACY_BANDS",
    "summarize_calibration", "render_calibration", "PREVIEW_CHARS",
]

SAMPLE_SIZE = 25   # bounded: a sample to inspect, not a second copy of the data


def _ordered(counter: Counter[str], preferred: tuple[str, ...] = ()) -> dict[str, int]:
    """Counts in a stable order: preferred vocabulary first, then size, then name."""
    known = {name: counter[name] for name in preferred if counter[name]}
    rest = sorted(
        ((name, count) for name, count in counter.items() if name not in known),
        key=lambda item: (-item[1], item[0]),
    )
    known.update(rest)
    return known


def summarize(inventory: Any, sample_size: int = SAMPLE_SIZE) -> dict[str, Any]:
    """A JSON-ready summary of one discovery run.

    `inventory` is an `app.nrb.sitemap.Inventory`; typed loosely to keep this
    module free of the import cycle (sitemap imports classify, and the CLI is the
    only thing that needs both).
    """
    sections: Counter[str] = Counter()
    resources: Counter[str] = Counter()
    departments: Counter[str] = Counter()
    page_kinds: Counter[str] = Counter()
    unmapped_categories: Counter[str] = Counter()
    unrecognised_roots: Counter[str] = Counter()
    document_owners: Counter[str] = Counter()
    lastmods: list[str] = []

    for entry in inventory.urls:
        sections[entry.section] += 1
        resources[entry.resource_type] += 1
        departments[entry.department or "unknown"] += 1
        page_kinds[entry.page_kind] += 1
        if entry.page_kind == "document_post":
            document_owners[entry.department or "unknown"] += 1
        if entry.evidence.startswith("unmapped category root "):
            unmapped_categories[
                entry.evidence.removeprefix("unmapped category root ")
            ] += 1
        if entry.evidence.startswith("unrecognised path root "):
            unrecognised_roots[entry.evidence.removeprefix("unrecognised path root ")] += 1
        if entry.last_modified:
            lastmods.append(entry.last_modified)

    # Sorted before slicing so the sample is reproducible run to run.
    unclassified = sorted(
        entry.url for entry in inventory.urls if entry.section in ("unknown", "other")
    )

    return {
        "root": inventory.root,
        "sitemaps_fetched": len(inventory.sitemaps_fetched),
        "sitemap_urls": list(inventory.sitemaps_fetched),
        "entries_discovered": inventory.total_entries,
        "unique_urls": inventory.unique_urls,
        "duplicates": inventory.duplicates,
        "by_section": _ordered(sections, SECTIONS),
        "by_resource_type": _ordered(resources, RESOURCE_TYPES),
        "by_page_kind": _ordered(page_kinds),
        "by_department": _ordered(departments),
        "document_posts_by_owner": _ordered(document_owners),
        "unmapped_categories": _ordered(unmapped_categories),
        "unrecognised_path_roots": _ordered(unrecognised_roots),
        "lastmod_earliest": min(lastmods) if lastmods else None,
        "lastmod_latest": max(lastmods) if lastmods else None,
        "unclassified_total": len(unclassified),
        "unclassified_sample": unclassified[:sample_size],
        "rejected": [list(item) for item in inventory.rejected[:sample_size]],
        "rejected_total": len(inventory.rejected),
        "errors": [list(item) for item in inventory.errors[:sample_size]],
        "errors_total": len(inventory.errors),
        "truncated": list(inventory.truncated),
    }


def _block(title: str, counts: dict[str, int], total: int | None = None) -> list[str]:
    if not counts:
        return [f"{title}:", "  (none)", ""]
    lines = [f"{title}:"]
    width = max(len(name) for name in counts)
    for name, count in counts.items():
        share = f"  {count / total:6.1%}" if total else ""
        lines.append(f"  {name.ljust(width)}  {count:>7,}{share}")
    lines.append("")
    return lines


def render(summary: dict[str, Any]) -> str:
    """The human-readable report. The JSON output is the same data, unformatted."""
    unique = summary["unique_urls"]
    out: list[str] = [
        "Nepal Rastra Bank sitemap discovery",
        "=" * 72,
        f"Root sitemap:      {summary['root']}",
        f"Sitemaps fetched:  {summary['sitemaps_fetched']:,}",
        f"URLs discovered:   {summary['entries_discovered']:,}",
        f"Unique URLs:       {unique:,}  ({summary['duplicates']:,} duplicates)",
        f"lastmod range:     {summary['lastmod_earliest']} .. {summary['lastmod_latest']}",
        "",
    ]
    if summary["truncated"]:
        out += [
            "!! INVENTORY TRUNCATED — a bound was reached, so the counts below are",
            "!! a floor, not the site total: " + ", ".join(summary["truncated"]),
            "",
        ]

    out += _block("By section", summary["by_section"], unique)
    out += _block("By page kind", summary["by_page_kind"], unique)
    out += _block("By resource type", summary["by_resource_type"], unique)
    out += _block("By department/office code", summary["by_department"], unique)
    out += _block("Document posts by owner", summary["document_posts_by_owner"])

    if summary["unmapped_categories"]:
        out += _block(
            "Unmapped /category/ roots (extend classify.CATEGORY_SECTIONS)",
            summary["unmapped_categories"],
        )
    if summary["unrecognised_path_roots"]:
        out += _block(
            "Unrecognised path roots (new post type? new section?)",
            summary["unrecognised_path_roots"],
        )

    out.append(
        f"Unclassified (section unknown/other): {summary['unclassified_total']:,}"
        f" — showing {len(summary['unclassified_sample'])}:"
    )
    out += [f"  {url}" for url in summary["unclassified_sample"]] or ["  (none)"]
    out.append("")

    for label, items, total in (
        ("Rejected (off-host / unfetchable)", summary["rejected"], summary["rejected_total"]),
        ("Errors", summary["errors"], summary["errors_total"]),
    ):
        out.append(f"{label}: {total:,}")
        out += [f"  {why}  <- {url}" for url, why in items]
        out.append("")

    return "\n".join(out)


# =========================================================================== #
# Phase 3: the document inventory
# =========================================================================== #
def _examples(items: list[Any], limit: int) -> list[Any]:
    """A bounded, sorted sample. Sorted before slicing so runs are comparable."""
    return sorted(items)[:limit]


def summarize_documents(
    documents: list[Any],
    *,
    attempted: int | None = None,
    errors: list[Any] | None = None,
    probes: list[Any] | None = None,
    rest_unavailable: list[str] | None = None,
    sample_size: int = SAMPLE_SIZE,
) -> dict[str, Any]:
    """A JSON-ready summary of one document-discovery run.

    `documents` are `NRBDocument`s, `errors` are `FetchError`s from the REST
    enumeration, and `probes` are optional `PageProbe`s from the verification
    pass. Typed loosely to keep this module import-light and free of cycles.
    """
    errors = errors or []
    probes = probes or []

    owners: Counter[str] = Counter()
    sections: Counter[str] = Counter()
    primary: Counter[str] = Counter()
    resource_types: Counter[str] = Counter()
    type_sources: Counter[str] = Counter()
    extensions: Counter[str] = Counter()
    mimes: Counter[str] = Counter()
    sources: Counter[str] = Counter()
    attachment_hosts: Counter[str] = Counter()
    per_post: Counter[int] = Counter()
    extras_seen: Counter[str] = Counter()
    category_labels: Counter[str] = Counter()
    unmapped_categories: Counter[str] = Counter()
    failure_kinds: Counter[str] = Counter()

    by_year: Counter[str] = Counter()
    typed_by_year: Counter[str] = Counter()
    total_links = 0
    unique_urls: set[str] = set()
    duplicate_refs = 0
    off_host: list[str] = []
    malformed: list[str] = []
    pdf_like = 0
    non_pdf = 0
    no_attachment: list[str] = []
    multi_attachment: list[str] = []
    unknown_type: list[str] = []
    canonical_mismatch: list[str] = []
    no_title: list[str] = []
    no_date: list[str] = []
    warned: list[str] = []
    redirect_flag: Counter[str] = Counter()

    for document in documents:
        owners[document.owner or "unknown"] += 1
        primary[document.primary_section] += 1
        for section in document.sections:
            sections[section] += 1
        if not document.sections:
            unknown_type.append(document.url)
        for label in document.category_names:
            category_labels[label] += 1
        for evidence in document.section_evidence:
            if evidence.startswith("unmapped category "):
                unmapped_categories[evidence.removeprefix("unmapped category ")] += 1
        for key in document.extras:
            extras_seen[key] += 1
        redirect_flag[str(document.redirects_to_file)] += 1

        # Coverage by publication year, because the corpus is not homogeneous:
        # NRB's 2019 CMS migration dumped ~9,000 legacy documents into a single
        # catch-all category, so one blended percentage hides both the real
        # backlog and the much higher coverage of everything published since.
        year = (document.published or "")[:4] or "unknown"
        by_year[year] += 1
        if document.sections:
            typed_by_year[year] += 1

        if not document.title:
            no_title.append(document.url)
        if not document.published:
            no_date.append(document.url)
        if document.warnings:
            warned.append(document.url)
        # WordPress's `link` is its canonical URL; a mismatch after normalization
        # would mean our normalizer changed something load-bearing.
        if document.canonical_url and document.canonical_url != document.url:
            canonical_mismatch.append(document.url)

        count = document.attachment_count
        per_post[count] += 1
        if count == 0:
            no_attachment.append(document.url)
        elif count > 1:
            multi_attachment.append(document.url)

        for attachment in document.attachments:
            total_links += 1
            # Keyed on the comparison form: NRB publishes the same file with
            # literal and percent-encoded Devanagari, and counting both would
            # overstate the corpus.
            if attachment.dedup_key in unique_urls:
                duplicate_refs += 1
            unique_urls.add(attachment.dedup_key)
            resource_types[attachment.resource_type] += 1
            type_sources[attachment.type_source] += 1
            extensions[attachment.extension or "(none)"] += 1
            mimes[attachment.mime_type or "(not recorded)"] += 1
            sources[attachment.source] += 1
            if attachment.resource_type == "pdf":
                pdf_like += 1
            else:
                non_pdf += 1
            if attachment.on_allowed_host:
                attachment_hosts["www.nrb.org.np (configured NRB host)"] += 1
            else:
                attachment_hosts[attachment.host_reason or "off-host"] += 1
                off_host.append(attachment.url)
            if attachment.extension is None and attachment.mime_type is None:
                malformed.append(attachment.url)

    for error in errors:
        failure_kinds[error.kind] += 1

    probe_outcomes: Counter[str] = Counter()
    probe_agree = probe_disagree = 0
    probe_examples: list[list[str]] = []
    for probe in probes:
        probe_outcomes[probe.outcome] += 1
        expected = getattr(probe, "expected_attachment", None)
        if expected is None:
            continue
        if probe.final_url == expected:
            probe_agree += 1
        else:
            probe_disagree += 1
            if len(probe_examples) < sample_size:
                probe_examples.append([probe.url, str(probe.final_url), str(expected)])

    total = len(documents)
    with_sections = total - len(unknown_type)
    return {
        "pages_attempted": attempted if attempted is not None else total,
        "documents_normalized": total,
        # Post types the sitemap lists but REST does not serve: a corpus gap, and
        # deliberately not folded into the failure count.
        "post_types_not_served_by_rest": sorted(rest_unavailable or []),
        "fetch_failures": sum(failure_kinds.values()),
        "failures_by_kind": _ordered(failure_kinds, FETCH_KINDS),
        "failure_examples": [[e.kind, e.url, e.detail] for e in errors[:sample_size]],
        # --- attachments ---
        "attachment_links_total": total_links,
        "attachment_urls_unique": len(unique_urls),
        "duplicate_attachment_references": duplicate_refs,
        "posts_by_attachment_count": {str(k): v for k, v in sorted(per_post.items())},
        "posts_with_no_attachment": len(no_attachment),
        "posts_with_one_attachment": per_post.get(1, 0),
        "posts_with_multiple_attachments": len(multi_attachment),
        "pdf_like_attachments": pdf_like,
        "non_pdf_attachments": non_pdf,
        "by_resource_type": _ordered(resource_types, ATTACHMENT_TYPES),
        "by_type_source": _ordered(type_sources, ("mime", "extension", "none")),
        "by_extension": _ordered(extensions),
        "by_mime_type": _ordered(mimes),
        "by_attachment_source": _ordered(sources),
        "attachment_hosts": _ordered(attachment_hosts),
        "off_host_attachments": len(off_host),
        "off_host_examples": _examples(off_host, sample_size),
        "untyped_attachments": len(malformed),
        "untyped_examples": _examples(malformed, sample_size),
        # --- metadata quality ---
        "documents_with_title": total - len(no_title),
        "documents_without_title": len(no_title),
        "documents_with_published_date": total - len(no_date),
        "documents_without_published_date": len(no_date),
        "canonical_url_mismatches": len(canonical_mismatch),
        "canonical_mismatch_examples": _examples(canonical_mismatch, sample_size),
        "documents_with_warnings": len(warned),
        "redirects_to_file": dict(sorted(redirect_flag.items())),
        # --- classification ---
        "documents_with_known_type": with_sections,
        "documents_with_unknown_type": len(unknown_type),
        "type_coverage": round(with_sections / total, 4) if total else 0.0,
        "type_coverage_by_year": {
            year: {
                "documents": by_year[year],
                "typed": typed_by_year[year],
                "coverage": round(typed_by_year[year] / by_year[year], 4),
            }
            for year in sorted(by_year)
        },
        "by_primary_section": _ordered(primary, SECTIONS),
        "by_section_any": _ordered(sections, SECTIONS),
        "by_owner": _ordered(owners),
        "category_labels_observed": _ordered(category_labels),
        "unmapped_categories": _ordered(unmapped_categories),
        "acf_fields_observed": _ordered(extras_seen),
        # --- verification pass ---
        "probes_run": len(probes),
        "probe_outcomes": _ordered(probe_outcomes),
        "probe_attachment_agreements": probe_agree,
        "probe_attachment_disagreements": probe_disagree,
        "probe_disagreement_examples": probe_examples,
        # --- samples ---
        "no_attachment_examples": _examples(no_attachment, sample_size),
        "multi_attachment_examples": _examples(multi_attachment, sample_size),
        "unknown_type_examples": _examples(unknown_type, sample_size),
    }


def render_documents(summary: dict[str, Any]) -> str:
    """The human-readable Phase 3 report.

    Ordered by what would stop Phase 4: failures, then missing attachments, then
    host trust, then classification coverage. The headline count comes last on
    purpose — "18,000 documents discovered" is the least actionable line here.
    """
    total = summary["documents_normalized"] or 1
    out: list[str] = [
        "Nepal Rastra Bank document discovery (Phase 3)",
        "=" * 72,
        f"Posts attempted:      {summary['pages_attempted']:,}",
        f"Documents normalized: {summary['documents_normalized']:,}",
        f"Fetch failures:       {summary['fetch_failures']:,}",
        "",
    ]
    if summary["post_types_not_served_by_rest"]:
        out += [
            "!! These post types are in the sitemap but NOT served by the REST API,",
            "!! so their documents are absent from the counts below:",
            "     " + ", ".join(summary["post_types_not_served_by_rest"]),
            "",
        ]
    if summary["failures_by_kind"]:
        out += _block("Failures by kind", summary["failures_by_kind"])
        out += ["  examples:"]
        out += [f"    {kind}: {url}  ({detail})"
                for kind, url, detail in summary["failure_examples"]]
        out.append("")

    out += [
        "Attachments",
        f"  links found:        {summary['attachment_links_total']:,}",
        f"  unique URLs:        {summary['attachment_urls_unique']:,}"
        f"  ({summary['duplicate_attachment_references']:,} duplicate references)",
        f"  PDF-looking:        {summary['pdf_like_attachments']:,}",
        f"  non-PDF:            {summary['non_pdf_attachments']:,}",
        f"  off-host:           {summary['off_host_attachments']:,}",
        f"  untyped:            {summary['untyped_attachments']:,}",
        "",
    ]
    out += _block("Posts by attachment count",
                  {k: v for k, v in summary["posts_by_attachment_count"].items()}, total)
    out += _block("By resource type", summary["by_resource_type"])
    out += _block("Type determined from", summary["by_type_source"])
    out += _block("By extension", summary["by_extension"])
    out += _block("By recorded MIME type", summary["by_mime_type"])
    out += _block("Attachment discovered via", summary["by_attachment_source"])
    out += _block("Attachment hosts", summary["attachment_hosts"])

    out += [
        "Metadata availability",
        f"  title present:      {summary['documents_with_title']:,}"
        f" / {summary['documents_normalized']:,}",
        f"  published date:     {summary['documents_with_published_date']:,}"
        f" / {summary['documents_normalized']:,}",
        f"  canonical mismatch: {summary['canonical_url_mismatches']:,}",
        f"  posts with warnings:{summary['documents_with_warnings']:,}",
        "",
    ]

    out += [
        "Document type (from NRB's own category metadata)",
        f"  determined:         {summary['documents_with_known_type']:,}"
        f"  ({summary['type_coverage']:.1%})",
        f"  still unknown:      {summary['documents_with_unknown_type']:,}",
        "",
    ]
    coverage_rows = summary["type_coverage_by_year"]
    if len(coverage_rows) > 1:
        out.append("  by publication year:")
        out += [
            f"    {year}  {row['typed']:>6,}/{row['documents']:>6,}  {row['coverage']:6.1%}"
            for year, row in coverage_rows.items()
        ]
        out.append("")
    out += _block("By primary section", summary["by_primary_section"], total)
    out += _block("By section (any, posts may hold several)", summary["by_section_any"])
    out += _block("By owner", summary["by_owner"], total)
    out += _block("ACF fields observed", summary["acf_fields_observed"])
    if summary["unmapped_categories"]:
        out += _block("Unmapped categories (extend classify.CATEGORY_SECTIONS)",
                      summary["unmapped_categories"])

    if summary["probes_run"]:
        out += [
            "Page verification probe",
            f"  probes run:         {summary['probes_run']:,}",
        ]
        out += [f"    {name}: {count:,}"
                for name, count in summary["probe_outcomes"].items()]
        out += [
            f"  redirect target matches acf.document_file: "
            f"{summary['probe_attachment_agreements']:,}",
            f"  disagreements:      {summary['probe_attachment_disagreements']:,}",
        ]
        out += [f"    {url}\n      probe={final}\n      rest ={expected}"
                for url, final, expected in summary["probe_disagreement_examples"]]
        out.append("")

    for label, key in (
        ("Posts with NO attachment", "no_attachment_examples"),
        ("Posts with MULTIPLE attachments", "multi_attachment_examples"),
        ("Posts with unknown document type", "unknown_type_examples"),
        ("Off-host attachments", "off_host_examples"),
    ):
        items = summary[key]
        out.append(f"{label} (showing {len(items)}):")
        out += [f"  {url}" for url in items] or ["  (none)"]
        out.append("")

    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Phase 4 — one catalog sync
# --------------------------------------------------------------------------- #
def summarize_sync(result: Any) -> dict[str, Any]:
    """A JSON-ready summary of one sync run.

    `result` is an `app.nrb.sync.SyncResult`; typed loosely for the same reason as
    `summarize` — this module stays free of the model/session imports so it can be
    unit-tested with a plain object.
    """
    return {
        "run_id": result.run_id,
        "status": result.status,
        "dry_run": result.dry_run,
        "discovery_complete": result.discovery_complete,
        "deactivation_applied": result.deactivation_applied,
        "discovery_seconds": round(result.discovery_seconds, 1),
        "duration_seconds": round(result.duration_seconds, 1),
        "counters": dict(result.counters),
        "database": dict(result.counts),
        "notes": dict(result.notes),
    }


def render_sync(summary: dict[str, Any]) -> str:
    """The operator's view of a sync.

    Ordered by what a reader needs to *decide* something: whether the run was
    complete, then what changed, then the integrity checks. `created`/`updated`
    are the meaningful numbers and `unchanged` is the idempotency evidence — on a
    second run against an unchanged NRB, the first two are 0 and the third is
    everything.
    """
    counters = summary["counters"]
    database = summary["database"]
    notes = summary["notes"]
    dry = "  (DRY RUN — nothing was written)" if summary["dry_run"] else ""
    out: list[str] = [
        f"NRB catalog sync — {summary['status']}{dry}",
        "=" * 72,
        f"Run id:               {summary['run_id']}",
        f"Discovery:            {summary.get('discovery_seconds', 0):,.1f}s",
        f"Reconcile:            {summary['duration_seconds']:,.1f}s",
        f"Discovery complete:   {summary['discovery_complete']}",
        f"Deactivation applied: {summary['deactivation_applied']}",
        "",
        "Sources",
        f"  seen:               {counters['sources_seen']:>8,}",
        f"  created:            {counters['sources_created']:>8,}",
        f"  updated:            {counters['sources_updated']:>8,}",
        f"  unchanged:          {counters['sources_unchanged']:>8,}",
        f"  reactivated:        {counters['sources_reactivated']:>8,}",
        f"  deactivated:        {counters['sources_deactivated']:>8,}",
        f"  sitemap-only:       {counters['sitemap_only_sources']:>8,}",
        "",
        "Files",
        f"  seen:               {counters['files_seen']:>8,}",
        f"  created:            {counters['files_created']:>8,}",
        f"  updated:            {counters['files_updated']:>8,}",
        f"  unchanged:          {counters['files_unchanged']:>8,}",
        f"  blocked (unfetchable): {counters['blocked_files']:>5,}",
        "",
        "Relationships",
        f"  created:            {counters['relationships_created']:>8,}",
        f"  updated:            {counters['relationships_updated']:>8,}",
        f"  removed:            {counters['relationships_removed']:>8,}",
        "",
        "Discovery",
        f"  sitemaps read:      {counters['sitemaps_seen']:>8,}",
        f"  sitemap URLs:       {notes.get('sitemap_urls_seen', 0):>8,}",
        f"  sitemap documents:  {notes.get('sitemap_document_urls', 0):>8,}",
        f"  errors:             {counters['error_count']:>8,}",
        f"  warnings:           {counters['warning_count']:>8,}",
        "",
    ]

    if notes.get("post_types_not_served"):
        out += [
            "Post types the REST API does not serve (covered from the sitemap):",
            "  " + ", ".join(notes["post_types_not_served"]),
            "",
        ]
    if notes.get("skipped_sitemap_page_kinds"):
        out.append("Sitemap URLs not persisted as sources (not document posts):")
        out += [f"  {kind:<24} {count:>8,}"
                for kind, count in notes["skipped_sitemap_page_kinds"].items()]
        out.append("")
    if notes.get("truncated"):
        out += ["!! bounds that truncated discovery:",
                "     " + ", ".join(notes["truncated"]), ""]
    if notes.get("deactivation_skipped"):
        out += ["!! absence-based deactivation was SKIPPED:",
                f"     {notes['deactivation_skipped']}", ""]

    if database:
        out += [
            "Database after the run",
            f"  sources:            {database.get('sources', 0):>8,}"
            f"  (active {database.get('active_sources', 0):,},"
            f" inactive {database.get('inactive_sources', 0):,})",
            f"  from REST:          {database.get('rest_sources', 0):>8,}",
            f"  sitemap-only:       {database.get('sitemap_only_sources', 0):>8,}",
            f"  untyped:            {database.get('untyped_sources', 0):>8,}",
            f"  files:              {database.get('files', 0):>8,}"
            f"  (blocked {database.get('blocked_files', 0):,})",
            f"  relationships:      {database.get('relationships', 0):>8,}",
            # Both are enforced by unique indexes. Printed anyway: a report that
            # only shows what it assumes to be true is not evidence.
            f"  duplicate source identities: {database.get('duplicate_source_identities', 0):,}",
            f"  duplicate comparison keys:   {database.get('duplicate_comparison_keys', 0):,}",
            "",
        ]

    for label, key in (("Errors", "errors"), ("Warnings", "warnings")):
        items = notes.get(key) or []
        if items:
            out.append(f"{label} (showing {len(items)}):")
            out += [f"  {item}" for item in items]
            out.append("")

    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Phase 5 — one download pass
# --------------------------------------------------------------------------- #
def _mb(value: Any) -> str:
    return f"{(value or 0) / 1_048_576:,.1f} MB"


def summarize_fetch(result: Any) -> dict[str, Any]:
    """A JSON-ready summary of one download pass.

    `result` is an `app.nrb.fetch.FetchResult`; typed loosely for the same reason as
    the other summarizers — this module stays free of model/session imports.
    """
    return {
        "run_id": result.run_id,
        "status": result.status,
        "dry_run": result.dry_run,
        "duration_seconds": round(result.duration_seconds, 1),
        "scope": dict(result.scope),
        "counters": dict(result.counters),
        "database": dict(result.counts),
        "notes": dict(result.notes),
    }


def render_fetch(summary: dict[str, Any]) -> str:
    """The operator's view of a download pass.

    Leads with the scope, because a fetch is always a slice of the corpus and every
    number below is meaningless without knowing which slice. `downloaded` vs
    `stored` is the deduplication measure: they differ by exactly the files whose
    bytes were already on disk under a different URL.
    """
    counters = summary["counters"]
    notes = summary["notes"]
    scope = summary["scope"]
    database = summary["database"]
    dry = "  (DRY RUN — nothing was downloaded)" if summary["dry_run"] else ""
    selected = counters.get("files_selected", 0)

    out: list[str] = [
        f"NRB file fetch — {summary['status']}{dry}",
        "=" * 72,
        f"Run id:               {summary['run_id']}",
        f"Duration:             {summary['duration_seconds']:,.1f}s",
        "",
        "Scope",
        f"  sections:           {', '.join(scope.get('sections') or []) or '(any)'}",
        f"  owners:             {', '.join(scope.get('owners') or []) or '(any)'}",
        f"  resource types:     {', '.join(scope.get('resource_types') or []) or '(any)'}",
        f"  years:              {', '.join(str(y) for y in scope.get('years') or []) or '(any)'}",
        f"  manifest keys:      {scope.get('manifest_keys') or '(none)'}",
        f"  limit:              {scope.get('limit') if scope.get('limit') is not None else '(none)'}",
        f"  retry failed:       {scope.get('retry_failed')}",
        f"  byte budget:        {_mb(scope['max_bytes']) if scope.get('max_bytes') else '(none)'}",
        "",
        "Files",
        f"  selected:           {selected:>8,}",
        f"  fetched:            {counters.get('files_fetched', 0):>8,}",
        f"  failed:             {counters.get('files_failed', 0):>8,}",
        f"  skipped (budget):   {counters.get('files_skipped', 0):>8,}",
        f"  already on disk:    {counters.get('files_deduplicated', 0):>8,}"
        "   (same bytes, another URL)",
        "",
        "Bytes",
        f"  NRB reported:       {_mb(notes.get('reported_bytes_selected')):>12}",
        f"  downloaded:         {_mb(counters.get('bytes_downloaded')):>12}",
        f"  newly stored:       {_mb(counters.get('bytes_stored')):>12}",
        "",
    ]
    manifest = notes.get("manifest")
    if manifest:
        # The cohort's own accounting. `selected` above is only the slice this pass
        # will attempt, so without these lines a manifest already on disk reads as
        # a manifest that lost its files. Named states, not one total: they mean
        # different things, and only `unknown to the catalog` is a defect.
        by_status = manifest.get("by_status") or {}
        out += [
            "Manifest cohort",
            f"  requested:          {manifest.get('requested', 0):>8,}"
            + (f"   ({manifest['duplicate_keys']} duplicate entries collapsed)"
               if manifest.get("duplicate_keys") else ""),
            f"  already fetched:    {by_status.get('fetched', 0):>8,}",
            f"  pending:            {by_status.get('pending', 0):>8,}",
            f"  previously failed:  {by_status.get('failed', 0):>8,}"
            "   (needs --retry-failed)",
            f"  blocked:            {by_status.get('blocked_host', 0):>8,}"
            "   (host guard; never fetchable)",
            f"  fetched this pass:  {counters.get('files_fetched', 0):>8,}",
            f"  failed this pass:   {counters.get('files_failed', 0):>8,}",
            f"  not in the catalog: {manifest.get('missing_count', 0):>8,}",
            "",
        ]
        if manifest.get("missing"):
            shown = manifest["missing"]
            total = manifest["missing_count"]
            noun = "key names" if total == 1 else "keys name"
            out.append(
                f"!! {total:,} manifest {noun} no catalog row (showing "
                f"{len(shown)}) — the manifest and the catalog have diverged; "
                f"re-sync, or re-draw the cohort:"
            )
            out += [f"  {key}" for key in shown]
            out.append("")

    if notes.get("unknown_size_files"):
        out += [
            f"{notes['unknown_size_files']:,} selected files report no size, so the "
            "reported total is a floor.",
            "",
        ]
    if notes.get("stopped"):
        out += ["!! the pass stopped early:", f"     {notes['stopped']}", ""]

    if database:
        out += [
            "File catalog after the pass",
            f"  pending:            {database.get('pending', 0):>8,}",
            f"  fetched:            {database.get('fetched', 0):>8,}",
            f"  failed:             {database.get('failed', 0):>8,}",
            f"  blocked:            {database.get('blocked', 0):>8,}"
            "   (host guard; never fetchable)",
            f"  distinct blobs:     {database.get('distinct_blobs', 0):>8,}",
            f"  on disk:            {_mb(database.get('bytes_on_disk')):>12}",
            "",
        ]

    for label, key in (("Failures", "errors"), ("Type disagreements", "warnings")):
        items = notes.get(key) or []
        if items:
            shown = len(items)
            total = (
                counters.get("error_count", shown) if key == "errors"
                else notes.get("warning_count", shown)
            )
            out.append(f"{label} (showing {shown} of {total:,}):")
            out += [f"  {item}" for item in items]
            out.append("")

    return "\n".join(out)


# --------------------------------------------------------------------------- #
# The benchmark cohort (Phase 6A, Task 7)
# --------------------------------------------------------------------------- #
def summarize_sample(manifest: Any) -> dict[str, Any]:
    """A JSON-ready summary of one drawn benchmark cohort.

    `manifest` is an `app.nrb.manifest.Manifest`; typed loosely for the same
    reason as the other summarizers — this module stays free of model/session
    imports. Everything here is already in the manifest; this only chooses what an
    operator reads first.
    """
    diagnostics = dict(manifest.diagnostics or {})
    by_year: Counter[str] = Counter()
    by_type: Counter[str] = Counter()
    by_resource: Counter[str] = Counter()
    by_owner: Counter[str] = Counter()
    for entry in manifest.entries:
        by_year[str(entry.get("year") or "unknown")] += 1
        by_type[entry.get("document_type") or "untyped"] += 1
        by_resource[entry.get("resource_type") or "unknown"] += 1
        by_owner[entry.get("owner") or "unknown"] += 1

    selected_strata = [s for s in manifest.strata if s.get("selected")]
    return {
        "algorithm_version": manifest.algorithm_version,
        "seed": manifest.seed,
        "drawn_at": manifest.drawn_at,
        "selection_sha256": manifest.selection_sha256,
        "requested": manifest.requested,
        "selected": manifest.selected or len(manifest.entries),
        "shortfall": manifest.shortfall,
        "sampler": dict(manifest.sampler or {}),
        "diagnostics": diagnostics,
        "catalog_counts": dict(manifest.catalog_counts or {}),
        "strata_total": len(manifest.strata),
        "strata_selected": len(selected_strata),
        "strata_weak": len([s for s in selected_strata if s.get("weak")]),
        "by_year": _ordered(by_year),
        "by_cohort": diagnostics.get("allocation_by_cohort", {}),
        "by_document_type": _ordered(by_type, SECTIONS),
        "by_resource_type": _ordered(by_resource),
        "by_owner": _ordered(by_owner),
        "notes": list(manifest.notes),
    }


def render_sample(summary: dict[str, Any]) -> str:
    """The operator's view of a drawn cohort.

    Leads with the fingerprint and the requested-vs-selected pair, because those
    are the two facts that decide whether this cohort is the one every later step
    is talking about. The allocation block exists so a short cohort explains
    itself: a reader should never have to re-run the sampler to find out which
    constraint bound.
    """
    diagnostics = summary.get("diagnostics") or {}
    sampler = summary.get("sampler") or {}
    selected = summary["selected"]
    out: list[str] = [
        "NRB Phase 6A benchmark cohort",
        "=" * 72,
        f"Algorithm:            {summary['algorithm_version']}",
        f"Seed:                 {summary['seed']}",
        f"Drawn at:             {summary['drawn_at'] or '(unset)'}",
        f"Selection sha256:     {summary['selection_sha256'] or '(none)'}",
        "",
        f"Requested:            {summary['requested']:>8,}",
        f"Selected:             {selected:>8,}",
        f"Shortfall:            {summary['shortfall']:>8,}",
        "",
        "Sampler",
        f"  floor:              {sampler.get('floor')}",
        f"  max cohort share:   {sampler.get('max_cohort_share')}",
        f"  explicit caps:      {sampler.get('cohort_caps') or '(none)'}",
        "",
        "Allocation",
        f"  candidates:         {diagnostics.get('candidates', 0):>8,}",
        f"  strata:             {summary['strata_total']:>8,}"
        f"   ({summary['strata_selected']} selected from,"
        f" {summary['strata_weak']} weak n<10)",
        f"  floor slots wanted: {diagnostics.get('floor_requested_slots', 0):>8,}",
        f"  floor slots given:  {diagnostics.get('floor_allocated_slots', 0):>8,}",
        f"  floor short by:     {diagnostics.get('floor_shortfall_slots', 0):>8,}"
        f"   (in {diagnostics.get('floor_short_strata_count', 0)} strata)",
        f"  removed by caps:    {diagnostics.get('slots_removed_by_cap', 0):>8,}",
        f"  redistributed:      {diagnostics.get('slots_redistributed', 0):>8,}"
        f"   (in {diagnostics.get('redistribution_rounds', 0)} rounds)",
        f"  unfillable:         {diagnostics.get('unfillable_slots', 0):>8,}"
        f"   {diagnostics.get('incomplete_reason') or ''}",
        "",
    ]
    caps = diagnostics.get("cohort_caps") or {}
    cohort_counts = summary.get("by_cohort") or {}
    candidates_by_cohort = diagnostics.get("candidates_by_cohort") or {}
    if cohort_counts or caps:
        out.append("Year cohorts (selected / cap / candidates)")
        for cohort in sorted(set(caps) | set(cohort_counts)):
            flag = "  AT CAP" if cohort in (diagnostics.get("capped_cohorts") or ()) else ""
            out.append(
                f"  {cohort:<12} {cohort_counts.get(cohort, 0):>5,} /"
                f" {caps.get(cohort, 0):>5,} /"
                f" {candidates_by_cohort.get(cohort, 0):>7,}{flag}"
            )
        out.append("")

    out += _block("Document types", summary["by_document_type"], selected or None)
    out += _block("File formats", summary["by_resource_type"], selected or None)
    owners = summary["by_owner"]
    out += _block(f"Owners ({len(owners)} codes)",
                  dict(list(owners.items())[:15]), selected or None)

    if summary["notes"]:
        out.append("Notes:")
        out += [f"  {note}" for note in summary["notes"]]
        out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# The extraction profile (Phase 6A, Task 9)
# --------------------------------------------------------------------------- #
# Bands for `legacy_line_ratio`, the per-LINE legacy-font measurement. The
# 0.20 edge is `quality.LEGACY_LINE_RATIO`, the classifier's own threshold — it
# is NOT re-derived or re-tuned here, and a band boundary moving would silently
# change what the published profile means. The bands exist because 0.28 and 1.00
# are both `suspicious` and describe very different documents: one has a readable
# English annex behind a Preeti covering note, the other is unusable throughout,
# and Phase 6B has to size those two cohorts separately.
LEGACY_BANDS: tuple[tuple[str, float, float], ...] = (
    ("0", 0.0, 0.0),
    (">0-<0.20", 0.0, 0.20),
    ("0.20-<0.50", 0.20, 0.50),
    ("0.50-<0.80", 0.50, 0.80),
    (">=0.80", 0.80, 1.01),
)


def _band_for(value: float) -> str:
    """The band a ratio falls in. Edges come from `LEGACY_BANDS` alone, so the
    boundaries cannot drift away from the labels printed next to them."""
    if value <= 0:
        return LEGACY_BANDS[0][0]
    for name, _low, high in LEGACY_BANDS[1:]:
        if value < high:
            return name
    return LEGACY_BANDS[-1][0]


def _distribution(values: Sequence[float | int]) -> dict[str, Any]:
    """A deterministic five-number-ish summary. No sampling, no row order.

    Percentiles by nearest-rank on the sorted values, so the same inputs always
    give the same answer regardless of the order they arrived in — the property
    that lets two runs of the profile be diffed.
    """
    ordered = sorted(float(v) for v in values if v is not None)
    if not ordered:
        return {"n": 0, "min": None, "p25": None, "median": None,
                "p75": None, "p90": None, "max": None, "zero": 0}

    def at(fraction: float) -> float:
        index = min(int(fraction * len(ordered)), len(ordered) - 1)
        return round(ordered[index], 4)

    return {
        "n": len(ordered),
        "min": round(ordered[0], 4),
        "p25": at(0.25),
        "median": at(0.50),
        "p75": at(0.75),
        "p90": at(0.90),
        "max": round(ordered[-1], 4),
        "zero": sum(1 for v in ordered if v == 0),
    }


def _empty_cell() -> dict[str, Any]:
    return {
        "manifest_files": 0,
        "fetched_files": 0,
        "unique_blobs": 0,
        "extracted_blobs": 0,
        "files_not_extracted": 0,
        "by_status": {},
    }


def summarize_extraction(
    result: Any,
    *,
    cohort: Any = None,
    manifest: Any = None,
    manifest_path: str | None = None,
) -> dict[str, Any]:
    """A JSON-ready profile of one extraction pass. Deterministic and pure.

    `result` is an `app.nrb.extract.ExtractResult`, `cohort` an
    `app.nrb.profile.Cohort`, `manifest` an `app.nrb.manifest.Manifest`; all typed
    loosely for the same reason as the other summarizers — this module stays free
    of model and session imports.

    THREE POPULATIONS, NEVER MERGED
      * `source_coverage` counts MANIFEST FILES. Its denominator is the frozen
        cohort, so a file that was never downloaded still appears; it does not
        quietly leave the denominator and flatter every percentage above it.
      * `blob_coverage` counts unique `content_sha256`. Two cohort files with
        identical bytes are one blob, one extraction and one verdict.
      * the verdict and metric sections count BLOBS, because that is the unit a
        verdict is about; the metadata breakdowns count FILES, because that is
        the unit a document type is about. Each section says which it is.

    Source metadata enters ONLY here, from the manifest's own frozen entries —
    not from a fresh catalog read, so a source re-typed by a later sync cannot
    silently re-label a cohort that has already been profiled.
    """
    counters = dict(result.counters)
    accounting = dict(result.cohort or {})
    source = dict(accounting.get("source") or {})
    blob = dict(accounting.get("blob") or {})

    # A cohort's verdicts cover the WHOLE cohort — including blobs a previous
    # pass extracted — which is what a benchmark report needs. Without one, the
    # pass carries the verdicts for the blobs it touched, so a `--section` run
    # still reports statuses instead of a bare "49 persisted".
    verdicts = list(
        (cohort.verdicts if cohort is not None else result.verdicts).values()
    )
    statuses: Counter[str] = Counter(v.status for v in verdicts)
    reasons: Counter[str] = Counter(v.reason for v in verdicts)
    warnings: Counter[str] = Counter(w for v in verdicts for w in v.warnings)

    # --- page structure, over the blobs that have a page concept ---------- #
    paged = [v for v in verdicts if v.page_count is not None]
    pages_total = sum(v.page_count or 0 for v in paged)
    pages_with_text = sum(v.pages_with_text or 0 for v in paged)
    no_native_text = sum(
        1 for v in paged
        if (v.page_count or 0) > 0 and (v.pages_with_text or 0) == 0
    )

    # --- legacy severity --------------------------------------------------- #
    bands: dict[str, int] = {name: 0 for name, _, _ in LEGACY_BANDS}
    for verdict in verdicts:
        if verdict.legacy_line_ratio is not None:
            bands[_band_for(verdict.legacy_line_ratio)] += 1

    summary: dict[str, Any] = {
        "pass": {
            "status": result.status,
            "dry_run": result.dry_run,
            "extractor_version": result.extractor_version,
            "manifest_path": manifest_path,
            "selection_sha256": getattr(manifest, "selection_sha256", None),
            "manifest_entries": len(getattr(manifest, "entries", ()) or ()),
            "duration_seconds": round(result.duration_seconds, 1),
        },
        "scope": dict(result.scope),
        "source_coverage": source,
        "blob_coverage": {
            **blob,
            "selected_this_pass": counters.get("blobs_selected", 0),
            "attempted_this_pass": counters.get("blobs_attempted", 0),
            "persisted_this_pass": counters.get("blobs_persisted", 0),
            "failed_this_pass": counters.get("blobs_failed", 0),
            "missing_on_disk": counters.get("blobs_missing_on_disk", 0),
            "corrupt_on_disk": counters.get("blobs_corrupt_on_disk", 0),
        },
        "by_status": _ordered(statuses),
        "by_reason": _ordered(reasons),
        "warnings": _ordered(warnings),
        "metrics": {
            "char_count": _distribution([v.char_count for v in verdicts]),
            "devanagari_ratio": _distribution(
                [v.devanagari_ratio for v in verdicts
                 if v.devanagari_ratio is not None]
            ),
            "legacy_line_ratio": _distribution(
                [v.legacy_line_ratio for v in verdicts
                 if v.legacy_line_ratio is not None]
            ),
            "text_page_coverage": _distribution(
                [v.text_page_coverage for v in verdicts
                 if v.text_page_coverage is not None]
            ),
            "median_chars_per_text_page": _distribution(
                [v.median_chars_per_text_page for v in verdicts
                 if v.median_chars_per_text_page is not None]
            ),
        },
        "pages": {
            "blobs_with_pages": len(paged),
            "total_pages": pages_total,
            "pages_with_text": pages_with_text,
            "pages_without_text": pages_total - pages_with_text,
            "documents_with_no_native_text": no_native_text,
        },
        "legacy": {
            "legacy_font_suspected": reasons.get("legacy_font_suspected", 0),
            "threshold": LEGACY_BANDS[2][1],
            "bands": bands,
        },
        "catalog": dict(result.counts),
        "notes": dict(result.notes),
        "breakdowns": {},
    }

    if cohort is not None and manifest is not None:
        summary["breakdowns"] = _extraction_breakdowns(cohort, manifest)
    return summary


def _extraction_breakdowns(cohort: Any, manifest: Any) -> dict[str, Any]:
    """Verdict coverage by the cohort's own frozen metadata. Counted in FILES.

    Every cell carries all four denominators, because they answer different
    questions and a percentage over the wrong one is how an unfetched half of a
    cohort disappears:

        manifest_files      what the benchmark asked for
        fetched_files       what was acquired
        unique_blobs        what there was to extract (duplicates collapsed)
        extracted_blobs     what has a verdict at this extractor version

    `by_status` is over FETCHED files, using their blob's verdict, and
    `files_not_extracted` is the remainder — so `sum(by_status) +
    files_not_extracted == fetched_files` always holds and nothing falls out of
    the accounting silently.
    """
    state = {key.comparison_key: key for key in cohort.keys}
    dimensions = {
        "by_cohort": lambda e: (e.get("sampling_stratum") or "/").split("/")[0]
                               or "unknown",
        "by_year": lambda e: str(e.get("year") or "unknown"),
        "by_document_type": lambda e: e.get("document_type") or "untyped",
        "by_resource_type": lambda e: e.get("resource_type") or "unknown",
        "by_owner": lambda e: e.get("owner") or "unknown",
    }
    out: dict[str, dict[str, Any]] = {name: {} for name in dimensions}

    for name, label_of in dimensions.items():
        blobs_per_label: dict[str, set[str]] = {}
        extracted_per_label: dict[str, set[str]] = {}
        for entry in manifest.entries:
            label = label_of(entry)
            cell = out[name].setdefault(label, _empty_cell())
            cell["manifest_files"] += 1

            key = state.get(entry["comparison_key"])
            if key is None or not key.fetched:
                continue
            cell["fetched_files"] += 1
            sha = key.content_sha256
            blobs_per_label.setdefault(label, set()).add(sha)
            verdict = cohort.verdicts.get(sha)
            if verdict is None:
                cell["files_not_extracted"] += 1
            else:
                cell["by_status"][verdict.status] = (
                    cell["by_status"].get(verdict.status, 0) + 1
                )
                extracted_per_label.setdefault(label, set()).add(sha)
        for label, cell in out[name].items():
            cell["unique_blobs"] = len(blobs_per_label.get(label, ()))
            cell["extracted_blobs"] = len(extracted_per_label.get(label, ()))
            cell["by_status"] = dict(sorted(cell["by_status"].items()))

    return {
        name: dict(sorted(cells.items(), key=lambda kv: (-kv[1]["manifest_files"],
                                                         kv[0])))
        for name, cells in out.items()
    }


def render_extraction(summary: dict[str, Any]) -> str:
    """The operator's view of an extraction pass.

    Leads with the cohort identity — a profile of the wrong 400 files is worse
    than no profile — then the two coverage blocks, kept visually apart because
    conflating them is the specific mistake this report exists to prevent.
    """
    ident = summary["pass"]
    source = summary["source_coverage"]
    blob = summary["blob_coverage"]
    dry = "  (DRY RUN — nothing was parsed or written)" if ident["dry_run"] else ""

    out: list[str] = [
        f"NRB native extraction — {ident['status']}{dry}",
        "=" * 72,
        f"Extractor version:    {ident['extractor_version']}",
        f"Manifest:             {ident['manifest_path'] or '(no manifest scope)'}",
        f"Cohort fingerprint:   {ident['selection_sha256'] or '(none)'}",
        f"Duration:             {ident['duration_seconds']:,.1f}s",
        "",
        "Source coverage (MANIFEST FILES — the frozen benchmark)",
        f"  requested:          {source.get('requested', 0):>8,}",
        f"  in the catalog:     {source.get('in_catalog', 0):>8,}",
        f"  missing:            {source.get('missing_from_catalog', 0):>8,}"
        "   (a stale manifest; the only real defect here)",
        f"  fetched:            {source.get('fetched', 0):>8,}",
        f"  not fetched yet:    {source.get('unfetched', 0):>8,}"
        "   (reported, never substituted)",
    ]
    for status, count in (source.get("by_fetch_status") or {}).items():
        out.append(f"    {status:<16} {count:>8,}")

    out += [
        "",
        "Blob coverage (UNIQUE content_sha256 — the extraction unit)",
        f"  unique fetched:     {blob.get('unique_fetched', 0):>8,}",
        f"  duplicates:         {blob.get('duplicates_collapsed', 0):>8,}"
        "   (same bytes, two cohort files — one extraction)",
        f"  already extracted:  {blob.get('already_extracted', 0):>8,}"
        "   (at this exact extractor version)",
        f"  pending:            {blob.get('pending_extraction', 0):>8,}",
        f"  selected this pass: {blob.get('selected_this_pass', 0):>8,}",
        f"  attempted:          {blob.get('attempted_this_pass', 0):>8,}",
        f"  persisted:          {blob.get('persisted_this_pass', 0):>8,}",
        f"  failed:             {blob.get('failed_this_pass', 0):>8,}",
        f"  missing on disk:    {blob.get('missing_on_disk', 0):>8,}",
        f"  corrupt on disk:    {blob.get('corrupt_on_disk', 0):>8,}",
        "",
    ]

    total_blobs = sum(summary["by_status"].values())
    out += _block("Verdicts (per BLOB)", summary["by_status"], total_blobs or None)
    out += _block("Reasons (per BLOB)", summary["by_reason"], total_blobs or None)
    if summary["warnings"]:
        out += _block("Warnings", summary["warnings"])

    out.append("Metric distributions (per BLOB)")
    for name, dist in summary["metrics"].items():
        if not dist["n"]:
            continue
        out.append(
            f"  {name:<28} n={dist['n']:<6} min={dist['min']:<9} "
            f"median={dist['median']:<9} p90={dist['p90']:<9} max={dist['max']}"
        )
    out.append("")

    pages = summary["pages"]
    out += [
        "Pages",
        f"  documents with pages: {pages['blobs_with_pages']:>8,}",
        f"  total pages:          {pages['total_pages']:>8,}",
        f"  pages with text:      {pages['pages_with_text']:>8,}",
        f"  pages without text:   {pages['pages_without_text']:>8,}",
        f"  no native text at all:{pages['documents_with_no_native_text']:>8,}",
        "",
        "Legacy-font severity (legacy_line_ratio, threshold "
        f"{summary['legacy']['threshold']})",
        f"  legacy_font_suspected:{summary['legacy']['legacy_font_suspected']:>8,}",
    ]
    for band, count in summary["legacy"]["bands"].items():
        out.append(f"    {band:<14} {count:>8,}")
    out.append("")

    for name, title in (
        ("by_cohort", "Year cohort"),
        ("by_resource_type", "File format"),
        ("by_document_type", "Document type"),
    ):
        cells = (summary.get("breakdowns") or {}).get(name) or {}
        if not cells:
            continue
        out.append(f"{title} (manifest / fetched / blobs / extracted)")
        for label, cell in cells.items():
            verdicts = " ".join(
                f"{status}={count}" for status, count in cell["by_status"].items()
            )
            out.append(
                f"  {label:<22} {cell['manifest_files']:>5,} /"
                f" {cell['fetched_files']:>5,} / {cell['unique_blobs']:>5,} /"
                f" {cell['extracted_blobs']:>5,}   {verdicts}"
            )
        out.append("")

    failures = (summary.get("notes") or {}).get("failures") or []
    if failures:
        total = summary["notes"].get("failure_count", len(failures))
        out.append(f"Failures (showing {len(failures)} of {total:,}):")
        out += [f"  {item}" for item in failures]
        out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# The Docling calibration (Phase 6A, Task 10)
# --------------------------------------------------------------------------- #
# How much of a stored preview the report prints. The database contract is 300
# characters (`extraction.PREVIEW_CHARS`) and this is never wider — a report is a
# place to eyeball a disagreement, not a second store of extracted text.
PREVIEW_CHARS = 160

# Only disagreements are printed with previews, and only this many. Every
# disagreement is still COUNTED; the cap is on the transcript, not the finding.
DISAGREEMENT_SAMPLE = 25


def _speed(durations_ms: Sequence[int]) -> dict[str, Any]:
    """Total and per-document timings for one engine.

    p95 as well as the median because the two engines fail slowly in different
    ways: pypdf's worst case is a big file, Docling's is a page whose layout model
    finds a great deal to do, and a median hides both.
    """
    ordered = sorted(int(v or 0) for v in durations_ms)
    if not ordered:
        return {"n": 0, "total_seconds": 0.0, "median_ms": None, "p95_ms": None}

    def at(fraction: float) -> int:
        return ordered[min(int(fraction * len(ordered)), len(ordered) - 1)]

    return {
        "n": len(ordered),
        "total_seconds": round(sum(ordered) / 1000, 3),
        "median_ms": at(0.50),
        "p95_ms": at(0.95),
    }


def _engine_block(sides: Sequence[Any]) -> dict[str, Any]:
    """One engine's own numbers, computed identically for both engines."""
    return {
        "by_status": _ordered(Counter(s.status for s in sides)),
        "by_reason": _ordered(Counter(s.reason for s in sides)),
        "warnings": _ordered(Counter(w for s in sides for w in s.warnings)),
        "total_chars": sum(s.char_count or 0 for s in sides),
        "char_count": _distribution([s.char_count for s in sides]),
        "devanagari_ratio": _distribution(
            [s.devanagari_ratio for s in sides if s.devanagari_ratio is not None]
        ),
        "legacy_line_ratio": _distribution(
            [s.legacy_line_ratio for s in sides if s.legacy_line_ratio is not None]
        ),
        "text_page_coverage": _distribution(
            [s.text_page_coverage for s in sides if s.text_page_coverage is not None]
        ),
        "legacy_bands": _legacy_bands(sides),
        "speed": _speed([s.duration_ms for s in sides]),
    }


def _legacy_bands(sides: Sequence[Any]) -> dict[str, int]:
    bands = {name: 0 for name, _, _ in LEGACY_BANDS}
    for side in sides:
        if side.legacy_line_ratio is not None:
            bands[_band_for(side.legacy_line_ratio)] += 1
    return bands


def summarize_calibration(
    result: Any, *, subset: Any = None, subset_path: str | None = None
) -> dict[str, Any]:
    """A JSON-ready profile of one pypdf-vs-Docling pass. Deterministic and pure.

    `result` is an `app.nrb.calibrate.CalibrationResult`, `subset` an
    `app.nrb.calibration.CalibrationSubset`; both typed loosely so this module
    stays free of model and session imports.

    DENOMINATORS, STATED
      * `source` counts SUBSET FILES — the frozen 40. A file that was never
        downloaded stays in that denominator instead of quietly leaving it and
        flattering every rate above it.
      * `blobs` counts unique `content_sha256`, which is what was actually
        parsed. Two subset files with identical bytes are one comparison.
      * every rate in `agreement` and `rescues` is over `comparisons_run` — the
        blobs both engines actually read — and that number is printed next to
        them so a 100% agreement over three files cannot be mistaken for one over
        forty.

    Nothing here depends on the order the comparisons arrive in: the counters are
    ordered, the distributions sort their inputs, and the disagreement sample is
    sorted by content hash.
    """
    comparisons = sorted(result.comparisons, key=lambda c: c.content_sha256)
    counters = dict(result.counters)
    accounting = dict(result.cohort or {})
    source = dict(accounting.get("source") or {})
    blob = dict(accounting.get("blob") or {})

    native = [c.native for c in comparisons]
    docling = [c.docling for c in comparisons]
    compared = len(comparisons)
    categories = Counter(c.category for c in comparisons)

    def rate(count: int) -> float:
        return round(count / compared, 4) if compared else 0.0

    status_agreed = sum(1 for c in comparisons if c.status_agreement)
    reason_agreed = sum(1 for c in comparisons if c.reason_agreement)

    # Pairwise: only where the comparison means something. A blob pypdf read
    # nothing from has no char ratio — 4100/0 is not "infinitely better", it is a
    # rescue, and it is counted as one immediately below.
    ratios = [
        c.docling.char_count / c.native.char_count
        for c in comparisons
        if c.native.char_count
    ]
    native_zero = sum(1 for c in comparisons if not c.native.char_count)
    docling_zero = sum(1 for c in comparisons if not c.docling.char_count)

    def delta(name: str) -> list[float]:
        return [
            getattr(c.docling, name) - getattr(c.native, name)
            for c in comparisons
            if getattr(c.docling, name) is not None
            and getattr(c.native, name) is not None
        ]

    pypdf_speed = _speed([s.duration_ms for s in native])
    docling_speed = _speed([s.duration_ms for s in docling])
    pypdf_seconds = pypdf_speed["total_seconds"]
    docling_seconds = docling_speed["total_seconds"]

    return {
        "calibration": {
            "status": result.status,
            "dry_run": result.dry_run,
            "purpose": getattr(subset, "purpose", None) or "docling-calibration",
            "subset_path": subset_path or result.subset_path,
            "subset_selection_sha256": result.subset_selection_sha256,
            "parent_selection_sha256": result.parent_selection_sha256,
            "subset_algorithm_version": getattr(
                subset, "subset_algorithm_version", None
            ),
            "requested_size": getattr(subset, "requested_size", None),
            "duration_seconds": round(result.duration_seconds, 1),
            "engine": (result.notes or {}).get("engine"),
        },
        # SUBSET FILES.
        "source": {
            "subset_entries": counters.get("subset_entries", 0),
            "in_catalog": source.get("in_catalog", counters.get(
                "subset_files_in_catalog", 0)),
            "missing_from_catalog": source.get("missing_from_catalog", 0),
            "fetched": source.get("fetched", counters.get("subset_files_fetched", 0)),
            "unfetched": source.get("unfetched", 0),
            "by_fetch_status": source.get("by_fetch_status") or {},
        },
        # UNIQUE BLOBS.
        "blobs": {
            "unique_fetched": blob.get("unique_fetched", 0),
            "duplicates_collapsed": blob.get("duplicates_collapsed", 0),
            "selected": counters.get("blobs_selected", 0),
            "compared": compared,
            "subset_files_represented": len(
                {key for c in comparisons for key in c.comparison_keys}
            ),
            "missing_on_disk": counters.get("blobs_missing_on_disk", 0),
            "corrupt_on_disk": counters.get("blobs_corrupt_on_disk", 0),
        },
        "agreement": {
            "compared": compared,
            "status_agreed": status_agreed,
            "status_agreement_rate": rate(status_agreed),
            "reason_agreed": reason_agreed,
            "reason_agreement_rate": rate(reason_agreed),
            "by_category": _ordered(categories, CATEGORY_ORDER),
        },
        "pypdf": _engine_block(native),
        "docling": _engine_block(docling),
        "pairwise": {
            "char_ratio": _distribution(ratios),
            "pypdf_zero_chars": native_zero,
            "docling_zero_chars": docling_zero,
            "devanagari_ratio_delta": _distribution(delta("devanagari_ratio")),
            "legacy_line_ratio_delta": _distribution(delta("legacy_line_ratio")),
            "status_transitions": _transitions(comparisons, "status"),
            "reason_transitions": _transitions(comparisons, "reason"),
        },
        "rescues": {
            # "A rescued B" = B's verdict is not usable and A's is. See
            # `calibrate.categorize`.
            "docling_rescued_pypdf": categories.get("docling_rescued_pypdf", 0),
            "pypdf_rescued_docling": categories.get("pypdf_rescued_docling", 0),
            "both_extracted": categories.get("both_extracted", 0),
            "both_suspicious": categories.get("both_suspicious", 0),
            "both_failed": categories.get("both_failed", 0),
            "disagreed_neither_usable": categories.get(
                "disagreed_neither_usable", 0),
            # The two explicit pairs, named because they are the ones a reader
            # looks for first. Both are rescues by the definition above.
            "docling_extracted_pypdf_suspicious": _pair_count(
                comparisons, "suspicious", "extracted"),
            "pypdf_extracted_docling_suspicious": _pair_count(
                comparisons, "extracted", "suspicious"),
            "docling_rescue_rate": rate(categories.get("docling_rescued_pypdf", 0)),
        },
        "speed": {
            "pypdf_seconds": pypdf_seconds,
            "docling_seconds": docling_seconds,
            "docling_init_seconds": round(result.docling_init_seconds, 3),
            "pypdf": pypdf_speed,
            "docling": docling_speed,
            # Init excluded: it is paid once for the whole pass, and folding it in
            # would make the per-document ratio depend on the sample size.
            "slowdown": (
                round(docling_seconds / pypdf_seconds, 1) if pypdf_seconds else None
            ),
        },
        "disagreements": [
            c.as_dict(preview_chars=PREVIEW_CHARS)
            for c in comparisons
            if not c.status_agreement
        ][:DISAGREEMENT_SAMPLE],
        "disagreement_count": compared - status_agreed,
        "notes": dict(result.notes or {}),
    }


# The order categories are printed in: agreement first, then the two asymmetric
# cases the calibration exists to find.
CATEGORY_ORDER = (
    "both_extracted", "both_suspicious", "both_failed", "agreed_other",
    "docling_rescued_pypdf", "pypdf_rescued_docling", "disagreed_neither_usable",
)


def _pair_count(comparisons: Sequence[Any], native: str, docling: str) -> int:
    return sum(
        1 for c in comparisons
        if c.native.status == native and c.docling.status == docling
    )


def _transitions(comparisons: Sequence[Any], field: str) -> dict[str, int]:
    """pypdf's answer -> Docling's answer, counted. Ordered, so two runs diff."""
    counts = Counter(
        f"{getattr(c.native, field)}->{getattr(c.docling, field)}"
        for c in comparisons
    )
    return dict(sorted(counts.items()))


def render_calibration(summary: dict[str, Any]) -> str:
    """The operator's view of a calibration pass.

    Leads with both fingerprints — a calibration of the wrong 40 files is worse
    than none — and puts the two rescue counts where they cannot be missed. A
    single agreement percentage would hide both inside it.
    """
    ident = summary["calibration"]
    source = summary["source"]
    blobs = summary["blobs"]
    agree = summary["agreement"]
    dry = "  (DRY RUN — neither parser ran)" if ident["dry_run"] else ""

    out: list[str] = [
        f"NRB pypdf-vs-Docling calibration — {ident['status']}{dry}",
        "=" * 72,
        f"Subset:               {ident['subset_path'] or '(none)'}",
        f"Subset fingerprint:   {ident['subset_selection_sha256'] or '(none)'}",
        f"Parent benchmark:     {ident['parent_selection_sha256'] or '(none)'}",
        f"Docling pipeline:     {ident['engine'] or '(not opened)'}",
        f"Duration:             {ident['duration_seconds']:,.1f}s",
        "",
        "Subset coverage (FILES — the frozen calibration slice)",
        f"  subset entries:     {source['subset_entries']:>8,}",
        f"  in the catalog:     {source['in_catalog']:>8,}",
        f"  missing:            {source['missing_from_catalog']:>8,}",
        f"  fetched:            {source['fetched']:>8,}",
        f"  not fetched yet:    {source['unfetched']:>8,}"
        "   (reported, never substituted)",
        "",
        "Blob coverage (UNIQUE content_sha256 — the comparison unit)",
        f"  unique fetched:     {blobs['unique_fetched']:>8,}",
        f"  duplicates:         {blobs['duplicates_collapsed']:>8,}"
        "   (same bytes, two subset files — one comparison)",
        f"  selected:           {blobs['selected']:>8,}",
        f"  compared:           {blobs['compared']:>8,}",
        f"  files represented:  {blobs['subset_files_represented']:>8,}",
        f"  missing on disk:    {blobs['missing_on_disk']:>8,}",
        f"  corrupt on disk:    {blobs['corrupt_on_disk']:>8,}",
        "",
        f"Agreement (over {agree['compared']:,} compared blobs)",
    ]
    if not agree["compared"]:
        out.append("  (none)")
    else:
        out += [
            f"  status agreement:   {agree['status_agreed']:>8,}"
            f"   {agree['status_agreement_rate']:.1%}",
            f"  reason agreement:   {agree['reason_agreed']:>8,}"
            f"   {agree['reason_agreement_rate']:.1%}",
        ]
    out.append("")

    rescues = summary["rescues"]
    out += [
        "Rescues  (A rescued B = B's verdict is not usable and A's is)",
        f"  DOCLING RESCUED PYPDF {rescues['docling_rescued_pypdf']:>6,}"
        "   <- the number that would invalidate the screen",
        f"  pypdf rescued docling {rescues['pypdf_rescued_docling']:>6,}",
        f"  both extracted        {rescues['both_extracted']:>6,}",
        f"  both suspicious       {rescues['both_suspicious']:>6,}",
        f"  both failed           {rescues['both_failed']:>6,}",
        f"  disagreed, neither    {rescues['disagreed_neither_usable']:>6,}",
        "",
    ]

    for engine in ("pypdf", "docling"):
        block = summary[engine]
        out.append(f"{engine} (per BLOB)")
        if not block["char_count"]["n"]:
            out.append("  (none)")
        else:
            out.append("  " + " ".join(
                f"{status}={count}" for status, count in block["by_status"].items()
            ))
            out.append("  " + " ".join(
                f"{reason}={count}" for reason, count in block["by_reason"].items()
            ))
            out.append(f"  total chars {block['total_chars']:,}   "
                       f"time {block['speed']['total_seconds']}s   "
                       f"median {block['speed']['median_ms']}ms   "
                       f"p95 {block['speed']['p95_ms']}ms")
            for name in ("char_count", "devanagari_ratio", "legacy_line_ratio",
                         "text_page_coverage"):
                dist = block[name]
                if dist["n"]:
                    out.append(
                        f"    {name:<22} n={dist['n']:<5} min={dist['min']:<9} "
                        f"median={dist['median']:<9} max={dist['max']}"
                    )
        out.append("")

    speed = summary["speed"]
    out += [
        "Speed",
        f"  pypdf total:        {speed['pypdf_seconds']:>10,.1f}s",
        f"  docling init:       {speed['docling_init_seconds']:>10,.1f}s"
        "   (paid once, excluded from the ratio)",
        f"  docling total:      {speed['docling_seconds']:>10,.1f}s",
        f"  slowdown:           {speed['slowdown'] if speed['slowdown'] else '-':>10}x",
        "",
    ]

    if summary["disagreements"]:
        out.append(f"Disagreements ({summary['disagreement_count']:,}, showing "
                   f"{len(summary['disagreements'])}) — read every one:")
        for row in summary["disagreements"]:
            out.append(f"\n  {row['content_sha256'][:12]}  {row['category']}")
            for engine in ("pypdf", "docling"):
                side = row[engine]
                out.append(
                    f"    {engine:<8} {side['status']}/{side['reason']}  "
                    f"chars={side['char_count']} "
                    f"devanagari={side['devanagari_ratio']} "
                    f"legacy={side['legacy_line_ratio']}"
                )
                out.append(f"             {side['preview']!r}")
        out.append("")
    else:
        out += ["Disagreements", "  (none)", ""]

    failures = (summary.get("notes") or {}).get("failures") or []
    if failures:
        out.append(f"Blobs not compared ({len(failures)}):")
        out += [f"  {item}" for item in failures]
        out.append("")
    return "\n".join(out)
