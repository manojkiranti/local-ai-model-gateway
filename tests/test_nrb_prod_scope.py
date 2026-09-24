"""The production corpus scope is a RULE, frozen into a file.

The previous attempt at this corpus defined its scope ("89 sources / 90 files /
~89 MB") and never wrote it down, so it could not be reproduced a week later.
`scripts/nrb_prod_scope.py` draws the scope from a rule and freezes the result
in the P7 cohort format (`scripts/nrb_p7_cohort.py`), which `nrb_pipeline.py
--cohort` already consumes. The rule is recorded verbatim in the file, so it can
be re-drawn against the FRESH production dump at cutover, which may carry
circulars published after this build.

These tests cover the pure half — no database.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import nrb_prod_scope as scope  # noqa: E402


def _row(key, doc_type="directive", published="2026-01-16", **kw):
    return {"comparison_key": key, "document_type": doc_type,
            "published_at": published, "owner": "bfr", "title": "t",
            "filename": key.rsplit("/", 1)[-1], "extension": "pdf", **kw}


def test_the_rule_admits_the_regulatory_core_regardless_of_date():
    for t in ("directive", "act", "rule_bylaw", "forex"):
        assert scope.admits(t, date(2008, 1, 1)), t


def test_circulars_are_admitted_only_from_the_cutoff():
    assert scope.admits("circular", scope.CIRCULAR_SINCE)
    assert not scope.admits("circular", date(2025, 7, 16))


def test_an_undated_circular_is_refused_rather_than_guessed():
    """No published date means the date rule cannot be decided. Admitting it
    would let an old circular in on a missing field; refusing costs one file,
    and it is reported in the counts, not dropped silently."""
    assert not scope.admits("circular", None)


def test_other_document_types_are_out():
    for t in ("notice", "statistics", "media", "career", "", None):
        assert not scope.admits(t, date(2026, 1, 1)), t


def test_the_circular_cutoff_is_the_date_that_was_approved():
    """Approved as "circulars since FY 2082/83 began (Jul 2025)". FY 2082/83
    runs Shrawan 1 = AD 2025-07-17 (CLAUDE.md, Bikram Sambat bullet). NOTE: that
    fiscal year ENDED 2026-07-16, so this covers the previous FY and the
    current one so far — broader than the "this FY" label it was offered
    under, and kept on the approved date, not the label."""
    assert scope.CIRCULAR_SINCE == date(2025, 7, 17)


def test_entries_are_ordered_by_key_so_the_file_is_reproducible():
    rows = [_row("https://x/b.pdf"), _row("https://x/a.pdf"), _row("https://x/c.pdf")]
    entries = scope.entries(rows)
    assert [e["comparison_key"] for e in entries] == [
        "https://x/a.pdf", "https://x/b.pdf", "https://x/c.pdf"]


def test_a_file_shared_by_two_sources_appears_once():
    """NRB really does attach one file to several posts (42 duplicate
    references were measured in Phase 4); the pipeline scopes on FILES."""
    rows = [_row("https://x/a.pdf"), _row("https://x/a.pdf", doc_type="act")]
    assert len(scope.entries(rows)) == 1


def test_the_fingerprint_is_the_p7_convention():
    """Same function shape as nrb_p7_cohort.cohort_sha256 — ordered
    [role, key] pairs — so the two frozen formats cannot drift apart."""
    import nrb_p7_cohort

    entries = scope.entries([_row("https://x/a.pdf"), _row("https://x/b.pdf")])
    assert scope.fingerprint(entries) == nrb_p7_cohort.cohort_sha256(entries)


def test_the_fingerprint_changes_when_the_scope_changes():
    a = scope.entries([_row("https://x/a.pdf")])
    b = scope.entries([_row("https://x/a.pdf"), _row("https://x/b.pdf")])
    assert scope.fingerprint(a) != scope.fingerprint(b)


def test_every_entry_carries_the_key_the_pipeline_reads():
    entry = scope.entries([_row("https://x/a.pdf")])[0]
    assert entry["comparison_key"] == "https://x/a.pdf"
    assert entry["role"] == "corpus"


def test_the_cutoff_is_read_in_nepal_time_not_utc():
    """A circular published at 00:30 on Shrawan 1 in Kathmandu is 18:45 the
    PREVIOUS day in UTC. Taking `.date()` of the UTC value would exclude it —
    the wrong side of the cutoff, silently, for anything published in the first
    5h45m of the day."""
    from datetime import datetime, timezone

    just_after_midnight_npt = datetime(2025, 7, 16, 18, 45, tzinfo=timezone.utc)
    assert just_after_midnight_npt.date() == date(2025, 7, 16)  # the trap
    published = scope._nepal_date(just_after_midnight_npt)
    assert published == date(2025, 7, 17)
    assert scope.admits("circular", published)
