"""The chat backend may live on a different server than the embeddings backend.

Moving the chat/agent model to vLLM while embeddings and the reranker stay on
Ollama means the gateway must address TWO model servers. `ollama_base_url` keeps
its meaning — the Ollama that serves embeddings — and `agent_base_url` is the new
chat/agent backend.

The blank default is the whole point: a dev laptop (where vLLM is impractical)
sets nothing and keeps talking to one local Ollama exactly as before, while the
server sets `AGENT_BASE_URL` to the vLLM port. Same build, both environments.
"""

import ast
from pathlib import Path

from app.config import Settings

REPO_ROOT = Path(__file__).resolve().parent.parent

BASE_ENV = {
    "database_url": "postgresql+asyncpg://u:p@127.0.0.1:5432/db",
    "jwt_secret": "test-secret",
}


def _code_only(path: Path) -> str:
    """The module's CODE, with comments and docstrings removed.

    Via `ast`, not a text scan — this file's own prose (and the modules
    under test) talk about `chat_base_url` in comments while deliberately
    NOT reading it, and a naive `"chat_base_url" not in source` would trip on
    that explanation rather than on an actual reference. Ordinary string
    literals are kept, since a raw attribute name inside one would still be
    worth catching. Same helper as
    `tests/test_nrb_corpus_ingest.py::_code_only`.
    """
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ) and ast.get_docstring(node) is not None:
            node.body = node.body[1:]
    return ast.unparse(tree)


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


EMBEDDING_MODULES = (
    "app/rag/worker.py",
    "app/tools/local/search_department_docs.py",
)


def test_splitting_the_chat_backend_does_not_move_the_embeddings_backend():
    """The embeddings/reranker URL is untouched by the split.

    `app/rag/worker.py` and the query-embed path (`search_department_docs.py`)
    must build their `OllamaClient` from `settings.ollama_base_url` and never
    from `settings.chat_base_url`. If the split leaked into either module,
    document and query embeddings would be sent to whatever serves CHAT (vLLM
    once the cutover happens) — which answers with a completion, not a
    rejection, so the failure would be silent and every retrieval afterward
    subtly wrong.

    This used to assert `settings.ollama_base_url == "http://gpu:11434"` —
    that a Settings field returns what was just assigned to it. That can
    never fail for the regression this docstring describes: it says nothing
    about which attribute either module actually reads. This is a real guard,
    at the source level, in the style of
    `tests/test_nrb_corpus_ingest.py::test_the_driver_never_consults_the_extraction_evidence_table`.
    """
    for rel_path in EMBEDDING_MODULES:
        code = _code_only(REPO_ROOT / rel_path)
        assert "ollama_base_url" in code, (
            f"{rel_path} no longer references ollama_base_url at all — "
            "the embeddings client's source URL changed"
        )
        assert "chat_base_url" not in code, (
            f"{rel_path} now references chat_base_url — the embeddings/query-"
            "embed path must stay on ollama_base_url, never the chat backend"
        )


def test_a_whitespace_only_agent_base_url_falls_back_to_the_ollama_url():
    """`AGENT_BASE_URL="   "` must behave like blank, not like a set URL.

    A bare `or` check treats any non-empty string as truthy, including one
    that is only whitespace — which would then be used as an httpx base_url
    verbatim (unusable) instead of falling back to Ollama the way an
    actually-blank value does.
    """
    settings = settings_with(
        ollama_base_url="http://localhost:11434",
        agent_base_url="   ",
    )
    assert settings.chat_base_url == "http://localhost:11434"


def test_the_shipped_default_keeps_one_backend():
    """Out of the box the two URLs are the same server — no behaviour change."""
    settings = settings_with()
    assert settings.agent_base_url == ""
    assert settings.chat_base_url == settings.ollama_base_url
