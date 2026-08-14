"""NRB Phase 5 — byte sniffing, the blob store, and the downloader.

No network: every `httpx.AsyncClient` goes through a `MockTransport`, the same
pattern as `test_nrb_pages.py`. No database either — `fetch_one` takes a
`FetchTarget` and a directory, which is precisely why it is a separate function
from `run_fetch`. The DB-level pass is covered in
`tests/test_nrb_fetch_integration.py`.

The fixtures are shaped like the failures the live site can actually produce, since
that is what this code is for: a themed HTML page returned with a 200 for a missing
file, a truncated transfer whose `Content-Length` disagrees with the body, the same
PDF republished under a second URL, and an `http://uat.nrb.org.np/` link that must
never be fetched.
"""

from __future__ import annotations

import asyncio
import hashlib

import httpx
import pytest

from app.nrb import fetch as fetch_mod
from app.nrb import filestore, sniff
from app.nrb.catalog import FetchTarget
from app.nrb.models import FETCH_BLOCKED_HOST, FETCH_FAILED, FETCH_FETCHED

HOST = "https://www.nrb.org.np"
UPLOADS = f"{HOST}/contents/uploads/2026/08"

PDF_BODY = b"%PDF-1.7\n" + b"circular text " * 40 + b"\n%%EOF\n"
XLSX_BODY = b"PK\x03\x04" + b"\x00" * 26 + b"xl/workbook.xml" + b"\x00" * 200
# What WordPress actually serves for a missing file: 200 OK, themed HTML.
SOFT_404 = (
    b"<!DOCTYPE html>\n<html lang='en'><head><title>Page not found - Nepal Rastra "
    b"Bank</title></head><body><div class='main'>Nothing here</div></body></html>"
) + b"<!-- padding -->" * 500

_ORIGINAL_INIT = httpx.AsyncClient.__init__


def _init_with(handler):
    def patched_init(self, *a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        _ORIGINAL_INIT(self, *a, **kw)

    return patched_init


def target(url: str = f"{UPLOADS}/circular-15.pdf", **overrides) -> FetchTarget:
    defaults = dict(
        id=1,
        source_url=url,
        comparison_key=url,
        extension="pdf",
        resource_type="pdf",
        reported_mime_type="application/pdf",
        reported_bytes=len(PDF_BODY),
        fetch_attempts=0,
    )
    defaults.update(overrides)
    return FetchTarget(**defaults)


def run_fetch_one(monkeypatch, tmp_path, handler, fetch_target=None):
    """`fetch_one` against a mocked transport and a throwaway blob directory."""
    monkeypatch.setattr(httpx.AsyncClient, "__init__", _init_with(handler))

    async def go():
        client = httpx.AsyncClient()
        try:
            return await fetch_mod.fetch_one(
                client, fetch_target or target(), base=tmp_path
            )
        finally:
            await client.aclose()

    return asyncio.run(go())


def serve(body: bytes, *, status: int = 200, headers: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body, headers=headers or {})

    return handler


# --------------------------------------------------------------------------- #
# sniff — what the bytes say
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "body,expected",
    [
        (b"%PDF-1.4\ntrailer", "application/pdf"),
        (b"\x89PNG\r\n\x1a\n\x00\x00", "image/png"),
        (b"\xff\xd8\xff\xe0JFIF", "image/jpeg"),
        (b"GIF89a....", "image/gif"),
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "application/x-ole-storage"),
        (b"{\\rtf1\\ansi", "application/rtf"),
        (b"Rar!\x1a\x07\x00", "application/x-rar-compressed"),
        (b"II*\x00\x08\x00", "image/tiff"),
    ],
)
def test_signatures_are_recognised(body, expected):
    assert sniff.sniff(body)[0] == expected


