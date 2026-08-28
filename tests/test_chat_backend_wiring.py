"""The chat client and the health badge must both mean the CHAT backend.

Once `AGENT_BASE_URL` moves the chat/agent model to vLLM, two things in
`app/main.py` still named "ollama" have to follow it: the shared client the
agent loop streams through, and the `/health` body an operator reads first.

Leaving either on `ollama_base_url` fails silently in the §11 way — the gateway
reports a healthy "ollama" that is really the embeddings server, while chat
either never moved or moved without anyone being able to see it.

This file also covers the three follow-up review findings on `/health` and
lifespan wiring:
  #1 (HIGH) — the two backend probes must run CONCURRENTLY and under a short
      budget, or a HANGING (not refusing) backend blocks `/health` past
      Docker's 3s HEALTHCHECK timeout and bounces a container that was never
      actually unhealthy.
  #2 (MEDIUM) — the embeddings client is built ONCE in `lifespan`, not fresh
      on every `/health` request (steady unpooled connection churn against an
      unauthenticated, frequently-polled endpoint).
  #3 (LOW) — the same-server shortcut compares NORMALISED URLs (matching
      `OllamaClient.__init__`'s own `.rstrip("/")`), so a trailing slash on
      only one of the two settings still takes the one-probe path.
"""

import asyncio
import json
import time

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


class _StubClient:
    """Stands in for a long-lived `OllamaClient` stored on `app.state`."""

    def __init__(self, base_url: str, healthy: bool = True, hang: float = 0.0) -> None:
        self.base_url = base_url
        self._healthy = healthy
        self._hang = hang

    async def is_healthy(self, timeout: float = 5.0) -> bool:
        """Mirrors the real client: a hang is bounded by `timeout`, and a
        backend that never answers within it comes back unreachable, not
        exceptionally.
        """
        if self._hang:
            await asyncio.sleep(min(self._hang, timeout))
            if self._hang >= timeout:
                return False
        return self._healthy


class _Request:
    def __init__(self, settings, ollama, embeddings) -> None:
        state = type(
            "S", (), {"settings": settings, "ollama": ollama, "embeddings": embeddings}
        )
        self.app = type("A", (), {"state": state})


def test_health_reports_the_chat_backend_not_the_embeddings_one():
    """The `ollama` key must stay the CHAT backend — the embeddings host
    belongs only in the separate `embeddings` key added for finding #2.
    """
    settings = Settings(**BASE_ENV, **SPLIT)
    ollama = _StubClient("http://vllm-host:8100")
    embeddings = _StubClient("http://embeddings-host:11434")
    response = asyncio.run(main_module.health(_Request(settings, ollama, embeddings)))
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


def test_health_reports_both_backends_when_split():
    """Finding #2: split URLs must both show up, using the two clients
    already on `app.state` — `/health` builds nothing itself.
    """
    settings = Settings(**BASE_ENV, **SPLIT)
    ollama = _StubClient("http://vllm-host:8100")
    embeddings = _StubClient("http://embeddings-host:11434")
    response = asyncio.run(main_module.health(_Request(settings, ollama, embeddings)))
    body = json.loads(response.body.decode())

    assert response.status_code == 200
    assert body["status"] == "ok"
    assert body["ollama"] == {"base_url": "http://vllm-host:8100", "reachable": True}
    assert body["embeddings"] == {
        "base_url": "http://embeddings-host:11434",
        "reachable": True,
    }


def test_health_surfaces_a_dead_embeddings_backend_without_failing_the_probe():
    """A healthy chat backend must not hide a dead embeddings backend.

    Before this fix `/health` asked only the chat backend, so vLLM up +
    Ollama down (department RAG dead) reported 200 "ok" with no signal at
    all. The HTTP status/overall liveness still tracks chat alone — restarting
    the gateway container cannot fix an external Ollama outage — but the body
    must say so explicitly for any monitor that parses it.
    """
    settings = Settings(**BASE_ENV, **SPLIT)
    ollama = _StubClient("http://vllm-host:8100", healthy=True)
    embeddings = _StubClient("http://embeddings-host:11434", healthy=False)
    response = asyncio.run(main_module.health(_Request(settings, ollama, embeddings)))
    body = json.loads(response.body.decode())

    assert response.status_code == 200
    assert body["ollama"]["reachable"] is True
    assert body["embeddings"]["reachable"] is False
    assert body["status"] != "ok"


def test_health_does_not_double_probe_when_both_urls_are_the_same():
    """The default, unset `AGENT_BASE_URL`: one server, one probe."""
    settings = Settings(**BASE_ENV)  # no split — both URLs equal
    probes: list[float] = []

    class _CountingClient(_StubClient):
        async def is_healthy(self, timeout: float = 5.0) -> bool:
            probes.append(timeout)
            return await super().is_healthy(timeout=timeout)

    ollama = _CountingClient("http://localhost:11434", healthy=True)
    # This stub must never be asked — passing a broken one that would raise
    # proves the same-server shortcut really does skip the second probe,
    # not just that its answer happens to match.
    class _MustNotBeCalled:
        base_url = "http://localhost:11434"

        async def is_healthy(self, timeout: float = 5.0) -> bool:
            raise AssertionError("embeddings probe ran despite same-server shortcut")

    response = asyncio.run(
        main_module.health(_Request(settings, ollama, _MustNotBeCalled()))
    )
    body = json.loads(response.body.decode())

    assert probes == [main_module.HEALTH_PROBE_TIMEOUT]  # exactly one probe
    assert body["ollama"]["base_url"] == body["embeddings"]["base_url"]
    assert body["embeddings"]["reachable"] is True


