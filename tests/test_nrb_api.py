"""NRB operations API. Real Postgres + TestClient; skips if the DB is down.

Same shape as `test_rag_documents_api.py`: register/login through the real auth
router, take `admin@example.com` as the admin (the first registered user is one),
and skip rather than fail if the database or that assumption is unavailable.

WHAT IS STUBBED
    The three upstream stages, because they reach a central bank's website,
    download gigabytes and parse hundreds of documents — and because a `POST`
    stages synchronously, so an unstubbed `sync` would make the request take
    minutes. `app.nrb.pipeline` itself is NOT stubbed: the router's whole job is
    to call it, and these tests are about the HTTP contract over the real service
    (real advisory lock, real run rows, real active-run gate).

    The RAG stage is real but has nothing to select — the tests use a department
    with no catalog blobs in scope — so runs settle immediately and no ingest job
    is created. That keeps these tests about the API and leaves the lifecycle
    itself to `test_nrb_pipeline.py`.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from starlette.testclient import TestClient

from app.main import app

PASSWORD = "supersecret123"
# A scope that is bounded (so it passes the guard) and matches no catalog row,
# so the rag stage selects nothing and the run is terminal on return.
EMPTY_SCOPE_KEY = "https://www.nrb.org.np/api-test/nothing-here.pdf"


def _auth(client, email):
    err = resp = None
    try:
        client.post("/auth/register", json={"email": email, "password": PASSWORD})
        resp = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    except Exception as exc:  # noqa: BLE001
        err = exc
    if err is not None:
        pytest.skip(f"Postgres unreachable: {type(err).__name__}")
    if resp.status_code != 200:
        pytest.skip(f"auth failed (login -> {resp.status_code})")
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _me(client, headers):
    return client.get("/users/me", headers=headers).json()


@pytest.fixture()
def stub_stages(monkeypatch):
    """Replace sync/fetch/extract; record the order the API drove them in."""
    calls: list[str] = []

    class _Result:
        def __init__(self, name):
            self.counters = {f"{name}_ran": 1}

    def make(name):
        async def stage(**kwargs):
            calls.append(name)
            return _Result(name)
        return stage

    from app.nrb import extract as extract_mod
    from app.nrb import fetch as fetch_mod
    from app.nrb import sync as sync_mod

    monkeypatch.setattr(sync_mod, "run_sync", make("sync"))
    monkeypatch.setattr(fetch_mod, "run_fetch", make("fetch"))
    monkeypatch.setattr(extract_mod, "run_extract", make("extract"))
    return calls


@pytest.fixture()
def env():
    with TestClient(app) as client:
        admin = _auth(client, "admin@example.com")
        if _me(client, admin).get("role") != "admin":
            pytest.skip("admin@example.com is not an admin in this database")
        member = _auth(client, f"nrb-member-{uuid.uuid4().hex[:8]}@example.com")
        if _me(client, member).get("role") == "admin":
            pytest.skip("the second user unexpectedly has the admin role")
        code = f"nrbapi{uuid.uuid4().hex[:6]}"
        yield client, admin, member, code
        _cleanup(code)


def _cleanup(code: str) -> None:
    """Remove the throwaway department and the runs these tests opened.

    Committed rows, so they are removed rather than rolled back: a `TestClient`
    request runs through the app's own engine and cannot be wrapped in the
    caller's transaction. Runs are matched by department so the scratch
    database's real ones (from live exercises) are untouched.
    """
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    from app.config import get_settings

    async def go():
        engine = create_async_engine(get_settings().database_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("DELETE FROM nrb_pipeline_runs WHERE department = :c"),
                    {"c": code},
                )
                for statement in (
                    "DELETE FROM ingest_jobs WHERE document_id IN (SELECT d.id "
                    "  FROM documents d JOIN departments dp "
                    "    ON dp.id = d.department_id WHERE dp.code = :c)",
                    "DELETE FROM documents WHERE department_id IN "
                    "  (SELECT id FROM departments WHERE code = :c)",
                    "DELETE FROM departments WHERE code = :c",
                ):
                    await conn.execute(text(statement), {"c": code})
        finally:
            await engine.dispose()

    asyncio.run(go())


def _trigger(client, headers, code, **overrides):
    body = {
        "department": code,
        "stages": ["sync", "fetch", "extract", "rag"],
        "keys": [EMPTY_SCOPE_KEY],
    }
    body.update(overrides)
    return client.post("/v1/nrb/runs", json=body, headers=headers)


# --------------------------------------------------------------------------- #
# Authorization — the repository's existing pattern, asserted in both directions.
# --------------------------------------------------------------------------- #
def test_an_ordinary_member_cannot_trigger_an_nrb_update(env, stub_stages):
    """403 via `require_admin`, and — the point — nothing ran.

    Triggering ingestion rewrites what every user of a department can retrieve,
    so it is an admin operation. The stage recorder proves the refusal happened
    before any work, not after.
    """
    client, _admin, member, code = env
    resp = _trigger(client, member, code)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Admin privileges required"
    assert stub_stages == []


def test_an_anonymous_request_is_rejected(env):
    """401 from the shared `HTTPBearer`, before any handler or validation runs.

    Note the difference from the member case above, and that it is the existing
    convention rather than anything NRB-specific: no credentials is 401
    (`Depends(_bearer)`), valid credentials without the admin role is 403
    (`require_admin`). The malformed body on the POST is never even reached.
    """
    client, _admin, _member, _code = env
    assert client.post("/v1/nrb/runs", json={"keys": ["x"]}).status_code == 401
    assert client.get("/v1/nrb/status").status_code == 401
    assert client.get("/v1/nrb/runs/1").status_code == 401


def test_a_member_cannot_read_operational_status_either(env):
    client, _admin, member, _code = env
    assert client.get("/v1/nrb/status", headers=member).status_code == 403


# --------------------------------------------------------------------------- #
# The trigger.
# --------------------------------------------------------------------------- #
def test_an_admin_trigger_returns_202_with_the_run(env, stub_stages):
    client, admin, _member, code = env
    resp = _trigger(client, admin, code)
    assert resp.status_code == 202
    body = resp.json()
    assert body["started"] is True
    run = body["run"]
    # Recorded as an API run, attributed to the admin who asked.
    assert run["trigger"] == "api"
    assert run["requested_by"] == "admin@example.com"
    assert run["department"] == code
    assert run["id"] > 0
    # The stages really ran, in the pipeline's order, driven by the service.
    assert stub_stages == ["sync", "fetch", "extract"]
    assert run["counters"]["sync"] == {"sync_ran": 1}
    # Nothing was in scope, so there is nothing to wait for.
    assert run["status"] == "succeeded"
    assert run["jobs"] == {}
    assert run["scope"]["retry_failed"] is False
    assert run["finished_at"]


def test_retry_failed_is_carried_through_to_the_service(env, stub_stages):
    """The flag reaches `PipelineScope` and is recorded on the run.

    Its BEHAVIOUR is `test_nrb_pipeline.py`'s; what matters here is that the API
    can express it at all and that it defaults off, so a UI button that nobody
    ticked cannot start re-attempting permanently unparseable files.
    """
    client, admin, _member, code = env
    resp = _trigger(client, admin, code, retry_failed=True)
    assert resp.status_code == 202
    assert resp.json()["run"]["scope"]["retry_failed"] is True


def test_a_subset_of_stages_can_be_requested(env, stub_stages):
    client, admin, _member, code = env
    resp = _trigger(client, admin, code, stages=["rag"])
    assert resp.status_code == 202
    assert stub_stages == []                       # no upstream stage ran
    assert resp.json()["run"]["scope"]["stages"] == ["rag"]


# --------------------------------------------------------------------------- #
# The full-corpus guard.
# --------------------------------------------------------------------------- #
def test_an_unbounded_request_is_refused_before_anything_runs(env, stub_stages):
    """No scope, no run. The API has no `--all`.

    A full-corpus pass is 18,266 files and the `RAG_DOCS_DIR` duplication
    decision is still open, so it stays a considered decision at a terminal. 422
    because the request is malformed — the caller has to say which slice they
    mean — not 403, which would suggest a permission that could be granted.
    """
    client, admin, _member, code = env
    resp = client.post(
        "/v1/nrb/runs",
        json={"department": code, "stages": ["rag"]},
        headers=admin,
    )
    assert resp.status_code == 422
    assert "bounded scope is required" in str(resp.json())
    assert stub_stages == []


def test_all_files_cannot_be_smuggled_in_through_the_body(env, stub_stages):
    """`extra="forbid"`, so an unknown field is a 422 rather than being ignored.

    Silently dropping `all_files` would be worse than rejecting it: the caller
    would believe they had asked for a full-corpus run and get a bounded one, or
    vice versa once someone wired the field up.
    """
    client, admin, _member, code = env
    resp = client.post(
        "/v1/nrb/runs",
        json={"department": code, "stages": ["rag"], "limit": 1, "all_files": True},
        headers=admin,
    )
    assert resp.status_code == 422
    assert stub_stages == []


def test_the_rag_stage_requires_a_department(env, stub_stages):
    client, admin, _member, _code = env
    resp = client.post(
        "/v1/nrb/runs", json={"stages": ["rag"], "limit": 1}, headers=admin
    )
    assert resp.status_code == 422
    assert "needs a department" in str(resp.json())


def test_an_unknown_stage_is_refused(env, stub_stages):
    client, admin, _member, code = env
    resp = client.post(
        "/v1/nrb/runs",
        json={"department": code, "stages": ["reindex"], "limit": 1},
        headers=admin,
    )
    assert resp.status_code == 422
    assert "unknown stage" in str(resp.json())


# --------------------------------------------------------------------------- #
# PipelineBusy — 409 with the active run, not a 500.
# --------------------------------------------------------------------------- #
def test_a_busy_pipeline_returns_409_carrying_the_active_run(env, stub_stages,
                                                             monkeypatch):
    """Both exclusion mechanisms look identical to a client, and neither is a 500.

    Forced through the service's own exception so the router is tested rather
    than the lock: `pipeline.start` raises `PipelineBusy(run)` both when another
    orchestrator holds the advisory lock and when a durable run is still
    `running`/`awaiting_jobs`. The body is the SAME schema as a 202, with
    `started: false`, so a client parses one shape.
    """
    client, admin, _member, code = env
    first = _trigger(client, admin, code)
    assert first.status_code == 202
    active_id = first.json()["run"]["id"]

    from app.nrb import pipeline as pipeline_mod

    # Build the RunView the service would have handed back, from the real row.
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.config import get_settings

    async def load():
        engine = create_async_engine(get_settings().database_url)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as session:
                view = await pipeline_mod.get_run(session, active_id)
                await session.rollback()
            return view
        finally:
            await engine.dispose()

    _view = asyncio.run(load())
    _view.status = pipeline_mod.PIPELINE_AWAITING     # pretend it is still active

    async def raise_busy(scope, **kwargs):
        raise pipeline_mod.PipelineBusy(_view)

    monkeypatch.setattr(pipeline_mod, "start", raise_busy)
    resp = _trigger(client, admin, code)

    assert resp.status_code == 409
    body = resp.json()
    assert body["started"] is False
    assert body["run"]["id"] == active_id
    assert body["run"]["status"] == "awaiting_jobs"
    # Same schema as the 202 body — a client needs no second parser.
    assert set(body) == {"started", "run"}
    assert set(body["run"]) == set(first.json()["run"])


def test_a_second_trigger_is_refused_while_a_run_is_awaiting_jobs(
    env, stub_stages, monkeypatch
):
    """The durable gate, end to end through HTTP, with no lock held.

    The first request has returned, so its advisory lock is gone; only the
    `awaiting_jobs` row stands between the second trigger and duplicate work.
    A run is forced into that state by leaving a queued job associated with it.
    """
    client, admin, _member, code = env
    first = _trigger(client, admin, code, stages=["rag"])
    assert first.status_code == 202
    run_id = first.json()["run"]["id"]

    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    from app.config import get_settings
    from app.rag import documents as docs_repo

    async def make_it_wait():
        engine = create_async_engine(get_settings().database_url)
        try:
            async with engine.begin() as conn:
                dept = (
                    await conn.execute(
                        text("SELECT id FROM departments WHERE code = :c"),
                        {"c": code},
                    )
                ).scalar_one()
                doc_id = uuid.uuid4().hex
                await conn.execute(
                    text(
                        "INSERT INTO documents (id, department_id, title, source, "
                        "  file_type, content_hash, status) VALUES "
                        "(:i, :d, 'api test', 'upload', 'pdf', :h, 'pending')"
                    ),
                    {"i": doc_id, "d": dept,
                     "h": docs_repo.content_hash_of(doc_id.encode())},
                )
                job_id = uuid.uuid4().hex
                await conn.execute(
                    text(
                        "INSERT INTO ingest_jobs (id, document_id, status) "
                        "VALUES (:j, :i, 'queued')"
                    ),
                    {"j": job_id, "i": doc_id},
                )
                await conn.execute(
                    text(
                        "INSERT INTO nrb_pipeline_run_jobs (run_id, job_id, "
                        "  document_id, reason) VALUES (:r, :j, :i, 'created')"
                    ),
                    {"r": run_id, "j": job_id, "i": doc_id},
                )
                await conn.execute(
                    text(
                        "UPDATE nrb_pipeline_runs SET status = 'awaiting_jobs', "
                        "  stage = 'waiting', finished_at = NULL WHERE id = :r"
                    ),
                    {"r": run_id},
                )
        finally:
            await engine.dispose()

    asyncio.run(make_it_wait())
    stub_stages.clear()

    resp = _trigger(client, admin, code, stages=["rag"])
    assert resp.status_code == 409
    body = resp.json()
    assert body["started"] is False
    assert body["run"]["id"] == run_id
    assert body["run"]["status"] == "awaiting_jobs"
    assert stub_stages == []      # nothing ran


# --------------------------------------------------------------------------- #
# Reading runs.
# --------------------------------------------------------------------------- #
def test_reading_a_run_returns_the_durable_view(env, stub_stages):
    client, admin, _member, code = env
    run_id = _trigger(client, admin, code).json()["run"]["id"]

    resp = client.get(f"/v1/nrb/runs/{run_id}", headers=admin)
    assert resp.status_code == 200
    run = resp.json()
    assert run["id"] == run_id
    assert run["trigger"] == "api"
    assert run["status"] == "succeeded"
    assert set(run) == {
        "id", "trigger", "requested_by", "status", "stage", "department",
        "scope", "counters", "error", "jobs", "created_at", "started_at",
        "finished_at",
    }


def test_reading_a_terminal_run_twice_returns_the_same_thing(env, stub_stages):
    """Polling must be idempotent — a UI will do it on a timer.

    `finished_at` in particular: `reconcile` returns a terminal run untouched, so
    "how long did the update take" stays answerable (§24.2).
    """
    client, admin, _member, code = env
    run_id = _trigger(client, admin, code).json()["run"]["id"]
    first = client.get(f"/v1/nrb/runs/{run_id}", headers=admin).json()
    second = client.get(f"/v1/nrb/runs/{run_id}", headers=admin).json()
    assert first == second
    assert first["finished_at"] == second["finished_at"]


def test_an_unknown_run_is_a_404(env):
    client, admin, _member, _code = env
    resp = client.get("/v1/nrb/runs/999999999", headers=admin)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Unknown pipeline run"


# --------------------------------------------------------------------------- #
# Overall status.
# --------------------------------------------------------------------------- #
def test_overall_status_composes_the_existing_helpers(env, stub_stages):
    client, admin, _member, code = env
    run_id = _trigger(client, admin, code).json()["run"]["id"]

    resp = client.get("/v1/nrb/status", headers=admin)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"active_run", "latest_run", "catalog", "files", "rag"}

    # Nothing is active: the run settled on return.
    assert body["active_run"] is None
    assert body["latest_run"]["id"] >= run_id

    # Catalog and file blocks are the same numbers the CLI prints.
    assert {"sources", "files"} <= set(body["catalog"])
    assert {"pending", "fetched", "failed", "blocked", "distinct_blobs"} <= set(
        body["files"]
    )
    # RAG readiness, NRB-only.
    assert {"documents", "jobs", "ready", "failed", "superseded", "chunks"} <= set(
        body["rag"]
    )
    assert isinstance(body["rag"]["ready"], int)


def test_status_can_be_narrowed_to_one_department(env, stub_stages):
    """The `rag` block narrows; the catalog does not, because it is global."""
    client, admin, _member, code = env
    _trigger(client, admin, code)
    whole = client.get("/v1/nrb/status", headers=admin).json()
    scoped = client.get(f"/v1/nrb/status?department={code}", headers=admin).json()
    assert scoped["rag"]["ready"] <= whole["rag"]["ready"]
    assert scoped["catalog"] == whole["catalog"]


def test_status_reports_the_active_run_a_trigger_would_be_refused_for(
    env, stub_stages
):
    """`active_run` and the 409's run are the same run, which is the contract a
    UI leans on: it can grey out its own button from the status poll."""
    client, admin, _member, code = env
    run_id = _trigger(client, admin, code, stages=["rag"]).json()["run"]["id"]

    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    from app.config import get_settings

    async def make_it_running():
        engine = create_async_engine(get_settings().database_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE nrb_pipeline_runs SET status = 'running', "
                        "  stage = 'fetch', finished_at = NULL WHERE id = :r"
                    ),
                    {"r": run_id},
                )
        finally:
            await engine.dispose()

    asyncio.run(make_it_running())

    body = client.get("/v1/nrb/status", headers=admin).json()
    assert body["active_run"] is not None
    assert body["active_run"]["id"] == run_id
    assert body["active_run"]["status"] == "running"


# --------------------------------------------------------------------------- #
# The router stays thin.
# --------------------------------------------------------------------------- #
def test_the_router_contains_no_orchestration_and_no_subprocess():
    """Asserted on the source, because "thin" is a property that erodes quietly.

    The router may call `pipeline`, `catalog` and `corpus` services; it must not
    reimplement the sequence, take the lock itself, or shell out to a script.
    """
    import ast
    from pathlib import Path

    source = Path("app/nrb/router.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ) and ast.get_docstring(node) is not None:
            node.body = node.body[1:]
    code = ast.unparse(tree)

    for forbidden in (
        "subprocess", "advisory_lock", "run_sync", "run_fetch", "run_extract",
        "create_ingest_targets", "requeue_failed", "sweep_abandoned",
        "resolve_status", "scripts/",
    ):
        assert forbidden not in code, f"router should not reference {forbidden}"
