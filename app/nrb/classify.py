"""Deterministic classification of NRB URLs. Pure — no HTTP, no DB, no model.

Every rule here was written against the URLs NRB's own sitemap publishes
(probed 2026-08-13), not against an assumption about how a central bank site
*should* be laid out. The three groundings that matter:

  1. **The document taxonomy is not in the document URL.** NRB's regulatory
     content lives in per-owner custom post types — `/bfr/<slug>/`,
     `/psd/<slug>/`, `/fxm/<slug>/` — where `<slug>` is a (usually Devanagari)
     title. Nothing in that URL says whether the post is a directive, a circular
     or a notice. The directive/circular vocabulary appears ONLY on the ~257
     taxonomy archive pages under `/category/…`. So a URL-only classifier can
     name the *owner* of almost every document and the *kind* of almost none, and
     the honest way to express that is `section="unknown"` with an accurate
     `page_kind`, not a section guessed from the owner.

  2. **`page_kind` is the load-bearing field for Phase 3.** "30,000 unknown"
     reads as a broken classifier; "30,000 `document_post`, whose section the
     site only exposes on its archive pages" is a design input — it says the
     next phase has to resolve section from the archives (or each post's page),
     because there is no URL rule waiting to be discovered.

  3. **The owner code comes from the URL's first path segment, not the sitemap
     filename.** They usually agree, but `ditty_news_ticker-sitemap.xml` publishes
     `/ticker/…`, and the eight office sitemaps (`brg`, `brt`, `jnp`, `nep`, `pkr`,
     `sid`, `skt`, `dhn`) publish `/federal-offices/<code>/<slug>/`, where the
     owner is the SECOND segment. Trusting the filename would have mislabelled 385
     URLs on the live site.

Codes are never expanded to names. `bfr`, `ficpd`, `skt` are what NRB uses; which
words they stand for is not derivable from a URL, and a plausible-looking wrong
expansion in a regulatory corpus is worse than a code. (`department` therefore
holds either a department or a provincial-office code — whichever post type owns
the document.)

Classification is metadata, not truth: no URL is ever dropped for being
unrecognised. `section` distinguishes two different situations on purpose —
`unknown` means the URL carries no section signal at all (expected, and the common
case), `other` means "looked at, genuinely miscellaneous". A category that is not
in the table at all is *also* `other`, but says so in its `evidence`
("unmapped category root …") so the report can list it as a to-do. Without that
split, a reviewed-and-miscellaneous page and a category nobody has classified yet
are the same number.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

__all__ = [
    "DiscoveredURL",
    "SECTIONS",
    "RESOURCE_TYPES",
    "CONTENT_POST_TYPES",
    "DEPARTMENT_CODES",
    "classify_url",
]

# Report order: regulatory classes first, because those are the Phase 3 corpus.
SECTIONS = (
    "directive",
    "circular",
    "monetary_policy",
    "monetary_operations",  # repo/SLF/bond auctions — operations, not policy
    "act",
    "rule_bylaw",
    "guideline_manual",
    "enforcement_action",   # NRB has four categories of these; not in the brief
    "license_registry",     # licensed-institutions, list-of-bfis, profiles, …
    "notice",
    "publication",
    "report",
    "statistics",
    "research",
    "faq",                  # NRB publishes a large, department-scoped FAQ tree
    "procurement",
    "career",
    "media",
    "forex",
    "other",                # a real NRB category, not mapped here yet
    "unknown",              # the URL carries no section signal
)

RESOURCE_TYPES = ("html", "pdf", "document", "spreadsheet", "archive", "unknown")

_EXTENSIONS = {
    ".pdf": "pdf",
    ".doc": "document",
    ".docx": "document",
    ".rtf": "document",
    ".xls": "spreadsheet",
    ".xlsx": "spreadsheet",
    ".csv": "spreadsheet",
    ".zip": "archive",
    ".rar": "archive",
    ".7z": "archive",
}

# Every post-type prefix in NRB's sitemap index that owns published content and
# reads as an organisational unit (department, division or provincial office).
# Taken verbatim from the 59 child sitemaps; not expanded, not invented.
DEPARTMENT_CODES = frozenset(
    {
        "bfr", "bkd", "brg", "brt", "bsd", "btc", "cmd", "cpd", "dbs", "dhn",
        "ficpd", "fis", "fisd", "fiu", "fmd", "fxm", "gsd", "hrm", "iad", "itd",
        "jnp", "lgd", "mfd", "mint", "mlpsd", "nbfisd", "nep", "ofg", "pbrd",
        "pdm", "pkr", "psd", "red", "sid", "skt",
    }
)

# Post types that are content classes rather than owners. These DO carry a
# section, so they are mapped; they never set `department`.
_CONTENT_POST_TYPES = {
    "economic-review": "research",
    "er-article": "research",
    "gallery-post-type": "media",
    "ticker": "media",
    "tuesday-fa": "media",       # weekly financial-awareness posts
}

# The same names as a public set. Phase 4 needs "is this URL's first segment a
# post-type root?" to infer a post type for a source the REST API cannot see, and
# duplicating the list there would let the two drift.
CONTENT_POST_TYPES = frozenset(_CONTENT_POST_TYPES)

# Taxonomy roots that are not content at all.
_TAXONOMY_ROOTS = frozenset({"category", "post_tag", "keyword", "tag"})

# `/category/<slug>/…` -> section, keyed by the FIRST category segment, or by
# `first/second` where the parent is a mixed archive whose children differ (NRB's
# `public-debt-operations-archive` holds archived circulars, archived acts and
# reports side by side, so a single parent-level answer would be wrong for most of
# it). The two-segment key is tried first.
#
# Built from NRB's complete published category list. Entries mapped to `other` are
# deliberate: they were reviewed and are genuinely miscellaneous, which is why they
# are here rather than left absent — an absent category shows up in the report's
# to-do list, and that list is only useful if it is short and real.
CATEGORY_SECTIONS: dict[str, str] = {
    # --- regulatory ---
    "unified-directives": "directive",
    "directives": "directive",
    "plan-directives": "directive",
    "aml-cft-directives": "directive",
    "circulars": "circular",
    "fxm-circulars": "circular",
    "pdm-circulars": "circular",
    "aml-cft-circulars": "circular",
    "monetary-policy": "monetary_policy",
    "acts": "act",
    "acts-bylaws": "act",
    "laws-legislations": "act",
    "bylaws": "rule_bylaw",
    "rules-by-laws": "rule_bylaw",
    "rules-and-bylaws-mlpsd": "rule_bylaw",
    "merger-acquisition-bylaw": "rule_bylaw",
    "guidelines": "guideline_manual",
    "manual-guidelines": "guideline_manual",
    "policies-guidelines": "guideline_manual",
    "policies": "guideline_manual",
    "fiu-guidelines": "guideline_manual",
    "new-capital-adequacy-framework-ncaf": "guideline_manual",
    "problem-bank-resolution-framework": "guideline_manual",
    "liquidity-monitoring-framework": "guideline_manual",
    "national-strategy-mlpsd": "guideline_manual",
    "new-capital-adequacy-framework-parallel-run-in-national-level-development-"
    "banks-since-2067-68-bs": "guideline_manual",
    # Archived public-debt content: the parent is mixed, the children are not.
    "public-debt-operations-archive/public-debt-circulars-archived": "circular",
    "public-debt-operations-archive/pdmd-acts-bylaws-archived": "act",
    "public-debt-operations-archive/reports-osgs": "report",
    "public-debt-operations-archive/summary-sheet-of-domestic-debt": "statistics",
    "public-debt-operations-archive/issue-auction-calendar-of-government-"
    "borrowing": "notice",
    "public-debt-operations-archive": "other",
    # --- monetary operations (repo/SLF/SDF/bond auctions — not policy) ---
    "monetary-operations": "monetary_operations",
    "reports/summary-sheet-of-monetary-operation": "monetary_operations",
    "pdm-issue-calander": "notice",
    # --- supervisory action ---
    "enforcement-action": "enforcement_action",
    "enforcement-action-mfisd": "enforcement_action",
    "enforcement-actions-offsite-onsite": "enforcement_action",
    # NRB publishes this misspelling as a separate live category. Kept, because
    # dropping it would silently lose its posts.
    "inforcement-actions-offsite-onsite": "enforcement_action",
    # --- registries ---
    "licensed-institutions": "license_registry",
    "list-of-bfis": "license_registry",
    "banks-and-financial-institutions": "license_registry",
    "profiles-of-development-banks-class": "license_registry",
    "profiles-for-class-c-financial-institutions": "license_registry",
    "ranking-of-finance-company": "license_registry",
    "pre-qualified-companies": "procurement",
    # --- notices ---
    "notices": "notice",
    "public-notices": "notice",
    "public-notice-and-press-releases": "notice",
    "fxm-notices": "notice",
    "pdm-notices": "notice",
    "rtgs-notices": "notice",
    "ecc-notices": "notice",
    "miscellaneous-notices": "notice",
    "notices-for-article": "notice",
    "siddharthanagar-notices": "notice",
    "lagani-sambandhi-suchana": "notice",
    "holiday": "notice",
    "list-of-holidays": "notice",
    "इजाजतपत्र-रद्द": "notice",          # licence revocations
    # --- reports ---
    "reports": "report",
    "annual-reports": "report",
    "annual-supervision-report": "report",
    "financial-stability-report": "report",
    "inspection-progress-report": "report",
    "tranx-supervision-report": "report",
    "mutual-evaluation-reports": "report",
    "economic-activity-reports": "report",
    "erd-macroeconomic-reports": "report",
    "reports-mpo": "report",
    "sources-uses-and-progress-report": "report",
    "bok-kpp-study-reports": "report",
    "esrm_report": "report",
    "annual-financial-statements": "report",
    "financial-statements": "report",
    "balance-sheet": "report",
    # --- statistics ---
    "monthly-statistics": "statistics",
    "banking-and-financial-statistics": "statistics",
    "govt-finance-statistics": "statistics",
    "indicators": "statistics",
    "key-financial-indicators": "statistics",
    "key-financial-highlights": "statistics",
    "financial-highlights": "statistics",
    "quarterly-financial-highlights-of-commercial-banks": "statistics",
    "quarterly-interest-rate": "statistics",
    "interest-rate-structure": "statistics",
    "financial-inclusion-index": "statistics",
    "national-summary-data-page-nsdp": "statistics",
    "central-bank-survey-and-liquidity-position": "statistics",
    "financial-corporations-survey": "statistics",
    "current-macroeconomic-situation": "statistics",
    "current-microfinance-activities": "statistics",
    "quarterly-situation-of-mfis": "statistics",
    "past-data-and-information": "statistics",
    # --- research ---
    "research-studies": "research",
    "nrb-working-paper": "research",
    "economic-review": "research",
    "mirmire": "research",
    "study-reports": "research",
    "economic-activities-study-reports": "research",
    "strategic-analysis": "research",
    "national-risk-assessment": "research",
    "national-conference-erd": "research",
    "चौथो-ग्रामीण-कर्जा-सर्वे": "research",   # fourth rural credit survey
    # --- publications ---
    "special-publications": "publication",
    "newsletters": "publication",
    "fiu-newsletters": "publication",
    "anniversary-publications": "publication",
    "golden-jubilee-publications": "publication",
    "economic-bulletin": "publication",
    "nrb-news-in-nepali": "publication",
    "nepal-rastra-bank-samachar": "publication",
    "nepal-rastra-bank-news": "publication",
    # --- FAQ ---
    "faqs": "faq",
    "fiu-faqs": "faq",
    # --- procurement / careers ---
    "tenders": "procurement",
    "bisesh-paristhiti-nirmanadhin-kharid": "procurement",
    "results": "career",
    "open-competition-syllabus": "career",
    "model-questions": "career",
    # --- media / outreach ---
    "media-releases": "media",
    "press-releases": "media",
    "governors-speech": "media",
    "keynote-speech": "media",
    "events-programs": "media",
    "event-programs-mlpsd": "media",
    "governors-meeting": "media",
    "inauguration": "media",
    "interaction": "media",
    "workshop": "media",
    "activities": "media",
    "global-money-week": "media",
    "financial-awareness-scrolls": "media",
    "financial-literacy": "media",
    # --- forex ---
    "fxm": "forex",
    # --- reviewed, genuinely miscellaneous: institutional pages, form banks,
    # project microsites and document dumps. Mapped explicitly so they leave the
    # report's "unmapped" to-do list.
    "aml-cft-reporting-format": "other",
    "aml-cft-resources": "other",
    "category": "other",
    "concessional-loan": "other",
    "domestic-documents": "other",
    "enhancing-access-to-financial-services-eafs-project": "other",
    "fiu-forms": "other",
    "fukuwa": "other",
    "goaml": "other",
    "governors": "other",
    "international-documents": "other",
    "international-reports-documents": "other",
    "office-of-the-governor": "other",
    "others": "other",
    "raising-incomes-for-small-and-medium-farmers-project-rismfp": "other",
    "right-to-information": "other",
    "rural-self-reliance-fund-rsrf": "other",
    "sampati": "other",
    "sanction-lists": "other",
    "uncategorized": "other",
    "upload-files": "other",
    "useful-links": "other",
}

# Standalone pages (from `page-sitemap.xml`) keyed by their first path segment.
# Only mapped where the section follows from a category NRB already uses for the
# same subject — the rest of the ~40 page roots are institutional (about, contact,
# organogram, privacy policy) and correctly land in `other`.
_PAGE_SECTIONS = {
    "forex": "forex",
    "download-forex": "forex",
    "database-on-nepalese-economy": "statistics",   # the real/fiscal/external data portal
    "nsdp": "statistics",                           # cf. category national-summary-data-page-nsdp
    "egdds": "statistics",
    "weighted-average-treasury-bills-rate": "statistics",
    "bank-list": "license_registry",                # cf. category list-of-bfis
    "international-conference": "research",          # cf. category national-conference-erd
    "faqs": "faq",
    "financial-literacy": "media",
    "financial-literacy-trainers-list": "media",
    "tuesday-financial-awareness-banner": "media",
    "gallery": "media",
}

# Post types whose URLs nest the owner one level deeper:
# `/federal-offices/<office-code>/<slug>/`.
_OWNER_IN_SECOND_SEGMENT = frozenset({"federal-offices"})


@dataclass(frozen=True)
class DiscoveredURL:
    """One inventoried NRB URL, classified.

    `url` is NRB's own loc, byte-for-byte; `normalized_url` is the dedup key.
    `evidence` names the rule that fired, so every classification in the report
    can be traced back to a rule rather than argued about.
    """

    url: str
    normalized_url: str
    source_sitemap: str
    last_modified: str | None
    section: str
    department: str | None
    resource_type: str
    page_kind: str
    evidence: str


def _resource_type(path: str) -> str:
    lowered = path.lower().rstrip("/")
    for extension, kind in _EXTENSIONS.items():
        if lowered.endswith(extension):
            return kind
    return "html"


def _is_year(segment: str) -> bool:
    return len(segment) == 4 and segment.isdigit() and segment.startswith(("19", "20"))


def classify_url(
    *,
    url: str,
    normalized_url: str,
    source_sitemap: str,
    last_modified: str | None,
) -> DiscoveredURL:
    """Classify one URL. Deterministic: same input, same output, no I/O."""
    path = urlsplit(normalized_url).path
    # NRB's slugs are percent-encoded Devanagari; decode before matching so the
    # Nepali category names in CATEGORY_SECTIONS can be written readably.
    segments = [unquote(part) for part in path.split("/") if part]
    resource_type = _resource_type(path)

    section = "unknown"
    department: str | None = None
    evidence = "no rule matched"

    if not segments:
        page_kind, section, evidence = "landing_page", "other", "site root"

    elif (root := segments[0]) in _TAXONOMY_ROOTS:
        page_kind = "taxonomy_archive"
        if root == "category" and len(segments) > 1:
            # Two-segment key first: a mixed parent archive answers differently
            # for each child (see CATEGORY_SECTIONS).
            pair = "/".join(segments[1:3]) if len(segments) > 2 else ""
            if pair and pair in CATEGORY_SECTIONS:
                section, evidence = CATEGORY_SECTIONS[pair], f"category path {pair!r}"
            elif segments[1] in CATEGORY_SECTIONS:
                section = CATEGORY_SECTIONS[segments[1]]
                evidence = f"category root {segments[1]!r}"
            else:
                section = "other"
                evidence = f"unmapped category root {segments[1]!r}"
        else:
            section = "other"
            evidence = f"taxonomy root {root!r}"

    elif root == "departments":
        page_kind = "department_page"
        section = "other"
        if len(segments) > 1 and segments[1] in DEPARTMENT_CODES:
            department = segments[1]
        evidence = "/departments/<code>/ page"

    elif root == "provincial-offices":
        page_kind, section, evidence = "office_page", "other", "provincial office page"

    elif root in _OWNER_IN_SECOND_SEGMENT:
        # `/federal-offices/<code>/<slug>/` — the office code is one level in.
        if len(segments) > 1 and segments[1] in DEPARTMENT_CODES:
            department = segments[1]
            page_kind = "post_type_archive" if len(segments) == 2 else "document_post"
            evidence = f"office post type {root!r}/{segments[1]!r}"
        else:
            page_kind, section = "office_page", "other"
            evidence = f"office post type {root!r}, unrecognised office code"

    elif _is_year(root) and len(segments) >= 3:
        # WordPress's default /YYYY/MM/slug/ permalink: the blog-style posts.
        page_kind, evidence = "news_post", "dated /YYYY/MM/ permalink"

    elif root in _CONTENT_POST_TYPES:
        section = _CONTENT_POST_TYPES[root]
        page_kind = "post_type_archive" if len(segments) == 1 else "document_post"
        evidence = f"content post type {root!r}"

    elif root in DEPARTMENT_CODES:
        department = root
        page_kind = "post_type_archive" if len(segments) == 1 else "document_post"
        # Deliberately no section: see the module docstring. The owner does not
        # imply the kind — a /bfr/ post may be a directive, a circular or a FAQ.
        evidence = f"owner post type {root!r}"

    else:
        page_kind = "page"
        if root in _PAGE_SECTIONS:
            section = _PAGE_SECTIONS[root]
            evidence = f"known page {root!r}"
        else:
            section = "other"
            evidence = f"unrecognised path root {root!r}"

    return DiscoveredURL(
        url=url,
        normalized_url=normalized_url,
        source_sitemap=source_sitemap,
        last_modified=last_modified,
        section=section,
        department=department,
        resource_type=resource_type,
        page_kind=page_kind,
        evidence=evidence,
    )