@pytest.mark.parametrize(
    "member,expected",
    [
        (b"xl/workbook.xml",
         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        (b"word/document.xml",
         "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        (b"ppt/presentation.xml",
         "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    ],
)
def test_ooxml_flavour_is_read_from_the_zip_head(member, expected):
    assert sniff.sniff(b"PK\x03\x04" + b"\x00" * 20 + member)[0] == expected


def test_a_zip_whose_flavour_is_not_in_the_head_is_reported_as_a_zip():
    """Honest degradation: the central directory is at the END of the file, and we
    only ever look at 4 KB of the front."""
    mime, evidence = sniff.sniff(b"PK\x03\x04" + b"\x00" * 100)
    assert mime == "application/zip"
    assert "not in the head" in evidence


@pytest.mark.parametrize(
    "body",
    [
        b"<!DOCTYPE html><html>",
        b"\n\n  <html lang='en'>",
        b"\xef\xbb\xbf<!doctype HTML>",     # BOM first
        b"<head><title>404</title>",
        SOFT_404,
    ],
)
def test_html_is_recognised_however_it_starts(body):
    """The soft-404 shape. Every one of these must read as `web`."""
    mime, _ = sniff.sniff(body)
    assert sniff.family_for(mime) == "web"


def test_an_empty_body_is_not_mistaken_for_a_document():
    mime, evidence = sniff.sniff(b"")
    assert mime == "application/octet-stream"
    assert evidence == "empty body"
    assert not sniff.is_documentish(sniff.family_for(mime))


def test_plain_text_is_text_not_markup():
    assert sniff.sniff(b"currency,buy,sell\nUSD,141.2,141.8\n")[0] == "text/plain"


def test_binary_with_no_signature_is_octet_stream_not_a_guess():
    assert sniff.sniff(b"\x07\x08\x00\x01\x02rubbish")[0] == "application/octet-stream"


def test_truncated_utf8_at_the_head_cap_is_not_called_binary():
    """The head is a 4 KB slice, so a multi-byte character can be cut in half. That
    is an artefact of where we stopped reading, not evidence of a binary file."""
    body = ("नेपाल राष्ट्र बैंक " * 500).encode("utf-8")[:4096]
    assert sniff.sniff(body)[0] in ("text/plain", "text/html")


@pytest.mark.parametrize(
    "mime,family",
    [
        ("application/pdf", "pdf"),
        ("application/vnd.ms-excel", "office_legacy"),
        ("application/msword", "office_legacy"),
        ("image/svg+xml", "image"),
        ("text/html; charset=UTF-8", "web"),
        ("application/x-nonsense", "unknown"),
        (None, "unknown"),
    ],
)
def test_family_mapping(mime, family):
    assert sniff.family_for(mime) == family


def test_web_and_unknown_are_never_documentish():
    assert not sniff.is_documentish("web")
    assert not sniff.is_documentish("unknown")
    assert sniff.is_documentish("pdf")


# --------------------------------------------------------------------------- #
# filestore — content-addressed blobs
# --------------------------------------------------------------------------- #
def test_the_storage_key_is_the_hash_fanned_one_byte_deep():
    digest = hashlib.sha256(PDF_BODY).hexdigest()
    assert filestore.storage_key_for(digest, "pdf") == f"{digest[:2]}/{digest}.pdf"


def test_an_implausible_extension_becomes_bin_rather_than_being_trusted():
    digest = "a" * 64
    assert filestore.storage_key_for(digest, "../../etc/passwd").endswith(".bin")
    assert filestore.storage_key_for(digest, None).endswith(".bin")
    assert filestore.storage_key_for(digest, "").endswith(".bin")


def test_a_non_hash_key_is_refused():
    with pytest.raises(filestore.FileStoreError):
        filestore.storage_key_for("not-a-hash", "pdf")


def test_a_key_that_escapes_the_base_directory_is_refused(tmp_path):
    with pytest.raises(filestore.FileStoreError):
        filestore.resolve_path("../../etc/passwd", tmp_path)


def test_promote_reports_new_bytes_then_reports_a_duplicate(tmp_path):
    digest = hashlib.sha256(PDF_BODY).hexdigest()
    key = filestore.storage_key_for(digest, "pdf")

    first = filestore.new_temp_path(tmp_path)
    first.write_bytes(PDF_BODY)
    assert filestore.promote(first, key, tmp_path) is True
    assert filestore.resolve_path(key, tmp_path).read_bytes() == PDF_BODY

    second = filestore.new_temp_path(tmp_path)
    second.write_bytes(PDF_BODY)
    # Same bytes arriving under another URL: not stored twice, and not an error.
    assert filestore.promote(second, key, tmp_path) is False
    assert not second.exists()


def test_delete_is_compensation_and_does_not_raise_on_a_missing_blob(tmp_path):
    key = filestore.storage_key_for("b" * 64, "pdf")
    assert filestore.delete_blob(key, tmp_path) is False


# --------------------------------------------------------------------------- #
# fetch_one — the happy path
# --------------------------------------------------------------------------- #
def test_a_pdf_is_downloaded_hashed_and_stored(monkeypatch, tmp_path):
    outcome = run_fetch_one(monkeypatch, tmp_path, serve(PDF_BODY))
    assert outcome.status == FETCH_FETCHED
    assert outcome.sha256 == hashlib.sha256(PDF_BODY).hexdigest()
    assert outcome.length == len(PDF_BODY)
    assert outcome.sniffed_mime == "application/pdf"
    assert outcome.type_mismatch is None
    assert outcome.duplicate is False
    assert filestore.resolve_path(outcome.storage_key, tmp_path).read_bytes() == PDF_BODY


def test_the_same_bytes_under_a_second_url_are_not_stored_twice(monkeypatch, tmp_path):
    first = run_fetch_one(monkeypatch, tmp_path, serve(PDF_BODY))
    second = run_fetch_one(
        monkeypatch, tmp_path, serve(PDF_BODY),
        target(f"{UPLOADS}/circular-15-copy.pdf", id=2),
    )
    assert second.status == FETCH_FETCHED
    assert second.duplicate is True
    assert second.storage_key == first.storage_key
    blobs = [p for p in tmp_path.rglob("*") if p.is_file() and p.suffix != ".part"]
    assert len(blobs) == 1


def test_no_partial_file_is_left_behind_on_success(monkeypatch, tmp_path):
    run_fetch_one(monkeypatch, tmp_path, serve(PDF_BODY))
    assert list((tmp_path / filestore.INCOMING).glob("*.part")) == []


# --------------------------------------------------------------------------- #
# fetch_one — the failures that matter
# --------------------------------------------------------------------------- #
def test_html_where_a_pdf_was_promised_is_a_failure_and_nothing_is_stored(
    monkeypatch, tmp_path
):
    """THE case this module exists for. WordPress answers a missing file with a
    themed 200 page; storing it would give Phase 6 a navigation menu to index as a
    regulatory circular."""
    outcome = run_fetch_one(monkeypatch, tmp_path, serve(SOFT_404))
    assert outcome.status == FETCH_FAILED
    assert "soft 404" in outcome.error
    assert "HTML where pdf was promised" in outcome.error
    assert outcome.sniffed_mime == "text/html"
    assert outcome.storage_key is None
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


def test_a_truncated_transfer_is_a_failure(monkeypatch, tmp_path):
    """A short PDF still opens; its tail is simply missing. Worst kind of corruption
    for a corpus, so the declared length is checked against what arrived."""
    handler = serve(PDF_BODY, headers={"content-length": str(len(PDF_BODY) + 500)})
    outcome = run_fetch_one(monkeypatch, tmp_path, handler)
    assert outcome.status == FETCH_FAILED
    assert "truncated transfer" in outcome.error
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


def test_an_empty_body_is_a_failure(monkeypatch, tmp_path):
    outcome = run_fetch_one(monkeypatch, tmp_path, serve(b""))
    assert outcome.status == FETCH_FAILED
    assert outcome.error == "empty body"


def test_an_http_error_is_recorded_with_its_status(monkeypatch, tmp_path):
    outcome = run_fetch_one(monkeypatch, tmp_path, serve(b"nope", status=503))
    assert outcome.status == FETCH_FAILED
    assert outcome.http_status == 503
    assert "HTTP 503" in outcome.error


def test_a_redirect_is_refused_rather_than_followed(monkeypatch, tmp_path):
    handler = serve(b"", status=302, headers={"location": f"{UPLOADS}/elsewhere.pdf"})
    outcome = run_fetch_one(monkeypatch, tmp_path, handler)
    assert outcome.status == FETCH_FAILED
    assert "refused to follow a redirect" in outcome.error


def test_a_body_over_the_cap_is_abandoned_and_leaves_no_file(monkeypatch, tmp_path):
    monkeypatch.setattr(fetch_mod, "MAX_FILE_BYTES", 128)
    outcome = run_fetch_one(monkeypatch, tmp_path, serve(b"%PDF-" + b"x" * 4096))
    assert outcome.status == FETCH_FAILED
    assert "exceeded 128 bytes" in outcome.error
    # The cap path is the one that returns mid-stream with a partly written file.
    assert list((tmp_path / filestore.INCOMING).glob("*.part")) == []
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


def test_a_timeout_is_recorded_not_raised(monkeypatch, tmp_path):
    def handler(request):
        raise httpx.ReadTimeout("too slow", request=request)

    outcome = run_fetch_one(monkeypatch, tmp_path, handler)
    assert outcome.status == FETCH_FAILED
    assert outcome.error == "timed out"


def test_a_transport_error_is_recorded_not_raised(monkeypatch, tmp_path):
    def handler(request):
        raise httpx.ConnectError("no route", request=request)

    outcome = run_fetch_one(monkeypatch, tmp_path, handler)
    assert outcome.status == FETCH_FAILED
    assert "transport error" in outcome.error


# --------------------------------------------------------------------------- #
# fetch_one — the host guard, re-checked at the socket
# --------------------------------------------------------------------------- #
def test_the_uat_host_is_refused_at_fetch_time_too(monkeypatch, tmp_path):
    """The catalog already marks these `blocked_host`, so they cannot be selected —
    this is the second line of defence, at the code that opens the socket."""
    called: list[str] = []

    def handler(request):
        called.append(str(request.url))
        return httpx.Response(200, content=PDF_BODY)

    outcome = run_fetch_one(
        monkeypatch, tmp_path, handler,
        target("http://uat.nrb.org.np/wp-content/uploads/2019/12/r.pdf", id=9),
    )
    assert outcome.status == FETCH_BLOCKED_HOST
    assert outcome.error
    assert called == []          # no request was ever made


def test_plain_http_on_the_right_host_is_refused(monkeypatch, tmp_path):
    outcome = run_fetch_one(
        monkeypatch, tmp_path, serve(PDF_BODY),
        target("http://www.nrb.org.np/uploads/x.pdf", id=10),
    )
    assert outcome.status == FETCH_BLOCKED_HOST
    assert "http" in outcome.error


def test_an_off_host_url_is_refused(monkeypatch, tmp_path):
    outcome = run_fetch_one(
        monkeypatch, tmp_path, serve(PDF_BODY),
        target("https://evil.example/x.pdf", id=11),
    )
    assert outcome.status == FETCH_BLOCKED_HOST


# --------------------------------------------------------------------------- #
# fetch_one — type disagreements that are findings, not failures
# --------------------------------------------------------------------------- #
def test_a_non_fatal_type_disagreement_is_stored_and_recorded(monkeypatch, tmp_path):
    """NRB claims PDF, the bytes are a spreadsheet. Kept — Phase 6 decides what it
    can parse — but the disagreement is written down."""
    outcome = run_fetch_one(monkeypatch, tmp_path, serve(XLSX_BODY))
    assert outcome.status == FETCH_FETCHED
    assert outcome.type_mismatch and "bytes are spreadsheet" in outcome.type_mismatch
    assert outcome.storage_key


def test_an_unsniffable_body_is_kept_rather_than_thrown_away(monkeypatch, tmp_path):
    """`unknown` is not the soft-404 shape: rejecting it would lose real files whose
    type nothing at the front identifies (a latin-1 CSV, for instance)."""
    outcome = run_fetch_one(monkeypatch, tmp_path, serve(b"\x07\x08rubbish" * 20))
    assert outcome.status == FETCH_FETCHED
    assert outcome.sniffed_mime == "application/octet-stream"


def test_an_html_file_that_was_promised_as_html_is_allowed(monkeypatch, tmp_path):
    """The rejection is about a *mismatch*, not about HTML being forbidden."""
    outcome = run_fetch_one(
        monkeypatch, tmp_path, serve(SOFT_404),
        target(f"{UPLOADS}/page.html", id=12, extension="html",
               resource_type="web", reported_mime_type="text/html"),
    )
    assert outcome.status == FETCH_FETCHED


# --------------------------------------------------------------------------- #
# The row a result becomes
# --------------------------------------------------------------------------- #
def test_a_successful_row_carries_the_content_columns():
    outcome = fetch_mod.FetchOutcome(
        target(), FETCH_FETCHED, sha256="c" * 64, length=10,
        storage_key="cc/xx.pdf", sniffed_mime="application/pdf", http_status=200,
    )
    row = fetch_mod._row_for(outcome, run_id=5, now="NOW")
    assert row["fetch_status"] == FETCH_FETCHED
    assert row["content_sha256"] == "c" * 64
    assert row["downloaded_at"] == "NOW"
    assert row["fetch_attempts"] == 1
    assert row["last_fetch_run_id"] == 5


def test_a_failed_row_omits_the_content_columns_rather_than_nulling_them():
    """If bytes were ever downloaded for this file they are still on disk; a later
    failure must not erase the pointer to them."""
    outcome = fetch_mod.FetchOutcome(target(), FETCH_FAILED, error="HTTP 500")
    row = fetch_mod._row_for(outcome, run_id=5, now="NOW")
    assert row["fetch_status"] == FETCH_FAILED
    assert row["fetch_error"] == "HTTP 500"
    for column in ("content_sha256", "content_length", "storage_key", "downloaded_at"):
        assert column not in row


def test_a_blocked_row_records_the_guards_reason():
    outcome = fetch_mod.FetchOutcome(target(), FETCH_BLOCKED_HOST, error="host x is not y")
    row = fetch_mod._row_for(outcome, run_id=5, now="NOW")
    assert row["blocked_reason"] == "host x is not y"


def test_the_attempt_counter_advances_from_whatever_the_row_had():
    outcome = fetch_mod.FetchOutcome(target(fetch_attempts=3), FETCH_FAILED, error="x")
    assert fetch_mod._row_for(outcome, run_id=1, now="NOW")["fetch_attempts"] == 4
