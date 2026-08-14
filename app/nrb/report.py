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
from typing import Any

from .attachments import RESOURCE_TYPES as ATTACHMENT_TYPES
from .classify import RESOURCE_TYPES, SECTIONS
from .http import FETCH_KINDS

__all__ = [
    "summarize", "render", "SAMPLE_SIZE",
    "summarize_documents", "render_documents",
    "summarize_sync", "render_sync",
    "summarize_fetch", "render_fetch",
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
