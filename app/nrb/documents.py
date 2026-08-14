"""The normalized NRB document record. Pure — no HTTP, no DB, no schema decisions.

This is the Phase 3 output: what a single NRB document post is, assembled from
what NRB's own metadata says and nothing else. It is deliberately *not* a database
model — Phase 4 designs that, and it should design it against this shape rather
than the other way round.

Where each field's evidence comes from, and why it is trustworthy:

  * `title` — REST `title.rendered`, the post's authoritative title (Devanagari
    preserved; HTML entities decoded; no transliteration, ever).
  * `published` / `modified` — REST `date` / `modified`, NRB's own timestamps.
    **The rendered HTML page exposes no date at all**, so this is the only source.
  * `owner` — the post type, cross-checked against the URL's owner segment from
    Phase 2's classifier. Codes stay codes; `owner_label` holds the human name
    only when NRB itself supplied one.
  * `sections` — resolved from the post's category ids through the taxonomy's
    parent chain into Phase 2's `CATEGORY_SECTIONS`. This is the answer to the
    question Phase 2 could not answer, and it comes from NRB's taxonomy rather
    than from reading a slug.
  * `attachments` — see `attachments.py`.
  * `extras` — the deterministic ACF scalars NRB happens to publish
    (`circular_number`, `fiscal_year`, tender dates…). Kept verbatim, unparsed,
    because they are evidence for Phase 4, not conclusions for Phase 3.

`sections` is a **tuple, not a scalar**, because a post genuinely belongs to
several: of 2,786 sampled posts, 357 resolved to 2 sections, 75 to 3, 28 to 4 and
2 to 5 (a circular that is also a notice really is both). `primary_section` picks
one deterministically using `classify.SECTIONS` order — regulatory classes first,
which is exactly why that tuple was ordered that way in Phase 2. Collapsing to the
primary and discarding the rest would lose real classification evidence.

Coverage, measured live: **90.5%** of sampled posts resolve at least one section.
Of the 9.5% that do not, 5.6% carry no categories at all and 3.9% are only in
categories that genuinely mean "miscellaneous". Unknown stays unknown.
"""

from __future__ import annotations

import html as html_module
from dataclasses import dataclass, field
from typing import Any

from .attachments import Attachment, extract_attachments
from .classify import CATEGORY_SECTIONS, DEPARTMENT_CODES, SECTIONS, classify_url
from .http import normalize_url

__all__ = [
    "NRBDocument",
    "Taxonomy",
    "build_document",
    "clean_title",
]

# ACF keys that are page mechanics rather than document metadata. Excluded from
# `extras` so the interesting fields are not buried; listed rather than pattern-
# matched so the exclusion is reviewable.
_MECHANICAL_ACF = frozenset(
    {
        "document_file", "secondary_file", "photos", "banner_details",
        "no_homepage", "document_new_tag_duration",
    }
)


class Taxonomy:
    """Category id -> (slug, name, parent), with section resolution.

    Built once per run from `/api/wp/v2/categories` (3 requests for NRB's 284
    categories) rather than looked up per post.
    """

    def __init__(self, categories: list[Any]) -> None:
        self._by_id = {category.id: category for category in categories}

    def __len__(self) -> int:
        return len(self._by_id)

    def slug(self, category_id: int) -> str | None:
        category = self._by_id.get(category_id)
        return category.slug if category else None

    def name(self, category_id: int) -> str | None:
        category = self._by_id.get(category_id)
        return category.name if category else None

    def chain(self, category_id: int) -> list[str]:
        """Slugs from `category_id` up to its root, nearest first.

        Cycle-guarded: a corrupted parent pointer must not hang an inventory run.
        """
        out: list[str] = []
        seen: set[int] = set()
        current = category_id
        while current in self._by_id and current not in seen:
            seen.add(current)
            out.append(self._by_id[current].slug)
            current = self._by_id[current].parent
        return out

    def section_for(self, category_id: int) -> tuple[str | None, str]:
        """`(section, evidence)` for one category id.

        Walks the parent chain because NRB files posts under *children*
        (`domestic-tenders` under `tenders`, `2082-83` under `circulars`,
        `balance-sheet-fy-2082-83` under `balance-sheet`), while Phase 2's map is
        keyed on the parents that appear as URL path roots. Without the chain,
        coverage measured 88.3%; with it, 90.5%.

        A `parent/child` pair is tried before the ancestors, so a mixed parent
        archive still answers per child (`public-debt-operations-archive`).
        """
        chain = self.chain(category_id)
        if not chain:
            return None, f"unknown category id {category_id}"
        for child, parent in zip(chain, chain[1:]):
            key = f"{parent}/{child}"
            if key in CATEGORY_SECTIONS:
                return CATEGORY_SECTIONS[key], f"category path {key!r}"
        for slug in chain:
            if slug in CATEGORY_SECTIONS:
                evidence = (
                    f"category {slug!r}" if slug == chain[0]
                    else f"category {chain[0]!r} via ancestor {slug!r}"
                )
                return CATEGORY_SECTIONS[slug], evidence
        return None, f"unmapped category {chain[0]!r}"


