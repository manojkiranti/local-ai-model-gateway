"""The AD shim adapter: every body it can return, mapped to a three-state outcome.

Two properties matter more than the mapping itself and are asserted here:

1. **A malfunction is not a rejection.** The shim answers only "Success" or
   "Failed", so an empty body, an IIS error page or a SOAP fault means the
   service changed or broke — NOT that the password was wrong. Collapsing those
   into `REJECTED` would tell an entire office its passwords had stopped working
   during an outage.
2. **The password never reaches a log.** The shim takes credentials in the query
   STRING, so the URL is a secret. `httpx` exceptions render the URL in their
   string form, which makes `f"failed: {exc}"` a credential leak.

No network and no database: every case runs on an `httpx.MockTransport`. There is
no pytest-asyncio in this repo, so each test drives its own loop via `_run`.
"""

import asyncio
import logging

import httpx
import pytest

from app.auth.directory import (
    AD_PATH,
    DirectoryOutcome,
    _read_verdict,
    verify_credentials,
)
from app.config import get_settings

BASE_URL = "http://ad.invalid/IzoneAuth/service.asmx"
SECRET = "Sup3rS3cret-DoNotLog"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def ad_enabled(monkeypatch):
    """Point the adapter at a fake shim, and restore the settings cache after."""
    monkeypatch.setenv("AD_AUTH_ENABLED", "true")
    monkeypatch.setenv("AD_AUTH_BASE_URL", BASE_URL)
    get_settings.cache_clear()
    yield
    monkeypatch.undo()
    get_settings.cache_clear()


def _responder(status_code=200, text="Success"):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text=text)

    return httpx.MockTransport(handler)


def _raiser(exc):
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return httpx.MockTransport(handler)


def _recorder(seen, status_code=200, text="Success"):
    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = request.url
        seen["method"] = request.method
        return httpx.Response(status_code, text=text)

    return httpx.MockTransport(handler)


def _verify(password="pw", *, transport, username="user@example.com"):
    return _run(verify_credentials(username, password, transport=transport))


# --------------------------------------------------------------------------
# The verdict parser, in isolation. This is the labelled eval set.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "body",
    [
        "Success",
        "success",
        "  Success \n",
        "<string>Success</string>",
        '<?xml version="1.0" encoding="utf-8"?>\n<string>Success</string>',
        "<string> success </string>",
    ],
)
def test_success_bodies_authenticate(body):
    assert _read_verdict(body) is DirectoryOutcome.AUTHENTICATED


@pytest.mark.parametrize(
    "body",
    [
        "Failed",
        "failed",
        "  Failed\r\n",
        "<string>Failed</string>",
        '<?xml version="1.0" encoding="utf-8"?><string>Failed</string>',
    ],
)
def test_failed_bodies_reject(body):
    assert _read_verdict(body) is DirectoryOutcome.REJECTED


@pytest.mark.parametrize(
    "body",
    [
        "",
        "   ",
        "<html><body>Server Error in '/IzoneAuth' Application.</body></html>",
        "<soap:Fault><faultstring>boom</faultstring></soap:Fault>",
        "<string></string>",
        "Succeeded",  # near miss: not the documented vocabulary
        "OK",
        "True",
    ],
)
def test_unrecognised_bodies_are_a_malfunction_not_a_rejection(body):
    """The whole point: only "Failed" is a rejection. Anything else is broken."""
    assert _read_verdict(body) is None


# --------------------------------------------------------------------------
# The call itself
# --------------------------------------------------------------------------

def test_success_over_the_wire(ad_enabled):
    assert _verify(transport=_responder(text="Success")) is DirectoryOutcome.AUTHENTICATED


def test_success_over_the_wire_xml_wrapped(ad_enabled):
    transport = _responder(text="<string>Success</string>")
    assert _verify(transport=transport) is DirectoryOutcome.AUTHENTICATED


def test_failed_over_the_wire(ad_enabled):
    assert _verify(transport=_responder(text="Failed")) is DirectoryOutcome.REJECTED


@pytest.mark.parametrize("status_code", [301, 302, 400, 401, 404, 500, 502, 503])
def test_non_200_is_unavailable(ad_enabled, status_code):
    """Even a 500 whose body says "Failed" is a malfunction, not a rejection.

    Redirects are in this list on purpose: `follow_redirects=False`, because a
    3xx is an unexpected place to send a credential.
    """
    transport = _responder(status_code=status_code, text="Failed")
    assert _verify(transport=transport) is DirectoryOutcome.UNAVAILABLE


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("refused"),
        httpx.ConnectTimeout("timed out"),
        httpx.ReadTimeout("timed out"),
        httpx.RemoteProtocolError("bad framing"),
    ],
)
def test_transport_failures_are_unavailable(ad_enabled, exc):
    assert _verify(transport=_raiser(exc)) is DirectoryOutcome.UNAVAILABLE


