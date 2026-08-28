"""The chat client and the health badge must both mean the CHAT backend.

Once `AGENT_BASE_URL` moves the chat/agent model to vLLM, two things in
`app/main.py` still named "ollama" have to follow it: the shared client the
agent loop streams through, and the `/health` body an operator reads first.

Leaving either on `ollama_base_url` fails silently in the §11 way — the gateway
reports a healthy "ollama" that is really the embeddings server, while chat
either never moved or moved without anyone being able to see it.
"""

import asyncio

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


def test_health_reports_the_chat_backend_not_the_embeddings_one():
    settings = Settings(**BASE_ENV, **SPLIT)
    response = asyncio.run(main_module.health(_Request(settings, _StubOllama())))
    body = response.body.decode()
    assert "vllm-host:8100" in body
    assert "embeddings-host" not in body


def test_the_shared_chat_client_is_built_from_the_chat_backend(monkeypatch):
    """The client the agent loop streams through must point at vLLM."""
    monkeypatch.setattr(
        main_module, "get_settings", lambda: Settings(**BASE_ENV, **SPLIT)
    )
    with TestClient(main_module.app) as client:
        assert client.app.state.ollama.base_url == "http://vllm-host:8100"
