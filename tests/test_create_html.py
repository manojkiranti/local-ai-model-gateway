"""create_html local tool (offline): dispatch through the registry and store an
HTML doc via the in-memory fallback sink (no user/turn context, no Postgres).

Owner-scoped SERVING via GET /v1/files/{id} is exercised end-to-end (real user +
Postgres sink) in tests/test_files_integration.py."""

import asyncio
import re

from app.files.store import SVG_MEDIA_TYPE, file_store  # noqa: F401 (media consts live here)
from app.tools.registry import ToolRegistry


def _dispatch(name, args, tmp_path):
    """Run a tool through the registry against a temp-configured fallback store."""
    file_store.configure(str(tmp_path))
    registry = ToolRegistry()
    registry.register_local_tools()
    return asyncio.run(registry.dispatch(name, args))


def test_create_html_stores_exact_bytes(tmp_path):
    html = "<!doctype html><html><body><h1>Hi &amp; bye</h1></body></html>"
    result = _dispatch("create_html", {"html_content": html, "filename": "hello"}, tmp_path)

    # Same string shape as create_excel -> frontend parses it identically.
    m = re.search(r"Download it at: GET /v1/files/([0-9a-f]+)", result)
    assert m, f"no file link in tool result: {result!r}"

    record = file_store.get(m.group(1))
    assert record is not None
    assert record.filename == "hello.html"  # .html appended
    assert record.media_type.startswith("text/html")
    assert open(record.path, "rb").read() == html.encode("utf-8")


def test_create_html_requires_content(tmp_path):
    result = _dispatch("create_html", {}, tmp_path)
    assert result.startswith("ERROR")
