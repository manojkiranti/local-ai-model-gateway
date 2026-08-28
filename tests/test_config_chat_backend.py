"""The chat backend may live on a different server than the embeddings backend.

Moving the chat/agent model to vLLM while embeddings and the reranker stay on
Ollama means the gateway must address TWO model servers. `ollama_base_url` keeps
its meaning — the Ollama that serves embeddings — and `agent_base_url` is the new
chat/agent backend.

The blank default is the whole point: a dev laptop (where vLLM is impractical)
sets nothing and keeps talking to one local Ollama exactly as before, while the
server sets `AGENT_BASE_URL` to the vLLM port. Same build, both environments.
"""

from app.config import Settings

BASE_ENV = {
    "database_url": "postgresql+asyncpg://u:p@127.0.0.1:5432/db",
    "jwt_secret": "test-secret",
}


def settings_with(**overrides) -> Settings:
    return Settings(**BASE_ENV, **overrides)


def test_a_blank_agent_base_url_falls_back_to_the_ollama_url():
    """The laptop path: set nothing, behave exactly as before the split."""
    settings = settings_with(ollama_base_url="http://localhost:11434")
    assert settings.chat_base_url == "http://localhost:11434"


def test_a_set_agent_base_url_becomes_the_chat_backend():
    """The server path: chat goes to vLLM."""
    settings = settings_with(
        ollama_base_url="http://gpu:11434",
        agent_base_url="http://gpu:8100",
    )
    assert settings.chat_base_url == "http://gpu:8100"


def test_splitting_the_chat_backend_does_not_move_the_embeddings_backend():
    """The embeddings/reranker URL is untouched by the split.

    `app/rag/worker.py` and the query-embed path read `ollama_base_url`. If the
    split leaked into that value, document and query embeddings would be sent to
    a vLLM serving a CHAT model — which answers, so the failure is silent.
    """
    settings = settings_with(
        ollama_base_url="http://gpu:11434",
        agent_base_url="http://gpu:8100",
    )
    assert settings.ollama_base_url == "http://gpu:11434"


def test_the_shipped_default_keeps_one_backend():
    """Out of the box the two URLs are the same server — no behaviour change."""
    settings = settings_with()
    assert settings.agent_base_url == ""
    assert settings.chat_base_url == settings.ollama_base_url
