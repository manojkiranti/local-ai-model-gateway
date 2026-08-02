"""All /v1 tool endpoints must be behind auth. No Ollama/MCP needed: the auth
guard rejects the request before any upstream call."""

from starlette.testclient import TestClient

from app.main import app


def test_endpoints_require_auth():
    with TestClient(app) as client:
        assert client.get("/v1/tools").status_code in (401, 403)
        assert client.get("/v1/files/deadbeef").status_code in (401, 403)
        assert client.get("/v1/sessions").status_code in (401, 403)
        assert client.post(
            "/v1/chat", json={"message": "hi"}
        ).status_code in (401, 403)