@dataclass
class NRBDocument:
    """One discovered NRB document post, normalized. Nothing here is inferred."""

    # Identity
    post_id: int | None
    post_type: str | None                 # NRB's registered post type
    url: str                              # REST `link`, normalized
    canonical_url: str | None             # REST `link` verbatim (WP's canonical)
    slug: str | None

    # Content metadata
    title: str | None
    published: str | None                 # REST `date` (site time)
    published_gmt: str | None
    modified: str | None
    status: str | None

    # Ownership + classification
    owner: str | None                     # department/office code, or None
    owner_label: str | None               # only if NRB supplied a human name
    page_kind: str | None                 # from Phase 2's URL classifier
    sections: tuple[str, ...] = ()        # ALL resolved sections, ordered
    section_evidence: tuple[str, ...] = ()
    category_ids: tuple[int, ...] = ()
    category_slugs: tuple[str, ...] = ()  # raw labels, kept as evidence
    category_names: tuple[str, ...] = ()

    # Payload
    attachments: list[Attachment] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)
    redirects_to_file: bool | None = None  # ACF `document_redirect_to_file`
    warnings: list[str] = field(default_factory=list)

    @property
    def primary_section(self) -> str:
        """One section, chosen deterministically by `classify.SECTIONS` order.

        Regulatory classes come first in that order, so a post filed as both
        `circular` and `notice` reports as `circular` — the more specific fact.
        """
        for candidate in SECTIONS:
            if candidate in self.sections:
                return candidate
        return "unknown"

    @property
    def attachment_count(self) -> int:
        return len(self.attachments)

    @property
    def has_pdf(self) -> bool:
        return any(a.resource_type == "pdf" for a in self.attachments)


def clean_title(rendered: str | None) -> str | None:
    """Decode a WordPress `title.rendered` without altering the text.

    HTML entities are resolved (`&#8211;` → `–`) and whitespace collapsed. Nothing
    is transliterated, translated, truncated or case-folded: for a Devanagari
    regulatory title, any of those would be a fabrication.
    """
    if not isinstance(rendered, str):
        return None
    text = " ".join(html_module.unescape(rendered).split())
    return text or None


def _owner_from_url(url: str) -> tuple[str | None, str | None]:
    """`(owner, page_kind)` from Phase 2's classifier — no new URL rules here."""
    entry = classify_url(
        url=url, normalized_url=normalize_url(url),
        source_sitemap="", last_modified=None,
    )
    return entry.department, entry.page_kind


def build_document(
    post: dict,
    *,
    taxonomy: Taxonomy | None = None,
    owner_labels: dict[str, str] | None = None,
) -> NRBDocument:
    """Normalize one REST post. Pure: same input, same output, no I/O.

    Tolerant by construction — a post missing `acf`, `categories`, `title` or even
    `link` still produces a record, with the gap visible in `warnings` rather than
    raising. An inventory run over 18,567 posts must not die on one odd row, and a
    silently dropped post is a hole in the measurement.
    """
    warnings: list[str] = []

    link = post.get("link")
    if not isinstance(link, str) or not link:
        link = ""
        warnings.append("post has no link")
    url = normalize_url(link) if link else ""

    post_type = post.get("type") if isinstance(post.get("type"), str) else None
    owner, page_kind = _owner_from_url(url) if url else (None, None)
    # The post type is NRB's own statement of ownership; the URL is a second
    # witness. Disagreement is a finding, not something to silently reconcile —
    # `/federal-offices/<code>/` posts legitimately have post_type != owner.
    if owner is None and post_type in DEPARTMENT_CODES:
        owner = post_type
    elif owner and post_type and owner != post_type and post_type in DEPARTMENT_CODES:
        warnings.append(f"owner {owner!r} from URL disagrees with post type {post_type!r}")

    sections: list[str] = []
    evidence: list[str] = []
    slugs: list[str] = []
    names: list[str] = []
    raw_ids = post.get("categories")
    category_ids = tuple(i for i in raw_ids if isinstance(i, int)) if isinstance(raw_ids, list) else ()
    if taxonomy is not None:
        for category_id in category_ids:
            slug = taxonomy.slug(category_id)
            if slug:
                slugs.append(slug)
            name = taxonomy.name(category_id)
            if name:
                names.append(name)
            section, why = taxonomy.section_for(category_id)
            evidence.append(why)
            # "other" is a real category that means miscellaneous; it is not a
            # section claim, so it never suppresses `unknown`.
            if section and section != "other" and section not in sections:
                sections.append(section)
    # Report in the canonical vocabulary order so two runs agree byte for byte.
    ordered = tuple(s for s in SECTIONS if s in sections)

    attachments, attachment_warnings = extract_attachments(post, base_url=url or link)
    warnings.extend(attachment_warnings)

    acf = post.get("acf")
    extras: dict[str, Any] = {}
    redirects: bool | None = None
    if isinstance(acf, dict):
        redirect_flag = acf.get("document_redirect_to_file")
        redirects = redirect_flag if isinstance(redirect_flag, bool) else None
        for key in sorted(acf):
            if key in _MECHANICAL_ACF:
                continue
            value = acf[key]
            # Scalars only: a nested object is either a file (already handled) or
            # a structure we have no evidence about.
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                if str(value).strip():
                    extras[key] = value

    return NRBDocument(
        post_id=post.get("id") if isinstance(post.get("id"), int) else None,
        post_type=post_type,
        url=url,
        canonical_url=link or None,
        slug=post.get("slug") if isinstance(post.get("slug"), str) else None,
        title=clean_title((post.get("title") or {}).get("rendered")
                          if isinstance(post.get("title"), dict) else None),
        published=post.get("date") if isinstance(post.get("date"), str) else None,
        published_gmt=post.get("date_gmt") if isinstance(post.get("date_gmt"), str) else None,
        modified=post.get("modified") if isinstance(post.get("modified"), str) else None,
        status=post.get("status") if isinstance(post.get("status"), str) else None,
        owner=owner,
        owner_label=(owner_labels or {}).get(owner or ""),
        page_kind=page_kind,
        sections=ordered,
        section_evidence=tuple(evidence),
        category_ids=category_ids,
        category_slugs=tuple(slugs),
        category_names=tuple(names),
        attachments=attachments,
        extras=extras,
        redirects_to_file=redirects,
        warnings=warnings,
    )
