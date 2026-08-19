"""The ONLY file that knows the Active Directory HTTP shim exists.

The shim is an ASMX method in front of AD:

    GET {AD_AUTH_BASE_URL}/AD_Authentication?Username=...&Password=...
    -> body is "Success" or "Failed"

Like `app/nrb/client.py`, **the host is application config and the method path is
ours** — `AD_PATH` is hardcoded, the identifier travels as a query PARAMETER, and
no caller can point this at another URL.

Three things about this shim shape the module, and each one is load-bearing:

1. **It returns authentication and nothing else.** No email, no display name, no
   group membership, no DN. So it cannot inform authorization: `role` and
   department grants live in our Postgres and are granted by an admin.

2. **A malfunction is not a rejection.** "Failed" is one bucket for wrong
   password, disabled, locked and unknown user — a real verdict. An empty body,
   an IIS error page, a SOAP fault, a redirect or a connection refusal are a
   different fact entirely, and the outcome vocabulary keeps them apart so the
   caller can answer 503 instead of 401. Rendering an outage as "invalid
   credentials" sends a whole office to reset passwords that were never wrong.
   Hence `UNAVAILABLE` exists and only a body that positively parses to "Failed"
   is `REJECTED`; anything unrecognised is a malfunction. A service change then
   breaks loudly rather than silently locking everyone out.

3. **The password is in the query string, so the URL is a secret.** That makes
   ordinary error handling a credential leak: `httpx.HTTPStatusError.__str__`
   embeds the full URL, so `f"AD call failed: {exc}"` would write the password to
   the log — as would the `httpx` logger at DEBUG. The rules below are controls,
   not tidiness:
     - never interpolate the URL, the params, the caught exception object, or the
       RESPONSE BODY into a log line or an exception message;
     - log `type(exc).__name__`, the status code, or a byte count instead;
     - the `httpx`/`httpcore` loggers are pinned at WARNING at import.
   `tests/test_ad_directory_client.py` asserts the password appears in no log
   record on any outcome path.

It also never raises: every failure is an outcome, because the caller's job is to
choose an HTTP status, not to handle a transport exception.
"""

import logging
import re
from enum import Enum

import httpx

from ..config import get_settings

logger = logging.getLogger("app.auth.directory")

# CREDENTIAL CONTROL, not noise reduction. At DEBUG these loggers write the
# request URL verbatim, and for this one endpoint the URL contains the user's
# password. Nothing in this app needs httpx wire logging; a leaked credential in
# a log file cannot be un-leaked.
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# The method name is ours, never a caller's. Only the base URL is configurable.
AD_PATH = "/AD_Authentication"

# The real answer is one word. Anything approaching this size is an error page,
# and it is a malfunction whatever it says.
MAX_RESPONSE_BYTES = 64 * 1024

_TAG = re.compile(r"<[^>]*>")


class DirectoryOutcome(str, Enum):
    """Three states, deliberately. Collapsing the last two IS the 401-vs-503 bug."""

    AUTHENTICATED = "authenticated"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"


def _read_verdict(body: str) -> DirectoryOutcome | None:
    """Map a response body to a verdict, or `None` if it is not one we recognise.

    Tolerant about the envelope because an ASMX method usually answers
    `<?xml ...?><string>Success</string>` rather than the bare word the vendor
    documentation shows — but strict about the vocabulary, because "probably a
    rejection" must never be treated as one.
    """
    text = _TAG.sub("", body).strip().casefold()
    if text == "success":
        return DirectoryOutcome.AUTHENTICATED
    if text == "failed":
        return DirectoryOutcome.REJECTED
    return None


async def verify_credentials(
    username: str,
    password: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> DirectoryOutcome:
    """Ask AD whether this credential is valid. Never raises.

    `transport` is a test seam (`httpx.MockTransport`); production passes nothing.
    """
    settings = get_settings()
    base_url = settings.ad_auth_base_url.strip().rstrip("/")
    if not base_url:
        # Reachable only by a direct call with AD switched off. Fail closed: a
        # missing configuration must never read as an authentication.
        logger.warning("AD auth requested for %s but AD_AUTH_BASE_URL is unset", username)
        return DirectoryOutcome.UNAVAILABLE

    timeout = httpx.Timeout(
        settings.ad_auth_read_timeout, connect=settings.ad_auth_connect_timeout
    )

    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            # A redirect would be an unexpected place to send a credential, and
            # the shim has no reason to issue one. 3xx becomes UNAVAILABLE below.
            follow_redirects=False,
        ) as client:
            response = await client.get(
                base_url + AD_PATH,
                # httpx does the percent-encoding, so a password containing "&"
                # or "=" cannot split the query string.
                params={"Username": username, "Password": password},
                headers={"Accept": "text/xml, text/plain, */*"},
            )
    except httpx.HTTPError as exc:
        # `exc` itself is NOT logged: its string form embeds the URL, i.e. the
        # password. The class name is the diagnostic.
        logger.warning(
            "AD auth unavailable for %s: %s", username, type(exc).__name__
        )
        return DirectoryOutcome.UNAVAILABLE

    if response.status_code != httpx.codes.OK:
        logger.warning(
            "AD auth unavailable for %s: shim answered HTTP %d",
            username,
            response.status_code,
        )
        return DirectoryOutcome.UNAVAILABLE

    body = response.content
    if len(body) > MAX_RESPONSE_BYTES:
        logger.warning(
            "AD auth unavailable for %s: shim answered %d bytes", username, len(body)
        )
        return DirectoryOutcome.UNAVAILABLE

    verdict = _read_verdict(response.text)
    if verdict is None:
        # The BODY is never logged — a shim that echoed the query back would put
        # the password in it. Its size is enough to diagnose "we got an HTML
        # error page instead of a word".
        logger.warning(
            "AD auth unavailable for %s: unrecognised %d-byte response body "
            "(expected 'Success' or 'Failed' — has the shim changed?)",
            username,
            len(body),
        )
        return DirectoryOutcome.UNAVAILABLE

    logger.info("AD auth %s for %s", verdict.value, username)
    return verdict
