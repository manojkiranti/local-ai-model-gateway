"""`POST /auth/login` picks a credential store by `auth_provider` — never both.

Real Postgres + TestClient, with a FAKE directory substituted for
`app/auth/directory.py` so nothing touches the network. Skips if the DB is down,
matching tests/test_rag_departments_api.py.

The assertions that carry the security properties:

  - a `local` user's login never reaches the directory (the fake records calls);
  - an `ad` user's login is decided by the directory ALONE, and with AD switched
    off it fails CLOSED with 503 rather than falling back to a local password;
  - a directory that cannot be reached is 503, not 401;
  - a rejected credential creates no user row;
  - an UNAVAILABLE outcome does not consume a throttle attempt.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from starlette.testclient import TestClient

from app.auth import router as auth_router
from app.auth import throttle as throttle_mod
from app.auth.directory import DirectoryOutcome
from app.config import get_settings
from app.main import app

PASSWORD = "supersecret123"
SEEDED_ADMIN_EMAIL = "admin@example.com"
AD_BASE_URL = "http://ad.invalid/IzoneAuth/service.asmx"


# --------------------------------------------------------------------------
# Database helpers (fresh NullPool engine per call — the repo's idiom, because
# the app's module-level engine is bound to the first event loop)
# --------------------------------------------------------------------------

def _sql(fn):
    async def run():
        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                return await fn(conn)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _skip_if_no_db():
    try:
        _sql(lambda c: c.execute(text("SELECT 1")))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres unreachable: {type(exc).__name__}")


def _user_row(email):
    def get(conn):
        return conn.execute(
            text(
                "SELECT auth_provider, password_hash, role, is_active"
                " FROM users WHERE email = :e"
            ),
            {"e": email},
        )

    return _sql(get).mappings().one_or_none()


def _insert_ad_user(email, *, role="member", is_active=True):
    def make(conn):
        return conn.execute(
            text(
                "INSERT INTO users (email, auth_provider, password_hash, role, is_active)"
                " VALUES (:e, 'ad', NULL, :r, :a)"
            ),
            {"e": email, "r": role, "a": is_active},
        )

    _sql(make)


def _delete_users(emails):
    if not emails:
        return
    _sql(
        lambda c: c.execute(
            text("DELETE FROM users WHERE email = ANY(:e)"), {"e": list(emails)}
        )
    )


def _fresh(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"


# --------------------------------------------------------------------------
# Fakes and fixtures
# --------------------------------------------------------------------------

class FakeDirectory:
    """Stands in for app/auth/directory.py. Records every call it receives."""

    def __init__(self, outcome=DirectoryOutcome.AUTHENTICATED):
        self.outcome = outcome
        self.calls: list[tuple[str, str]] = []

    async def verify_credentials(self, username, password, *, transport=None):
        self.calls.append((username, password))
        return self.outcome


@pytest.fixture()
def created():
    """Emails to remove afterwards, so the dev database does not accumulate."""
    emails: set[str] = set()
    yield emails
    _delete_users(emails)


@pytest.fixture()
def ad(monkeypatch):
    """AD switched ON with a fake directory. Yields the fake."""
    _skip_if_no_db()
    monkeypatch.setenv("AD_AUTH_ENABLED", "true")
    monkeypatch.setenv("AD_AUTH_BASE_URL", AD_BASE_URL)
    monkeypatch.setenv("LOGIN_MAX_ATTEMPTS", "3")
    get_settings.cache_clear()
    throttle_mod.get_throttle.cache_clear()

    fake = FakeDirectory()
    monkeypatch.setattr(auth_router, "directory", fake)
    yield fake

    monkeypatch.undo()
    get_settings.cache_clear()
    throttle_mod.get_throttle.cache_clear()


@pytest.fixture()
def ad_off(monkeypatch):
    """AD switched OFF, with a fake that must never be consulted."""
    _skip_if_no_db()
    monkeypatch.setenv("AD_AUTH_ENABLED", "false")
    monkeypatch.setenv("AD_AUTH_BASE_URL", "")
    get_settings.cache_clear()
    throttle_mod.get_throttle.cache_clear()

    fake = FakeDirectory()
    monkeypatch.setattr(auth_router, "directory", fake)
    yield fake

    monkeypatch.undo()
    get_settings.cache_clear()
    throttle_mod.get_throttle.cache_clear()


def _admin_headers(client):
    resp = client.post(
        "/auth/login", json={"email": SEEDED_ADMIN_EMAIL, "password": PASSWORD}
    )
    if resp.status_code != 200:
        pytest.skip(f"seeded admin login failed ({resp.status_code})")
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    if client.get("/users/me", headers=headers).json().get("role") != "admin":
        pytest.skip(f"{SEEDED_ADMIN_EMAIL} is not an admin in this database")
    return headers


def _make_local_user(client, email):
    resp = client.post(
        "/auth/register",
        json={"email": email, "password": PASSWORD},
        headers=_admin_headers(client),
    )
    assert resp.status_code == 201, resp.text


def _login(client, email, password=PASSWORD):
    return client.post("/auth/login", json={"email": email, "password": password})


# --------------------------------------------------------------------------
# A local user is never sent to the directory
# --------------------------------------------------------------------------

def test_a_local_user_logs_in_without_consulting_the_directory(ad, created):
    email = _fresh("local-ok")
    with TestClient(app) as client:
        _make_local_user(client, email)
        created.add(email)

        resp = _login(client, email)

    assert resp.status_code == 200
    assert resp.json()["token_type"] == "bearer"
    assert ad.calls == [], "a local password must not be sent to Active Directory"


def test_a_wrong_local_password_is_401_and_still_never_reaches_the_directory(ad, created):
    email = _fresh("local-bad")
    with TestClient(app) as client:
        _make_local_user(client, email)
        created.add(email)

        resp = _login(client, email, "not-the-password")

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid email or password"
    assert ad.calls == []


# --------------------------------------------------------------------------
# An unknown identifier: ask the directory, provision on success
# --------------------------------------------------------------------------

def test_an_unknown_identifier_is_provisioned_on_a_directory_success(ad, created):
    email = _fresh("ad-new")
    ad.outcome = DirectoryOutcome.AUTHENTICATED

    with TestClient(app) as client:
        resp = _login(client, email)
    created.add(email)

    assert resp.status_code == 200
    assert ad.calls == [(email, PASSWORD)]

    row = _user_row(email)
    assert row is not None, "a successful directory login must create the user row"
    assert row["auth_provider"] == "ad"
    assert row["password_hash"] is None, "a directory user must hold no local password"
    assert row["role"] == "member", "provisioning must never mint an admin"
    assert row["is_active"] is True


def test_a_provisioned_user_sees_no_departments_until_granted(ad, created):
    """Auto-provisioning grants access to the assistant, not to any corpus."""
    email = _fresh("ad-nodept")
    with TestClient(app) as client:
        resp = _login(client, email)
        created.add(email)
        assert resp.status_code == 200
        headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
        depts = client.get("/v1/departments", headers=headers)

    assert depts.status_code == 200
    assert depts.json() == []


def test_a_rejected_unknown_identifier_creates_no_user(ad, created):
    email = _fresh("ad-reject")
    ad.outcome = DirectoryOutcome.REJECTED

    with TestClient(app) as client:
        resp = _login(client, email)
    created.add(email)

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid email or password"
    assert _user_row(email) is None


def test_an_unreachable_directory_is_503_not_401(ad, created):
    """The distinction the whole design turns on: an outage is not a bad password."""
    email = _fresh("ad-down")
    ad.outcome = DirectoryOutcome.UNAVAILABLE

    with TestClient(app) as client:
        resp = _login(client, email)
    created.add(email)

    assert resp.status_code == 503
    assert "not a password problem" in resp.json()["detail"]
    assert _user_row(email) is None


# --------------------------------------------------------------------------
# An existing directory user
# --------------------------------------------------------------------------

def test_an_existing_directory_user_is_decided_by_the_directory(ad, created):
    email = _fresh("ad-known")
    _insert_ad_user(email)
    created.add(email)
    ad.outcome = DirectoryOutcome.AUTHENTICATED

    with TestClient(app) as client:
        resp = _login(client, email)

    assert resp.status_code == 200
    assert ad.calls == [(email, PASSWORD)]


def test_an_existing_directory_user_rejected_is_401(ad, created):
    email = _fresh("ad-known-bad")
    _insert_ad_user(email)
    created.add(email)
    ad.outcome = DirectoryOutcome.REJECTED

    with TestClient(app) as client:
        resp = _login(client, email)

    assert resp.status_code == 401


def test_an_inactive_directory_user_is_403_after_the_credential_check(ad, created):
    """`is_active` is checked AFTER the credential, so a disabled account does not
    answer 403 to every password and thereby confirm it exists."""
    email = _fresh("ad-inactive")
    _insert_ad_user(email, is_active=False)
    created.add(email)
    ad.outcome = DirectoryOutcome.AUTHENTICATED

    with TestClient(app) as client:
        good = _login(client, email)
        ad.outcome = DirectoryOutcome.REJECTED
        bad = _login(client, email)

    assert good.status_code == 403
    assert bad.status_code == 401, "a wrong password must not reveal the account"


def test_an_admin_allowlisted_directory_user_is_provisioned_as_admin(
    ad, created, monkeypatch
):
    email = _fresh("ad-boss")
    monkeypatch.setenv("ADMIN_EMAILS", email)
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            resp = _login(client, email)
        created.add(email)
        assert resp.status_code == 200
        assert _user_row(email)["role"] == "admin"
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


# --------------------------------------------------------------------------
# AD switched off: fail closed, never fall back
# --------------------------------------------------------------------------

def test_with_ad_off_an_unknown_identifier_is_401_exactly_as_before(ad_off, created):
    email = _fresh("off-unknown")
    with TestClient(app) as client:
        resp = _login(client, email)
    created.add(email)

    assert resp.status_code == 401
    assert ad_off.calls == []
    assert _user_row(email) is None


def test_with_ad_off_an_existing_directory_user_fails_closed_with_503(ad_off, created):
    """NOT 401 (which would read as a wrong password) and emphatically NOT a
    fallback to the local hash — there isn't one, and there must never be."""
    email = _fresh("off-known")
    _insert_ad_user(email)
    created.add(email)

    with TestClient(app) as client:
        resp = _login(client, email)

    assert resp.status_code == 503
    assert ad_off.calls == []


