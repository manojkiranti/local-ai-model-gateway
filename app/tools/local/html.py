"""Local tool: create_html (model-generated HTML document -> download/preview link)."""

from __future__ import annotations

from typing import Any

from ...files.store import HTML_MEDIA_TYPE, file_store
from .base import LocalToolSpec


async def _create_html(args: dict[str, Any]) -> str:
    """Write a model-generated HTML document to the store, return its download
    link. No server-side sanitizing — safe rendering is the frontend's job (it
    previews HTML files only inside a sandboxed <iframe srcdoc> with no scripts)."""
    html_content = args.get("html_content")
    if not isinstance(html_content, str) or not html_content:
        return "ERROR: 'html_content' is required and must be a non-empty HTML string."

    filename = str(args.get("filename") or "page.html")
    if not filename.lower().endswith(".html"):
        filename += ".html"

    record = await file_store.save(
        html_content.encode("utf-8"), filename=filename, media_type=HTML_MEDIA_TYPE
    )
    # Same string shape as create_excel so the frontend parses it identically.
    return (
        f"Created HTML file '{record.filename}' "
        f"({record.size} bytes). "
        f"Download it at: GET /v1/files/{record.id}"
    )


SPEC = LocalToolSpec(
    name="create_html",
    description=(
        "Create an HTML/CSS page from model-generated markup and return a "
        "download/preview link. Provide 'html_content' (a complete HTML "
        "document) and optionally 'filename'."
    ),
    parameters={
        "type": "object",
        "properties": {
            "html_content": {
                "type": "string",
                "description": "A complete HTML document (may include inline CSS).",
            },
            "filename": {
                "type": "string",
                "description": "Output file name, e.g. 'page.html' (default 'page.html').",
            },
        },
        "required": ["html_content"],
    },
    func=_create_html,
)