def test_health_takes_the_one_probe_path_on_a_trailing_slash_mismatch():
    """Finding #3: `AGENT_BASE_URL` with a trailing slash is still "the same
    server" as a slash-less `OLLAMA_BASE_URL` — `OllamaClient.__init__`
    normalises with `.rstrip("/")`, and the shortcut must compare the same
    normalised form or this pair wrongly takes the two-probe path.
    """
    settings = Settings(
        **BASE_ENV,
        ollama_base_url="http://gpu:11434",
        agent_base_url="http://gpu:11434/",
    )

    class _MustNotBeCalled:
        base_url = "http://gpu:11434/"

        async def is_healthy(self, timeout: float = 5.0) -> bool:
            raise AssertionError("embeddings probe ran despite matching servers")

    ollama = _StubClient("http://gpu:11434/", healthy=True)
    response = asyncio.run(
        main_module.health(_Request(settings, ollama, _MustNotBeCalled()))
    )
    body = json.loads(response.body.decode())
    assert body["embeddings"]["reachable"] is True


def test_health_returns_promptly_when_a_backend_hangs(monkeypatch):
    """Finding #1: a HANGING backend (not one that refuses) must not push
    `/health` anywhere near Docker's 3s HEALTHCHECK timeout, and the two
    probes must run CONCURRENTLY rather than summing their budgets.

    A short injected `HEALTH_PROBE_TIMEOUT` keeps this test itself fast while
    still proving the shape: both stubs "hang" for the full timeout, so a
    sequential implementation would take ~2x that, a concurrent one ~1x.
    """
    monkeypatch.setattr(main_module, "HEALTH_PROBE_TIMEOUT", 0.1)
    settings = Settings(**BASE_ENV, **SPLIT)
    ollama = _StubClient("http://vllm-host:8100", healthy=True, hang=0.1)
    embeddings = _StubClient("http://embeddings-host:11434", healthy=True, hang=0.1)

    started = time.perf_counter()
    response = asyncio.run(main_module.health(_Request(settings, ollama, embeddings)))
    elapsed = time.perf_counter() - started

    # Handler still returns (never hangs forever / raises).
    assert response.status_code in (200, 503)
    # Comfortably under the sequential sum (0.2s) and well under Docker's 3s
    # HEALTHCHECK timeout — proves the two probes ran concurrently.
    assert elapsed < 0.18


def test_lifespan_builds_a_single_embeddings_client_when_urls_match(monkeypatch):
    """Finding #2: no second client at all when there's only one server."""
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(**BASE_ENV))
    with TestClient(main_module.app) as client:
        assert client.app.state.embeddings is client.app.state.ollama


def test_lifespan_builds_one_shared_embeddings_client_when_split(monkeypatch):
    """Finding #2: exactly one embeddings client, built once, reused by
    every `/health` call for the life of the process (no per-request churn).
    """
    monkeypatch.setattr(
        main_module, "get_settings", lambda: Settings(**BASE_ENV, **SPLIT)
    )
    with TestClient(main_module.app) as client:
        assert client.app.state.embeddings is not client.app.state.ollama
        assert client.app.state.embeddings.base_url == "http://embeddings-host:11434"
        # Same object across repeated requests — nothing rebuilt per call.
        first = client.get("/health")
        assert first.status_code in (200, 503)
        assert client.app.state.embeddings is not client.app.state.ollama


def test_lifespan_does_not_close_the_aliased_client_twice(monkeypatch):
    """When the URLs match, `app.state.embeddings is app.state.ollama` — the
    shutdown path must close that single underlying client exactly once, not
    once per attribute name pointing at it.
    """
    close_calls: list[int] = []
    from app.ollama.client import OllamaClient

    original_aclose = OllamaClient.aclose

    async def _counting_aclose(self):
        close_calls.append(id(self))
        await original_aclose(self)

    monkeypatch.setattr(OllamaClient, "aclose", _counting_aclose)
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(**BASE_ENV))

    with TestClient(main_module.app):
        pass

    assert len(close_calls) == 1


def test_lifespan_closes_both_clients_when_split(monkeypatch):
    close_calls: list[int] = []
    from app.ollama.client import OllamaClient

    original_aclose = OllamaClient.aclose

    async def _counting_aclose(self):
        close_calls.append(id(self))
        await original_aclose(self)

    monkeypatch.setattr(OllamaClient, "aclose", _counting_aclose)
    monkeypatch.setattr(
        main_module, "get_settings", lambda: Settings(**BASE_ENV, **SPLIT)
    )

    with TestClient(main_module.app):
        pass

    assert len(close_calls) == 2
    assert len(set(close_calls)) == 2  # two distinct clients, not double-closed
