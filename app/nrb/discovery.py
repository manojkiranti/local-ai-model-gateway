"""One complete read of NRB's published corpus — the input to a catalog sync.

Phase 3 proved the corpus can be enumerated in ~190 REST requests instead of
18,567 page fetches, so Phase 4 does a **full metadata reconciliation on every
sync** rather than a clever incremental fetch. Full discovery makes deletion
detection trivial (anything this run did not see is gone) and removes a whole
class of "the incremental cursor was wrong" bugs. The expensive part —
downloading 18k files — is Phase 5's problem, not this module's.

Two sources, and both are needed:

  * **The WordPress REST API** (`wp_api`) is the data path: titles, dates,
    categories and the `acf.document_file` attachment objects. 18,370 documents.
  * **The sitemap** (`sitemap`) is the completeness check. `economic-review` and
    `er-article` — 196 URLs — are published in the sitemap and are not
    REST-registered post types, so REST alone silently under-reports the corpus.
    The sitemap also supplies `lastmod`, which is persisted for later chronology
    work.

`complete` is the field the whole safety story hangs on. Absence-based
deactivation is only permitted when it is True, which requires that every REST
collection AND the entire sitemap were read with no fetch error and no bound
truncating the walk. A half-read corpus that deactivated the sources it failed to
reach would destroy known-good state on a transient network fault — so a bounded
run (`limit`) is complete=False by construction, not by remembering to say so.

Note what does NOT make a run incomplete: a post type REST refuses to serve. That
is a known, measured gap, and the sitemap covers it — those URLs become
`sitemap_only` sources. The gap is reported (`post_types_not_served`), not
treated as a failure.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from . import sitemap as sitemap_module
from . import wp_api
from .classify import DEPARTMENT_CODES, DiscoveredURL
from .documents import NRBDocument, Taxonomy, build_document
from .http import open_client
from .records import page_key

logger = logging.getLogger("app.nrb.discovery")

__all__ = [
    "CONTENT_POST_TYPES",
    "DOCUMENT_POST_TYPES",
    "Discovery",
    "DiscoveryError",
    "discover_corpus",
]

# Post types that are content classes rather than owners. `forex` is deliberately
# absent: its 10,485 rate posts are already served by the `get_nrb_forex` tool and
# have nothing to do with the document corpus. Defined here rather than in the
# Phase 3 script so the inventory and the sync cannot disagree about what the
# corpus IS — a scope difference between them would read as a sync bug.
CONTENT_POST_TYPES = ("economic-review", "er-article", "tuesday-fa", "gallery-post-type")

# Deterministic order: two runs enumerate the same types in the same sequence.
DOCUMENT_POST_TYPES = tuple(sorted(DEPARTMENT_CODES)) + CONTENT_POST_TYPES

# The sitemap page kinds that are documents. Everything else it publishes —
# category archives, department pages, dated news permalinks, standalone pages —
# is a page ABOUT documents, not a document, and persisting those as "sources"
# would put ~1,100 rows in the catalog that no phase will ever download.
DOCUMENT_PAGE_KINDS = frozenset({"document_post"})


class DiscoveryError(Exception):
    """Discovery could not start at all (no taxonomy, no sitemap root).

    Distinct from the errors collected inside a `Discovery`: those describe a run
    that produced a usable-but-incomplete result. This one means there is nothing
    to reconcile against, and reconciling against nothing would look exactly like
    "NRB deleted everything".
    """


@dataclass
class Discovery:
    """Everything one discovery pass found, plus how trustworthy it is."""

    documents: list[NRBDocument] = field(default_factory=list)
    # url_key -> the sitemap's own entry, for document posts only. Keyed on
    # `page_key` because the sitemap percent-encodes Devanagari slugs while REST
    # returns them literally; raw-string matching would report every REST
    # document as missing from the sitemap.
    sitemap_documents: dict[str, DiscoveredURL] = field(default_factory=dict)
    sitemaps_seen: int = 0
    sitemap_urls_seen: int = 0
    # Counts of sitemap URLs deliberately not persisted, by page kind. Reported
    # so "1,100 URLs went nowhere" is visible rather than silently dropped.
    skipped_by_page_kind: dict[str, int] = field(default_factory=dict)
    post_types_not_served: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)
    # The REST half's problems, also present in `errors`/`truncated` above. Held
    # separately because the two halves gate different things — see
    # `rest_complete`.
    rest_errors: list[str] = field(default_factory=list)
    rest_truncated: list[str] = field(default_factory=list)

    @property
    def rest_complete(self) -> bool:
        """Whether REST returned the whole document corpus.

        This gates the *creation of sitemap-only sources*, and it must be a
        separate question from `complete`. A bounded run (`--limit 300`) reads 300
        REST documents against 18,567 sitemap document URLs, so "in the sitemap but
        not in REST" would name 18,267 URLs — and the catalog would fill with
        thousands of contentless stubs claiming REST cannot see them. When REST is
        not trustworthy the gap is simply not measured this run; the next clean run
        records it.
        """
        return not self.rest_errors and not self.rest_truncated

    @property
    def complete(self) -> bool:
        """Whether this run may be treated as the whole corpus.

        The precondition for absence-based deactivation, and the reason a bounded
        or partially-failed run cannot mass-deactivate anything. Stricter than
        `rest_complete`: the sitemap must have been read too, because it is the
        only witness for the post types REST does not serve.
        """
        return not self.errors and not self.truncated and bool(self.sitemaps_seen)


async def discover_corpus(
    *,
    limit: int | None = None,
    post_types: tuple[str, ...] | None = None,
    include_sitemap: bool = True,
) -> Discovery:
    """Read the whole corpus (or a bounded slice of it) from NRB.

    `limit` exists for a smoke test against the live site; it caps the documents
    collected and marks the run truncated, so a bounded run can never deactivate
    anything. Requests are sequential and paced by `NRB_CRAWL_DELAY_SECONDS` —
    the same politeness the Phase 3 inventory uses against a central bank's site.
    """
    discovery = Discovery()
    types = post_types or DOCUMENT_POST_TYPES

    client = open_client(
        wp_api.USER_AGENT,
        accept="application/json",
        connect_timeout=wp_api.CONNECT_TIMEOUT,
        read_timeout=wp_api.READ_TIMEOUT,
    )
    try:
        category_result = await wp_api.fetch_categories(client)
        if not category_result.items:
            raise DiscoveryError(
                "could not read the NRB category taxonomy: "
                f"{category_result.errors[0] if category_result.errors else 'empty'}"
            )
        # A partial taxonomy would silently mistype documents rather than fail, so
        # it counts against completeness even though discovery can continue.
        discovery.rest_errors.extend(str(error) for error in category_result.errors)
        discovery.rest_truncated.extend(category_result.truncated)
        taxonomy = Taxonomy(category_result.items)
        logger.info("NRB sync: taxonomy has %d categories", len(taxonomy))

        type_result = await wp_api.fetch_post_types(client)
        served = {info.rest_base for info in type_result.items}
        if served:
            discovery.post_types_not_served = [t for t in types if t not in served]
            types = tuple(t for t in types if t in served)
        else:
            # Without /types we cannot tell "not served" from "broken", and every
            # unserved type would be counted as a fetch error below. Recorded as
            # an error so the run cannot be trusted as complete.
            discovery.rest_errors.extend(str(error) for error in type_result.errors)

        remaining = limit
        posts: list[dict] = []
        for post_type in types:
            if remaining is not None and remaining <= 0:
                discovery.rest_truncated.append(f"limit={limit}")
                break
            result = await wp_api.fetch_posts(
                post_type,
                client=client,
                max_items=remaining,
            )
            discovery.rest_errors.extend(str(error) for error in result.errors)
            discovery.rest_truncated.extend(result.truncated)
            fetched = [item for item in result.items if isinstance(item, dict)]
            posts.extend(fetched)
            if remaining is not None:
                remaining -= len(fetched)
            logger.info(
                "NRB sync: %s -> %d posts (reported total %s)",
                post_type, len(fetched), result.total_reported,
            )
    finally:
        await client.aclose()

    # The REST issues are also overall issues: `errors`/`truncated` are the merged
    # view the report prints and `complete` is computed from.
    discovery.errors.extend(discovery.rest_errors)
    discovery.truncated.extend(discovery.rest_truncated)

    # Deterministic order so two runs batch identically and a diff of two runs is
    # a real change rather than dictionary ordering.
    discovery.documents = sorted(
        (build_document(post, taxonomy=taxonomy) for post in posts),
        key=lambda document: (document.url, document.post_id or 0),
    )
    logger.info("NRB sync: normalized %d documents", len(discovery.documents))

    if include_sitemap:
        await _add_sitemap(discovery)

    return discovery


async def _add_sitemap(discovery: Discovery) -> None:
    """Walk the sitemap and keep the document-post URLs.

    Failures are recorded rather than raised: a sitemap that could not be read
    means the corpus gap cannot be measured this run, which makes the run
    incomplete — and an incomplete run may not deactivate. That is the safe
    degradation, and it is why this does not simply skip the sitemap on error.
    """
    try:
        inventory = await sitemap_module.discover()
    except sitemap_module.SitemapError as exc:
        discovery.errors.append(f"sitemap discovery failed: {exc}")
        return

    discovery.sitemaps_seen = len(inventory.sitemaps_fetched)
    discovery.sitemap_urls_seen = len(inventory.urls)
    discovery.errors.extend(f"sitemap {url}: {why}" for url, why in inventory.errors)
    discovery.truncated.extend(inventory.truncated)
    # A rejected URL is NRB pointing off-host; it is a finding, not a failure of
    # ours, so it warns rather than blocking completeness.
    discovery.warnings.extend(
        f"sitemap rejected {url}: {why}" for url, why in inventory.rejected
    )

    skipped: dict[str, int] = {}
    for entry in inventory.urls:
        if entry.page_kind not in DOCUMENT_PAGE_KINDS:
            skipped[entry.page_kind] = skipped.get(entry.page_kind, 0) + 1
            continue
        discovery.sitemap_documents[page_key(entry.normalized_url)] = entry
    discovery.skipped_by_page_kind = dict(sorted(skipped.items()))
    logger.info(
        "NRB sync: sitemap has %d document URLs across %d sitemaps (%d other URLs)",
        len(discovery.sitemap_documents), discovery.sitemaps_seen,
        sum(skipped.values()),
    )