def test_with_ad_off_a_local_user_is_unaffected(ad_off, created):
    email = _fresh("off-local")
    with TestClient(app) as client:
        _make_local_user(client, email)
        created.add(email)
        resp = _login(client, email)

    assert resp.status_code == 200
    assert ad_off.calls == []


# --------------------------------------------------------------------------
# Throttling
# --------------------------------------------------------------------------

def test_repeated_rejections_are_throttled(ad, created):
    """LOGIN_MAX_ATTEMPTS is 3 in this fixture."""
    email = _fresh("ad-throttle")
    ad.outcome = DirectoryOutcome.REJECTED

    with TestClient(app) as client:
        codes = [_login(client, email).status_code for _ in range(4)]
    created.add(email)

    assert codes[:3] == [401, 401, 401]
    assert codes[3] == 429


def test_the_throttle_reports_a_retry_after_header(ad, created):
    email = _fresh("ad-retry")
    ad.outcome = DirectoryOutcome.REJECTED

    with TestClient(app) as client:
        for _ in range(3):
            _login(client, email)
        resp = _login(client, email)
    created.add(email)

    assert resp.status_code == 429
    assert int(resp.headers["retry-after"]) >= 1


def test_an_outage_does_not_consume_throttle_attempts(ad, created):
    """Otherwise an AD outage locks every user out for the lockout period on top
    of the outage itself."""
    email = _fresh("ad-outage")
    ad.outcome = DirectoryOutcome.UNAVAILABLE

    with TestClient(app) as client:
        codes = [_login(client, email).status_code for _ in range(5)]
    created.add(email)

    assert codes == [503] * 5, "an unreachable directory must not count as an attempt"


def test_a_successful_login_clears_the_failure_count(ad, created):
    email = _fresh("ad-reset")
    with TestClient(app) as client:
        ad.outcome = DirectoryOutcome.REJECTED
        _login(client, email)
        _login(client, email)

        ad.outcome = DirectoryOutcome.AUTHENTICATED
        ok = _login(client, email)
        created.add(email)

        ad.outcome = DirectoryOutcome.REJECTED
        after = [_login(client, email).status_code for _ in range(2)]

    assert ok.status_code == 200
    assert after == [401, 401], "the counter should have restarted after the success"
