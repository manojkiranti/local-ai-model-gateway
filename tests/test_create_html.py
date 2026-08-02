"""create_html local tool: dispatch through the registry, store an HTML doc,
and serve it (authed) via GET /v1/files/{id}. No Ollama/Postgres needed —
the file store is in-memory and auth is overridden."""

import re

import pytest
from starlette.testclient import TestClient

from app.auth.dependencies import get_current_user
from app.files.store import file_store
from app.main import app
from app.tools.registry import ToolRegistry


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_create_html_stores_and_serves_exact_bytes():
    html = "<!doctype html><html><body><h1>Hi &amp; bye</h1></body></html>"

    # Lifespan configures the file store (files_dir). Override auth so the
    # protected download route doesn't need a real JWT/Postgres user.
    with TestClient(app) as client:
        app.dependency_overrides[get_current_user] = lambda: object()
        try:
            registry = ToolRegistry()
            registry.register_local_tools()

            result = await registry.dispatch(
                "create_html", {"html_content": html, "filename": "hello"}
            )

            # Same string shape as create_excel -> frontend parses it identically.
            m = re.search(r"Download it at: GET /v1/files/([0-9a-f]+)", result)
            assert m, f"no file link in tool result: {result!r}"
            file_id = m.group(1)

            record = file_store.get(file_id)
            assert record is not None
            assert record.filename == "hello.html"  # .html appended
            assert record.media_type.startswith("text/html")

            resp = client.get(
                f"/v1/files/{file_id}", headers={"Authorization": "Bearer x"}
            )
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/html")
            assert resp.headers["x-content-type-options"] == "nosniff"
            assert resp.content == html.encode("utf-8")
        finally:
            app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_create_html_requires_content():
    registry = ToolRegistry()
    registry.register_local_tools()
    result = await registry.dispatch("create_html", {})
    assert result.startswith("ERROR")
