"""The chat client and the health badge must both mean the CHAT backend.

Once `AGENT_BASE_URL` moves the chat/agent model to vLLM, two things in
`app/main.py` still named "ollama" have to follow it: the shared client the
agent loop streams through, and the `/health` body an operator reads first.

Leaving either on `ollama_base_url` fails silently in the §11 way — the gateway
reports a healthy "ollama" that is really the embeddings server, while chat
either never moved or moved without anyone being able to see it.
"""

import asyncio
import json

from starlette.testclient import TestClient

import app.main as main_module
from app.config import Settings

BASE_ENV = {
    "database_url": "postgresql+asyncpg://u:p@127.0.0.1:5432/db",
    "jwt_secret": "test-secret",
}

SPLIT = dict(
    ollama_base_url="http://embeddings-host:11434",
    agent_base_url="http://vllm-host:8100",
)


class _StubOllama:
    """Stands in for the chat client; /health only asks it one question."""

    def __init__(self, healthy: bool = True) -> None:
        self.base_url = "http://vllm-host:8100"
        self._healthy = healthy

    async def is_healthy(self) -> bool:
        return self._healthy


class _Request:
    def __init__(self, settings, ollama) -> None:
        state = type("S", (), {"settings": settings, "ollama": ollama})
        self.app = type("A", (), {"state": state})


def _fake_embeddings_client(healthy: bool, calls: list):
    """A stand-in for `main_module.OllamaClient`, constructed fresh by
    `/health` to probe the embeddings backend. Records the base_url it was
    built with and whether `aclose()` ran, so a test can prove no client is
    leaked.
    """

    class _FakeClient:
        def __init__(self, base_url: str, timeout: float) -> None:
            self.base_url = base_url
            calls.append({"base_url": base_url, "closed": False})
            self._record = calls[-1]

        async def is_healthy(self) -> bool:
            return healthy

        async def aclose(self) -> None:
            self._record["closed"] = True

    return _FakeClient


def test_health_reports_the_chat_backend_not_the_embeddings_one(monkeypatch):
    """The `ollama` key must stay the CHAT backend — the embeddings host
    belongs only in the separate `embeddings` key added for finding #2.
    """
    calls: list = []
    monkeypatch.setattr(
        main_module, "OllamaClient", _fake_embeddings_client(True, calls)
    )
    settings = Settings(**BASE_ENV, **SPLIT)
    response = asyncio.run(main_module.health(_Request(settings, _StubOllama())))
    body = json.loads(response.body.decode())
    assert body["ollama"]["base_url"] == "http://vllm-host:8100"
    assert "embeddings-host" not in body["ollama"]["base_url"]


def test_the_shared_chat_client_is_built_from_the_chat_backend(monkeypatch):
    """The client the agent loop streams through must point at vLLM."""
    monkeypatch.setattr(
        main_module, "get_settings", lambda: Settings(**BASE_ENV, **SPLIT)
    )
    with TestClient(main_module.app) as client:
        assert client.app.state.ollama.base_url == "http://vllm-host:8100"


def test_health_reports_both_backends_when_split_and_probes_only_once_each(
    monkeypatch,
):
    """Finding #2: split URLs must both show up, and only one fresh client is
    built (for embeddings) — the chat client is reused, not re-probed.
    """
    calls: list = []
    monkeypatch.setattr(
        main_module, "OllamaClient", _fake_embeddings_client(True, calls)
    )
    settings = Settings(**BASE_ENV, **SPLIT)
    response = asyncio.run(main_module.health(_Request(settings, _StubOllama())))
    body = json.loads(response.body.decode())

    assert response.status_code == 200
    assert body["status"] == "ok"
    assert body["ollama"] == {"base_url": "http://vllm-host:8100", "reachable": True}
    assert body["embeddings"] == {
        "base_url": "http://embeddings-host:11434",
        "reachable": True,
    }
    # Exactly one embeddings client was constructed, and it was closed again —
    # a leaked httpx.AsyncClient per health check would be a slow, silent leak.
    assert calls == [{"base_url": "http://embeddings-host:11434", "closed": True}]


def test_health_surfaces_a_dead_embeddings_backend_without_failing_the_probe(
    monkeypatch,
):
    """A healthy chat backend must not hide a dead embeddings backend.

    Before this fix `/health` asked only the chat backend, so vLLM up +
    Ollama down (department RAG dead) reported 200 "ok" with no signal at
    all. The HTTP status/overall liveness still tracks chat alone — restarting
    the gateway container cannot fix an external Ollama outage — but the body
    must say so explicitly for any monitor that parses it.
    """
    calls: list = []
    monkeypatch.setattr(
        main_module, "OllamaClient", _fake_embeddings_client(False, calls)
    )
    settings = Settings(**BASE_ENV, **SPLIT)
    response = asyncio.run(
        main_module.health(_Request(settings, _StubOllama(healthy=True)))
    )
    body = json.loads(response.body.decode())

    assert response.status_code == 200
    assert body["ollama"]["reachable"] is True
    assert body["embeddings"]["reachable"] is False
    assert body["status"] != "ok"


def test_health_does_not_double_probe_when_both_urls_are_the_same(monkeypatch):
    """The default, unset `AGENT_BASE_URL`: one server, one probe."""
    calls: list = []
    monkeypatch.setattr(
        main_module, "OllamaClient", _fake_embeddings_client(True, calls)
    )
    settings = Settings(**BASE_ENV)  # no split — both URLs equal
    response = asyncio.run(
        main_module.health(_Request(settings, _StubOllama(healthy=True)))
    )
    body = json.loads(response.body.decode())

    assert calls == []  # no fresh embeddings client built at all
    assert body["ollama"]["base_url"] == body["embeddings"]["base_url"]
    assert body["embeddings"]["reachable"] is True