def test_unconfigured_base_url_is_unavailable(monkeypatch):
    """A direct call with AD off must not fall through to an authentication."""
    monkeypatch.setenv("AD_AUTH_ENABLED", "false")
    monkeypatch.setenv("AD_AUTH_BASE_URL", "")
    get_settings.cache_clear()
    try:
        transport = _responder(text="Success")
        assert _verify(transport=transport) is DirectoryOutcome.UNAVAILABLE
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


# --------------------------------------------------------------------------
# Request shape: the hardcoded path, and encoding of awkward passwords
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "password",
    [
        "plain",
        "has&ampersand",
        "has=equals",
        "has spaces and +plus",
        "unicode-नेपाल",
        "sym#?%/\\@:;",
    ],
)
def test_credentials_survive_url_encoding(ad_enabled, password):
    """A password containing & or = must not split the query string."""
    seen: dict = {}

    outcome = _verify(password, transport=_recorder(seen))

    assert outcome is DirectoryOutcome.AUTHENTICATED
    url = seen["url"]
    assert seen["method"] == "GET"
    assert url.path.endswith(AD_PATH)
    assert url.params["Username"] == "user@example.com"
    assert url.params["Password"] == password


def test_the_method_path_is_not_caller_supplied(ad_enabled):
    """The host is config; the method name is ours. Nothing routes it elsewhere."""
    seen: dict = {}

    _verify(transport=_recorder(seen), username="../../evil?x=")

    url = seen["url"]
    assert url.host == "ad.invalid"
    assert url.path == "/IzoneAuth/service.asmx" + AD_PATH


# --------------------------------------------------------------------------
# The credential-leak control
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "make_transport",
    [
        lambda: _responder(status_code=500, text="boom"),
        lambda: _responder(status_code=200, text="<html>Server Error</html>"),
        lambda: _responder(status_code=200, text=""),
        # A shim that echoed the query back would put the password in the
        # BODY, which is why the body is never logged either.
        lambda: _responder(status_code=200, text=f"Failed for pw={SECRET}"),
        lambda: _responder(status_code=500, text=f"error: Password={SECRET}"),
        lambda: _raiser(httpx.ConnectError("refused")),
        lambda: _raiser(httpx.ReadTimeout("timed out")),
        lambda: _responder(status_code=200, text="Failed"),
        lambda: _responder(status_code=200, text="Success"),
    ],
)
def test_the_password_never_reaches_a_log(ad_enabled, caplog, make_transport):
    """Every outcome path, at the most verbose level any of them can emit."""
    caplog.set_level(logging.DEBUG)

    _verify(SECRET, transport=make_transport())

    assert SECRET not in caplog.text
    for record in caplog.records:
        assert SECRET not in record.getMessage()
        assert SECRET not in str(record.args or "")


def test_the_password_is_absent_from_raised_exceptions(ad_enabled):
    """The adapter returns outcomes; it must not raise a URL-bearing httpx error."""
    try:
        outcome = _verify(SECRET, transport=_raiser(httpx.ConnectError("refused")))
    except Exception as exc:  # pragma: no cover - the point is this is unreached
        pytest.fail(f"adapter raised {type(exc).__name__} instead of returning")
    assert outcome is DirectoryOutcome.UNAVAILABLE


def test_the_httpx_logger_pinning_is_load_bearing(ad_enabled):
    """`directory.py` pins httpx/httpcore at WARNING. That is a control, not noise
    reduction, and this test is the proof: on the SUCCESS path the request
    completes, and httpx's own DEBUG line renders the full URL — which for this
    one endpoint contains the user's password.

    Measured with the pinning removed:
        HTTP Request: GET http://.../AD_Authentication?Username=...&Password=<secret>

    If someone deletes the pinning as tidy-up, this fails and says why.
    """
    import io

    transport = _responder(text="Success")

    def capture(httpx_level):
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setLevel(logging.DEBUG)
        root = logging.getLogger()
        root.addHandler(handler)
        previous_root = root.level
        root.setLevel(logging.DEBUG)
        for name in ("httpx", "httpcore"):
            logging.getLogger(name).setLevel(httpx_level)
        try:
            _verify(SECRET, transport=transport)
        finally:
            root.removeHandler(handler)
            root.setLevel(previous_root)
        return buf.getvalue()

    try:
        assert SECRET not in capture(logging.WARNING), (
            "the shipped configuration leaked the password into a log"
        )
        # Sanity-check the control by removing it: if this does NOT leak, either
        # httpx stopped logging URLs or this test has stopped proving anything.
        assert SECRET in capture(logging.NOTSET), (
            "httpx no longer logs the request URL at DEBUG, so this test no "
            "longer demonstrates why the pinning exists — re-verify the control"
        )
    finally:
        for name in ("httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.WARNING)
