"""Chat endpoint must be behind auth. These need no Ollama: the auth guard
rejects the request before any upstream call."""

from starlette.testclient import TestClient

from app.main import app


def test_chat_requires_auth():
    with TestClient(app) as client:
        # no Authorization header
        r = client.post("/v1/chat", json={"messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code in (401, 403)

        # garbage bearer token
        r2 = client.post(
            "/v1/chat",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer not-a-real-jwt"},
        )
        assert r2.status_code == 401
