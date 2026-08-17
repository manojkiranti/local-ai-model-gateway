"""ORM models for the persistent NRB catalog (Phase 4).

Three domain objects and an operational log, deliberately separate:

  * `nrb_sources` — a **logical** NRB post: a circular, a directive, a statistical
    release. What NRB published.
  * `nrb_files`   — a **physical** attachment: one downloadable resource. Where
    the bytes are.
  * `nrb_source_files` — the many-to-many between them, because Phase 3 measured
    the corpus and "one source = one file" is false: 1.1% of posts have no
    attachment and 0.7% carry two (a circular plus its annex). Collapsing the
    file onto the source would silently drop one of every pair.
  * `nrb_sync_runs` — what one reconciliation did, so "did the sync work and what
    changed" is a query rather than a log grep.

Nothing here is model-facing. `LOCAL_TOOLS` is untouched; the catalog is
populated by `scripts/nrb_sync.py` and read (later) by Phase 5+.

Two identity decisions carry the whole design, and both exist because NRB spells
the same URL two ways (`attachments.comparison_key`'s docstring has the measured
evidence):

  * **`nrb_files.comparison_key`** is the percent-decoded URL. The REST API
    returns `…/आगलागी-२०७४.pdf` literally; the post's 302 `Location` returns the
    same file percent-encoded. Keying on the raw URL would double-count it.
  * **`nrb_sources.url_key`** is the same normalization plus a trailing-slash
    strip. The sitemap publishes Devanagari slugs percent-encoded while REST
    returns them literally, so matching REST posts against sitemap URLs on raw
    strings would report every one of the ~18,370 REST documents as missing from
    the sitemap — and then insert it a second time as a "sitemap only" row.
    `page_url` keeps NRB's own spelling because that is what a fetcher should
    request; `url_key` is only ever compared.

Integer PKs rather than the UUID-hex used by `documents`/`ingest_jobs`: the
project convention is UUID-hex for rows the frontend renders and integer PKs for
internal tables, and this is an internal catalog of ~18.5k rows joined on every
sync. A narrow join table matters more here than an opaque id.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from ..db.base import Base

__all__ = [
    "EXTRACTION_PREVIEW_CHARS",
    "EXTRACTION_REASONS",
    "EXTRACTION_STATUSES",
    "FETCH_BLOCKED_HOST",
    "FETCH_FAILED",
    "FETCH_FETCHED",
    "FETCH_PENDING",
    "FETCH_RUN_COMPLETED",
    "FETCH_RUN_FAILED",
    "FETCH_RUN_PARTIAL",
    "FETCH_RUN_RUNNING",
    "FETCH_STATUSES",
    "NRBExtraction",
    "NRBFetchRun",
    "NRBRecovery",
    "NRBRecoveryUnit",
    "RECOVERY_PLANS",
    "RECOVERY_ROUTES",
    "METADATA_STATUSES",
    "METADATA_STATUS_REST",
    "METADATA_STATUS_SITEMAP_ONLY",
    "NRBFile",
    "NRBSource",
    "NRBSourceFile",
    "NRBSyncRun",
    "RELATIONSHIP_TYPES",
    "REL_ACF",
    "REL_BODY_LINK",
    "REL_PRIMARY",
    "REL_SECONDARY",
    "RUN_COMPLETED",
    "RUN_FAILED",
    "RUN_PARTIAL",
    "RUN_RUNNING",
    "RUN_STATUSES",
]

# --------------------------------------------------------------------------- #
# Closed vocabularies.
#
# Every one of these is backed by a CHECK constraint. That is not hygiene: the
# same lesson as `ck_ingest_jobs_status` in app/rag/models.py — a typo'd status
# ('blockedhost') would match no predicate and no query, so the row would look
# fetchable to Phase 5 while reading as blocked to a human. Adding a value means
# editing the CHECK.
# --------------------------------------------------------------------------- #

# nrb_sources.metadata_status — where this row's metadata came from.
METADATA_STATUS_REST = "rest"                  # NRB's WordPress REST API
METADATA_STATUS_SITEMAP_ONLY = "sitemap_only"  # in the sitemap, REST cannot see it
METADATA_STATUSES = (METADATA_STATUS_REST, METADATA_STATUS_SITEMAP_ONLY)

# nrb_files.fetch_status. Phase 4 added the first two; Phase 5 added the last two
# when it started downloading, editing the CHECK rather than bypassing it.
FETCH_PENDING = "pending"          # discovered, fetchable, not yet fetched
FETCH_BLOCKED_HOST = "blocked_host"  # the host guard refuses it; never fetchable
FETCH_FETCHED = "fetched"          # bytes on disk, hashed, type verified
FETCH_FAILED = "failed"            # a fetch was attempted and did not produce a file
FETCH_STATUSES = (FETCH_PENDING, FETCH_BLOCKED_HOST, FETCH_FETCHED, FETCH_FAILED)

# nrb_fetch_runs.status — same vocabulary and same meaning as nrb_sync_runs.
FETCH_RUN_RUNNING = "running"
FETCH_RUN_COMPLETED = "completed"
FETCH_RUN_PARTIAL = "partial"
FETCH_RUN_FAILED = "failed"

# nrb_source_files.relationship_type — which NRB field carried the attachment.
REL_PRIMARY = "primary"      # acf.document_file — what the post URL 302s to
REL_SECONDARY = "secondary"  # acf.secondary_file — usually an annex
REL_ACF = "acf"              # another file-shaped ACF field
REL_BODY_LINK = "body_link"  # an anchor in the rendered post body
RELATIONSHIP_TYPES = (REL_PRIMARY, REL_SECONDARY, REL_ACF, REL_BODY_LINK)

# nrb_sync_runs.status. `partial` is a real outcome, not a hedge: a run that
# reconciled everything it discovered but whose discovery was incomplete is
# neither a success nor a failure, and it is the state in which absence-based
# deactivation is forbidden.
RUN_RUNNING = "running"
RUN_COMPLETED = "completed"
RUN_PARTIAL = "partial"
RUN_FAILED = "failed"
RUN_STATUSES = (RUN_RUNNING, RUN_COMPLETED, RUN_PARTIAL, RUN_FAILED)


class NRBSource(Base):
    """One logical NRB document/post.

    Identity, in order of preference:

      1. `(wp_post_type, wp_post_id)` — WordPress's own id. Enforced by a
         PARTIAL unique index because a sitemap-only row has no post id.
      2. `url_key` — enforced unique unconditionally. This is what gives a
         sitemap-only row an identity at all, and what lets it be *upgraded* in
         place (post id filled in, `metadata_status` promoted to `rest`) if NRB
         ever registers that post type, instead of becoming a duplicate.

    Never the title: NRB publishes near-identical Devanagari titles across years,
    and 3 documents have no title at all.
    """

    __tablename__ = "nrb_sources"
    __table_args__ = (
        CheckConstraint(
            "metadata_status IN ('rest', 'sitemap_only')",
            name="ck_nrb_sources_metadata_status",
        ),
        # A post id without a type (or the reverse) cannot participate in the
        # partial unique index below, so it would silently escape identity.
        CheckConstraint(
            "(wp_post_id IS NULL) OR (wp_post_type IS NOT NULL)",
            name="ck_nrb_sources_post_identity",
        ),
        # Primary identity. Partial because sitemap-only rows have no post id and
        # a plain UNIQUE would let exactly one of them exist.
        Index(
            "ux_nrb_sources_post",
            "wp_post_type",
            "wp_post_id",
            unique=True,
            postgresql_where=text("wp_post_id IS NOT NULL"),
        ),
        # Fallback identity, and the join key against the sitemap inventory.
        Index("ux_nrb_sources_url_key", "url_key", unique=True),
        # Phase 5's corpus selection is by document type (the regulatory core is
        # ~1,800 of 18,370 rows), which is the one selective predicate this table
        # is known to be queried by. Dates and `is_active` are deliberately NOT
        # indexed: an 18k-row scan is sub-millisecond and the sync itself never
        # queries by them.
        Index("ix_nrb_sources_document_type", "document_type"),
        Index("ix_nrb_sources_owner", "owner"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # --- identity -------------------------------------------------------- #
    wp_post_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wp_post_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # NRB's own spelling of the URL, normalized only for scheme/host/tracking
    # params (`http.normalize_url`). What a fetcher should request.
    page_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    # The comparison form: percent-decoded, no trailing slash. Compared, never
    # fetched. See the module docstring.
    url_key: Mapped[str] = mapped_column(String(2048), nullable=False)
    # WordPress's canonical link, verbatim. Kept because a disagreement with
    # `page_url` is a finding (Phase 3 measured zero, so a non-zero count means
    # something changed upstream).
    canonical_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    slug: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # --- content metadata ------------------------------------------------ #
    # Text, not String(n): these are Devanagari regulatory titles and truncating
    # one is a fabrication.
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Parsed to an instant. The offset is derived from NRB's own `date` vs
    # `date_gmt` pair rather than assumed — see `records.parse_wp_datetime`. The
    # raw strings survive in `meta` so the parse is auditable.
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    modified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # From the sitemap, not REST. Persisted for later phases (chronology of
    # amendments) and NOT part of `metadata_hash`: Yoast derives it from
    # post_modified, which is hashed already, so including it would make a
    # sitemap-less run report every source as changed.
    sitemap_lastmod: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- ownership + classification -------------------------------------- #
    # NRB's department/office code (`bfr`, `psd`, `skt`). Called `owner` rather
    # than `department` on purpose: `department` in this codebase is the RAG
    # permission boundary, and reusing the word here would read as access
    # control. Codes are never expanded to names.
    owner: Mapped[str | None] = mapped_column(String(32), nullable=True)
    page_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # NULL means "not known", and that is a legitimate persisted answer for 28%
    # of the corpus — 5,052 documents sit in WordPress's `upload-files` catch-all
    # from NRB's 2019 CMS migration. Guessing a type from a Devanagari title to
    # improve the coverage number would put a wrong fact in a regulatory
    # catalog. Left NULL; Phase 6+ may reclassify from another signal.
    document_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # The rule that produced `document_type` (`category path 'circulars/2082-83'`),
    # so a disputed classification is traceable rather than arguable.
    classification_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ALL resolved sections, ordered by `classify.SECTIONS`. A post really is
    # filed under several; `document_type` is only the first of these.
    sections: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    # NRB's raw taxonomy (category ids/slugs/names + per-category evidence), so a
    # future reclassification can run against the database instead of re-crawling
    # 18,370 posts.
    raw_taxonomy: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # `metadata` is reserved by SQLAlchemy declarative, so the attribute is `meta`
    # (same as documents/document_chunks). Holds the deterministic ACF extras
    # (`circular_number`, `fiscal_year`, tender dates), the raw date strings, and
    # the extraction warnings.
    meta: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    # --- sync bookkeeping ------------------------------------------------- #
    metadata_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=METADATA_STATUS_REST
    )
    # sha256 over the normalized upstream metadata — the answer to "did this
    # source materially change?". Excludes every observational field, so a second
    # identical sync computes the same value and reports zero updates.
    metadata_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Which run last saw this row. Doubles as the deactivation predicate: a row
    # this run did not stamp is a row upstream no longer publishes, which avoids
    # an 18k-element NOT IN.
    last_sync_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("nrb_sync_runs.id", ondelete="SET NULL"), nullable=True
    )
    # Soft state. A source is NEVER hard-deleted for disappearing from one sync:
    # NRB reorganises, and a deleted row loses the fact that NRB ever published
    # the document.
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    deactivated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class NRBFile(Base):
    """One distinct external attachment.

    Conservative by construction: a file row is never deleted because a source
    stopped referencing it (another source may reference the same file — 42
    duplicate references were measured across the corpus — and the historical
    fact that NRB published it matters). Only the relationship goes away.
    """

    __tablename__ = "nrb_files"
    __table_args__ = (
        CheckConstraint(
            "fetch_status IN ('pending', 'blocked_host', 'fetched', 'failed')",
            name="ck_nrb_files_fetch_status",
        ),
        # A blocked file must say why. Without this, "blocked with no reason" is
        # indistinguishable from a bug in the host guard. Phase 5 widened it from
        # an equality to an implication: a `failed` row also carries a reason (in
        # `fetch_error`), and a fetched row must not claim to be blocked.
        CheckConstraint(
            "(fetch_status = 'blocked_host') = (blocked_reason IS NOT NULL)",
            name="ck_nrb_files_blocked_reason",
        ),
        # A `fetched` row that cannot say WHICH bytes it has is worse than no row:
        # Phase 6 would read it as available and find nothing. All three or none.
        CheckConstraint(
            "(fetch_status <> 'fetched') OR (content_sha256 IS NOT NULL"
            " AND content_length IS NOT NULL AND storage_key IS NOT NULL)",
            name="ck_nrb_files_fetched_is_complete",
        ),
        Index("ux_nrb_files_comparison_key", "comparison_key", unique=True),
        # The work queue is `WHERE fetch_status = 'pending'`, so this is the one
        # queue-shaped predicate. `host` is NOT indexed: the live corpus has two
        # distinct values (18,295 / 3).
        Index("ix_nrb_files_fetch_status", "fetch_status"),
        # Not unique: two rows legitimately share bytes (the same PDF republished
        # under a second URL), which is exactly what this index makes findable.
        Index("ix_nrb_files_content_sha256", "content_sha256"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # Percent-decoded identity — `attachments.comparison_key`, reused rather than
    # reimplemented. Two spellings of one file collapse to one row here.
    comparison_key: Mapped[str] = mapped_column(String(2048), nullable=False)
    # NRB's own spelling. This is the string a downloader must request; the
    # decoded form is not guaranteed to be a valid request target.
    source_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    filename: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # --- what NRB says this file is (never verified here) ----------------- #
    # WordPress's own `mime_type`, recorded at upload from the bytes. Better
    # evidence than a filename, and available for 99.6% of the corpus — but
    # still NRB's claim, not a validated fact. Phase 5 validates.
    reported_mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    extension: Mapped[str | None] = mapped_column(String(16), nullable=True)
    resource_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # 'mime' | 'extension' | 'none' — which of the two above decided
    # `resource_type`, so a rewrite of the typing rules can be scoped.
    type_source: Mapped[str] = mapped_column(String(16), nullable=False)
    # ACF's recorded byte size. A cheap change signal, and a sanity check
    # against what Phase 5 actually downloads.
    reported_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    wp_attachment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    host: Mapped[str] = mapped_column(String(255), nullable=False)

    # --- fetchability ----------------------------------------------------- #
    # Decided by the SAME host guard the fetchers use (`http.check_url` with
    # require_https), not by a second opinion. The three live attachments on
    # `http://uat.nrb.org.np/` land here as blocked: the catalog records that NRB
    # referenced them, and nothing in the codebase will fetch them.
    fetch_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=FETCH_PENDING
    )
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- the downloaded bytes (Phase 5; all NULL until a fetch succeeds) ---- #
    # Identity of the CONTENT, as distinct from `comparison_key`, which is the
    # identity of the URL. Two URLs with one sha256 are one file republished; one
    # URL whose sha256 changed is a document NRB edited in place.
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_length: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # RELATIVE key under NRB_FILES_DIR, content-addressed
    # (`<sha256[:2]>/<sha256>.<ext>`) — see `app/nrb/filestore.py`.
    storage_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # What the BYTES say they are, from magic numbers (`app/nrb/sniff.py`) — our
    # own determination, kept beside `reported_mime_type`, which is NRB's claim.
    # Both are retained because a disagreement is a finding, not a correction.
    sniffed_mime: Mapped[str | None] = mapped_column(String(128), nullable=True)
    downloaded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Attempts and the last failure, so a retry pass can be selective and a
    # permanently-broken URL is visible instead of being retried forever.
    fetch_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    fetch_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_fetch_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("nrb_fetch_runs.id", ondelete="SET NULL"), nullable=True
    )

    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_sync_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("nrb_sync_runs.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class NRBSourceFile(Base):
    """Which files a source publishes, in NRB's own order.

    The composite primary key IS the `UNIQUE (source_id, file_id)` the design
    requires — a duplicate relationship is impossible rather than merely avoided.

    `ondelete` is asymmetric on purpose: removing a source takes its
    relationships with it (CASCADE), but a file cannot be deleted while any
    source still references it (RESTRICT). The physical catalog is the
    conservative half.
    """

    __tablename__ = "nrb_source_files"
    __table_args__ = (
        CheckConstraint(
            "relationship_type IN ('primary', 'secondary', 'acf', 'body_link')",
            name="ck_nrb_source_files_relationship_type",
        ),
        # No separate UNIQUE(source_id, file_id): the composite PK below already
        # IS that constraint, and declaring both makes Postgres and Alembic
        # disagree about how many indexes the table should have (a permanent
        # `alembic check` drift).
        # The PK covers source_id lookups; this covers the other direction —
        # "which sources reference this file", which is what makes it safe to
        # keep a file whose last relationship was just removed.
        Index("ix_nrb_source_files_file", "file_id"),
    )

    source_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("nrb_sources.id", ondelete="CASCADE"), primary_key=True
    )
    file_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("nrb_files.id", ondelete="RESTRICT"), primary_key=True
    )
    # Position in NRB's own attachment order (document_file, secondary_file, …).
    # For a circular plus its annex, order is which is which.
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    relationship_type: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class NRBFetchRun(Base):
    """One download pass. The operator's answer to "what did we pull, and what broke".

    Deliberately a separate table from `nrb_sync_runs` rather than a `kind` column
    on it: the two commands answer different questions and share almost no
    counters. A sync's units are sources and relationships; a fetch's are files and
    bytes. One table with two disjoint halves would leave every row half NULL and
    make both reports read around the other's columns.

    `scope` is the part that matters months later. A fetch is always partial by
    design — you choose a slice of 18.3k files — so "which files were even
    considered" is not derivable from the counters. It records the selection
    (sections, owners, limit, whether failures were retried) verbatim.
    """

    __tablename__ = "nrb_fetch_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'completed', 'partial', 'failed')",
            name="ck_nrb_fetch_runs_status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=FETCH_RUN_RUNNING
    )
    dry_run: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # The selection this run was given: {"sections": [...], "owners": [...],
    # "limit": N, "retry_failed": bool, ...}. See the class docstring.
    scope: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    # --- counters -------------------------------------------------------- #
    files_selected: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    files_fetched: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    files_failed: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    files_skipped: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    # Fetched, hashed, and the bytes turned out to be already on disk under
    # another URL. Counted apart from `files_fetched` because it is the measure of
    # how much duplication NRB's corpus actually contains.
    files_deduplicated: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    # Off the wire vs newly written to disk. They differ by exactly the duplicates.
    bytes_downloaded: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    bytes_stored: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    error_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    # Bounded samples of failures by kind, plus why the run stopped if it did.
    notes: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class NRBSyncRun(Base):
    """One reconciliation. The operator's answer to "did it work, what changed".

    `discovery_complete` and `deactivation_applied` are separate booleans rather
    than one derived flag because they answer different questions, and the pair
    is the audit trail for the safety rule that matters most here: a run whose
    discovery was incomplete must never deactivate anything. If thousands of
    sources ever go inactive, this row says whether that was allowed to happen.
    """

    __tablename__ = "nrb_sync_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'completed', 'partial', 'failed')",
            name="ck_nrb_sync_runs_status",
        ),
        # Deactivation is only legal on a complete discovery. Encoded here as
        # well as in the service so a future caller cannot write the impossible
        # combination even by accident.
        CheckConstraint(
            "NOT (deactivation_applied AND NOT discovery_complete)",
            name="ck_nrb_sync_runs_deactivation_needs_complete",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=RUN_RUNNING
    )
    # True only when every REST collection and the whole sitemap were read
    # without an error or a truncating bound. The precondition for deactivation.
    discovery_complete: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    deactivation_applied: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    dry_run: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    # --- counters -------------------------------------------------------- #
    sitemaps_seen: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    sources_seen: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    sources_created: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    sources_updated: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    sources_unchanged: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    sources_deactivated: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    sources_reactivated: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    sitemap_only_sources: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    files_seen: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    files_created: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    files_updated: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    files_unchanged: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    blocked_files: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    relationships_created: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    relationships_removed: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    # A file that stayed attached but changed field or position (an annex becoming
    # the primary document). Counted apart from created/removed because it is
    # neither, and conflating it would hide a real editorial change.
    relationships_updated: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    error_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    warning_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    # Bounded samples plus the structured findings (which bounds truncated the
    # run, which post types REST did not serve, which sitemap kinds were skipped).
    # Capped in the service: 18,370 warnings would make this row unreadable and
    # the counts above are the aggregate.
    notes: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# --------------------------------------------------------------------------- #
# Phase 6A: native extraction results.
#
# The vocabularies are re-stated here as literals rather than imported from
# `app/nrb/quality.py`. `quality.py` is a pure, dependency-free module and the
# ORM must not become a reason for it to grow imports; the CHECK strings below
# and `quality.STATUSES`/`quality.REASONS` are kept in step by
# `test_the_database_vocabularies_match_the_classifiers`.
# --------------------------------------------------------------------------- #
EXTRACTION_STATUSES = (
    "extracted", "suspicious", "needs_ocr", "unsupported", "failed",
)
EXTRACTION_REASONS = (
    "clean", "legacy_font_suspected", "partial_text_coverage",
    "replacement_characters", "control_characters", "low_printable_ratio",
    "empty_spreadsheet", "no_text_layer", "sparse_text_layer", "image_file",
    "no_native_parser", "parser_error",
)

# The preview is a sanity-check window, not a text cache. Enforced by a CHECK
# (see the class docstring) rather than trusted to the writer.
EXTRACTION_PREVIEW_CHARS = 300


def _in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


class NRBExtraction(Base):
    """One native-extraction attempt on one BLOB. Content-intrinsic, always.

    Keyed on `content_sha256`, not on an `nrb_files.id`, and that is the whole
    design decision. Storage is content-addressed and a blob is shared: Phase 3
    measured 42 duplicate attachment references and Phase 5 found byte-identical
    duplicates within the first 25 files. Per-file-row extraction would parse the
    same bytes twice and store two answers to one question.

    It also forbids something subtler. A source TITLE is a useful quality signal
    (a Devanagari title over zero-Devanagari text is strong corroboration of a
    legacy-font extraction), but a blob referenced by one Devanagari-titled and
    one English-titled source would store a different verdict depending on which
    source the pass happened to reach first. That is non-deterministic persisted
    state, and it would break the second-run-is-identical invariant every earlier
    phase holds. So **every column here is a function of the bytes alone**, and
    the title-assisted signal lives in `profile.py`, computed over ALL referencing
    sources at read time.

    `extractor_version` is the other half of the key and the invalidation handle:
    bumping it makes every stored result stale and re-extractable without
    deleting anything, and "which blobs are stale" stays a
    `WHERE extractor_version <> …` query rather than a framework. That query
    scans (see the index note below); it is run by an operator, not by the pass.

    **No extracted text is stored.** Only `preview`, and that is held to
    `EXTRACTION_PREVIEW_CHARS` by a CHECK rather than by the writer remembering:
    Phase 7 re-parses with Docling for chunking anyway, and a text column that
    could hold a whole document is something a later phase would eventually embed
    by accident. Making it structurally impossible is cheaper than documenting
    that it must not be done.

    Column vs JSONB: the values Phase 6B is expected to FILTER on are columns
    (status, reason, and the severity measures that size an OCR cohort); the full
    metric set is `metrics` JSONB, because the metrics will evolve with the rules
    and a column per idea means a migration per idea.
    """

    __tablename__ = "nrb_extractions"
    __table_args__ = (
        CheckConstraint(
            _in_list("status", EXTRACTION_STATUSES),
            name="ck_nrb_extractions_status",
        ),
        CheckConstraint(
            _in_list("reason", EXTRACTION_REASONS),
            name="ck_nrb_extractions_reason",
        ),
        # A `failed` row that cannot say why is indistinguishable from a bug, and
        # a row claiming success must not carry an error. Same shape as
        # `ck_nrb_files_blocked_reason`.
        CheckConstraint(
            "(status = 'failed') = (error IS NOT NULL)",
            name="ck_nrb_extractions_error",
        ),
        # The legacy ratio's two halves travel together or not at all, and the
        # numerator can never exceed the denominator — an impossible ratio is
        # unrepresentable rather than merely unlikely.
        CheckConstraint(
            "(legacy_lines IS NULL) = (judged_lines IS NULL)"
            " AND (legacy_lines IS NULL OR legacy_lines <= judged_lines)",
            name="ck_nrb_extractions_legacy_counts",
        ),
        # THE guarantee that this table never becomes a document store.
        CheckConstraint(
            f"preview IS NULL OR char_length(preview) <= {EXTRACTION_PREVIEW_CHARS}",
            name="ck_nrb_extractions_preview_is_bounded",
        ),
        # Identity. One answer per (bytes, extractor), so a repeat pass is a
        # no-op rather than a second opinion. `content_sha256` leads, so this
        # index also serves the lookup by blob and the join back to
        # `nrb_files.content_sha256` — a separate single-column index on it would
        # be redundant. It does NOT serve a version-only scan
        # (`WHERE extractor_version <> …`): that column is second, so the
        # staleness query is a sequential scan. Deliberate, and left alone for
        # now — it is an occasional operator query over one row per blob, not a
        # hot path, and an index earns its write cost only once that changes.
        Index(
            "ux_nrb_extractions_content_version",
            "content_sha256",
            "extractor_version",
            unique=True,
        ),
        # Phase 6B's work queue is `WHERE status IN ('needs_ocr', 'suspicious')`.
        Index("ix_nrb_extractions_status", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # --- identity: the extraction INPUT ---------------------------------- #
    # Not a foreign key to `nrb_files`: the relationship is many rows to one
    # blob, and a file row being re-fetched or removed must not orphan a
    # perfectly valid extraction OF THE SAME BYTES.
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    extractor_version: Mapped[str] = mapped_column(String(32), nullable=False)
    parser: Mapped[str] = mapped_column(String(32), nullable=False)
    media_family: Mapped[str] = mapped_column(String(16), nullable=False)

    # --- the verdict ------------------------------------------------------ #
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # The rule that fired, so a disputed verdict is traceable rather than
    # arguable — the role `classification_source` plays on `nrb_sources`.
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    # Findings that did NOT change the status (a partly-scanned but mostly
    # readable PDF, a document too short to measure).
    warnings: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    # --- severity measures Phase 6B filters on ---------------------------- #
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pages_with_text: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # How many pages carry any text at all.
    text_page_coverage: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Two medians, because one cannot separate the two faults: over ALL pages it
    # collapses to 0 once most pages are blank, so a partly-scanned document
    # would read as having a sparse text layer, which is a different diagnosis.
    median_chars_per_page: Mapped[float | None] = mapped_column(Float, nullable=True)
    # How much text the readable pages carry — the `sparse_text_layer` input.
    median_chars_per_text_page: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    char_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    devanagari_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    # THE legacy-font signal, promoted to a column because it is what sizes the
    # OCR cohort: 0.28 and 1.00 are both "suspicious" but describe very different
    # documents.
    legacy_line_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Its numerator and denominator. A ratio alone cannot be audited — 0.5 over 4
    # judged lines is not 0.5 over 900 — and `judged_lines` against `line_count`
    # in `metrics` is the only way to see how much was too short to assess.
    legacy_lines: Mapped[int | None] = mapped_column(Integer, nullable=True)
    judged_lines: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Everything from `quality.TextMetrics` plus the page/sheet stats. JSONB
    # because the metric set evolves with the rules.
    metrics: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # <= EXTRACTION_PREVIEW_CHARS, enforced by CHECK. NOT a text cache.
    preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Exception TYPE plus a short message. Never a stack trace, never a
    # filesystem path — the rule `app/files/documents.py` already follows.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    extracted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# --------------------------------------------------------------------------- #
# Phase 7 step 2 — the versioned recovery cache.
#
# The vocabularies are restated here rather than imported from `recovery.py`,
# for the same reason `EXTRACTION_STATUSES` is restated: `models.py` is imported
# by Alembic's autogenerate and by the API image, and `recovery.py` pulls in
# `legacy_convert`, `provenance` and `ocr`. A CHECK constraint must not drag the
# OCR stack into a migration. `test_the_cache_vocabularies_match_recovery` keeps
# the two copies honest.
# --------------------------------------------------------------------------- #
RECOVERY_ROUTES = ("native", "legacy_conversion", "ocr")
RECOVERY_PLANS = ("keep_native", "convert_units", "route_pages", "no_recovery")


class NRBRecovery(Base):
    """One blob's recovered text under one ROUTING version. Content-intrinsic.

    THE PRODUCTION REUSE BOUNDARY, and deliberately not `nrb_extractions`. That
    table is Phase 6 evidence: it is written by a measurement pass an operator
    may never have run, it stores no text, and putting it on the ingestion path
    would make every future ingest depend on a profiling run
    (`docs/nrb-integration.md` §19, §20.1). This table is written BY ingestion,
    for ingestion.

    WHAT THIS TABLE IS THAT NO OTHER NRB TABLE IS
        A document store. `nrb_extractions` has a CHECK
        (`ck_nrb_extractions_preview_is_bounded`) that exists specifically to
        stop it becoming one. Here that is the point — the whole value of the
        cache is not re-running a 2-4 s/page OCR — so there is no such bound,
        and the inversion is stated rather than left to be discovered. What is
        stored is exactly the text `rag.chunks_from_recovery` would chunk:
        `recovery._withhold` has already run, so the glyph-mapped original of an
        unresolved unit is not here and cannot be resurrected from here.

        It is also GLOBAL and department-agnostic, keyed by bytes like
        `nrb_files`. Two departments ingesting the same blob share one recovery.
        It carries no embedding, no `tsv` and no vector index, and nothing in
        `app/rag/retrieval` can reach it.

    IDENTITY: (content_sha256, base_version)
        `content_sha256` for the reason `nrb_extractions` uses it — storage is
        content-addressed, a blob is shared by several catalog rows, and every
        column here is a function of the bytes alone. New bytes are a new hash
        and therefore a new row; a cache collision across a republished file is
        not possible.

        `base_version` is the ROUTING identity (see
        `recovery_cache.BASE_VERSION`): the native classifier, this module's
        routing rules, the provenance algorithm and the two gate constants.
        Anything that could change WHICH ROUTE a unit gets. It is explicitly NOT
        `extractor_version` alone — that would make a converter upgrade serve
        stale text — and explicitly NOT the whole dependency set either, because
        then an OCR model bump would invalidate every deterministic conversion
        in the corpus. What a route PRODUCES is versioned per unit, on
        `nrb_recovery_units.engine_version`.

        Rows for a superseded `base_version` are kept side by side, exactly as
        native-1 and native-2 rows are. Deleting is an explicit operator action
        (`scripts/nrb_recovery_cache.py --purge`), never a side effect of a
        write: a cache row is also the record of what was indexed at the time.
    """

    __tablename__ = "nrb_recoveries"
    __table_args__ = (
        CheckConstraint(
            _in_list("plan", RECOVERY_PLANS), name="ck_nrb_recoveries_plan"
        ),
        CheckConstraint(
            "gate_ratio IS NULL OR (gate_ratio >= 0 AND gate_ratio <= 1)",
            name="ck_nrb_recoveries_gate_ratio",
        ),
        # Identity. `content_sha256` leads so the index also serves the lookup
        # by blob — which is the hot path, once per ingest.
        Index(
            "ux_nrb_recoveries_content_version",
            "content_sha256",
            "base_version",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # Not a foreign key to `nrb_files`, for the same reason `nrb_extractions`
    # is not: a file row being re-fetched must not orphan a valid recovery OF
    # THE SAME BYTES.
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    base_version: Mapped[str] = mapped_column(String(255), nullable=False)

    # --- everything `RecoveredDocument` carries above the unit level ------- #
    family: Mapped[str] = mapped_column(String(16), nullable=False)
    plan: Mapped[str] = mapped_column(String(32), nullable=False)
    plan_reason: Mapped[str] = mapped_column(String(64), nullable=False)
    # The document's `unit_legacy_ratio` as the router read it. Load-bearing on
    # a refresh, not decorative: `convert_unit` takes it as
    # `document_legacy_ratio`, and re-deriving it per page is the exact mistake
    # §16 warns about (a 1.0-ratio document's page 1 gated on its own three
    # headings). Storing it is what lets ONE stale page be reconverted without
    # re-classifying the whole document.
    gate_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    warnings: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    unit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class NRBRecoveryUnit(Base):
    """One recovered unit — a PDF PAGE, a spreadsheet SHEET, or a whole document.

    The unit is whatever `recovery.py` already returns as a `PageText`, never a
    granularity invented here. For a PDF that is the source page, 1-indexed and
    the same number `provenance`, `documents.read_pdf_pages` and `ocr.ocr_page`
    use, so a citation needs no translation table. For a workbook it is the
    sheet, in workbook order, with the sheet name in `label` — fake page numbers
    on a spreadsheet would make `document_chunks.page_number` a lie. For a
    single-stream `.docx`/`.txt` it is unit 1.

    WHY THE ROUTE ENGINE IS VERSIONED HERE AND NOT ON THE HEADER
        This is the whole selectivity property. `engine_version` is the identity
        of whatever produced THIS unit's text — npttf2utf + mapping + lexicon
        fingerprint for a conversion, the PP-OCR model + backend for an OCR
        page, the extractor for a native passthrough. A unit is reusable when
        its stored `engine_version` equals the current one for its route, so:

          * a new OCR model makes OCR pages stale and leaves every converted
            page in the corpus reusable;
          * a converter or lexicon change makes converted units stale and leaves
            scanned pages alone;
          * a routing change bumps `base_version` instead, which invalidates the
            whole document — correctly, because the ROUTES themselves may differ.

        `e08988860534` is the case that makes this concrete: page 1 is OCR and
        pages 2-50 are conversion, in one document.

    UNRESOLVED UNITS ARE STORED, WITH THEIR REASON
        `ok = false` with empty text is a first-class cached outcome, not an
        omission. Caching only the successes means a deterministically
        unrecoverable page re-runs OCR on every ingest forever. `indexable` is
        NOT stored: it is recomputed from `(ok, text)` by `PageText`, so a
        cache row cannot assert a trust state the current rules would refuse.
    """

    __tablename__ = "nrb_recovery_units"
    __table_args__ = (
        CheckConstraint(
            _in_list("route", RECOVERY_ROUTES), name="ck_nrb_recovery_units_route"
        ),
        # A unit that succeeded must not carry an error. The converse is not an
        # invariant: `conversion_unresolved` fails with text (the guards kept
        # some lines) and an unavailable engine fails with none.
        CheckConstraint(
            "error IS NULL OR ok = false", name="ck_nrb_recovery_units_error"
        ),
        CheckConstraint("unit_number >= 1", name="ck_nrb_recovery_units_number"),
        Index(
            "ux_nrb_recovery_units_recovery_number",
            "recovery_id",
            "unit_number",
            unique=True,
        ),
        # The §18 verification query — "read the route split, never job success"
        # — as a plain GROUP BY rather than a JSONB unnest.
        Index("ix_nrb_recovery_units_route", "route"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    recovery_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("nrb_recoveries.id", ondelete="CASCADE"),
        nullable=False,
    )
    unit_number: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)

    route: Mapped[str] = mapped_column(String(24), nullable=False)
    # Which rule sent it here (`embedded_font`, `no_font_scan_backed`,
    # `conversion_unavailable`, …). Kept so a refresh can hand `convert_unit`
    # and `ocr_unit` the same reason a cold run would.
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    engine_version: Mapped[str] = mapped_column(String(255), nullable=False)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Post-`_withhold` text. Unbounded on purpose — see `NRBRecovery`.
    content: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    # Exception TYPE plus a short message. Never a stack trace, never a
    # filesystem path.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # `PageText.detail` verbatim: converter name/mapping/counts, or OCR
    # engine/model/version/`authoritative`. This is what `rag._chunk_meta` reads
    # to build citation provenance, so a reused unit cites identically to a
    # freshly recovered one.
    detail: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
