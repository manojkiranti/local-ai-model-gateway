"""Embedding for the department corpus.

Qwen3-Embedding is **asymmetric**: a query carries an instruction prefix, a
document does not. Embedding both sides the same way is the most common way to
lose retrieval quality with this model family, and it fails silently — you just
get mediocre results. Hence `mode` is required, never defaulted.

Native output is 2560 dimensions; we MRL-truncate to 1536 because pgvector's
HNSW index caps at 2000, then re-normalize. Ollama honours a `dimensions`
parameter and normalizes for us, but doing it here is a portability contract:
whether a backend renormalizes after truncating is backend-specific, and a
non-unit sub-vector silently breaks `<#>` and `<->`.
"""

from __future__ import annotations

import math
from typing import Any, Literal, Protocol, Sequence

Mode = Literal["query", "document"]

# From the Qwen3-Embedding model card. Slice 3 (retrieval) is the only caller of
# query mode; it lives here so both sides can never drift apart.
QUERY_INSTRUCTION = (
    "Given a search query, retrieve relevant passages that answer the query"
)


class EmbeddingError(Exception):
    """The backend returned something we refuse to store."""


class EmbeddingClient(Protocol):
    async def embeddings(self, payload: dict[str, Any]) -> dict[str, Any]: ...


def format_query(text: str) -> str:
    return f"Instruct: {QUERY_INSTRUCTION}\nQuery: {text}"


def truncate_normalize(vec: Sequence[float], dim: int) -> list[float]:
    """Take the leading `dim` components (MRL packs the most significant first)
    and rescale to unit length."""
    if len(vec) < dim:
        raise EmbeddingError(
            f"embedding has {len(vec)} dimensions, need at least {dim}"
        )
    head = [float(x) for x in vec[:dim]]
    norm = math.sqrt(sum(x * x for x in head))
    if norm == 0.0:
        raise EmbeddingError("embedding is a zero vector; refusing to store it")
    return [x / norm for x in head]


def _ordered_vectors(response: dict, expected: int, dim: int) -> list[list[float]]:
    """Validate a batch response and return its vectors in INPUT order.

    `index` is authoritative — array order is not contractual — but a bad index
    is far worse than a missing one: silently defaulting it (`.get("index", 0)`)
    would map several results onto the same input and quietly drop the rest, so
    every failure mode here is an exception rather than a fallback.
    """
    data = response.get("data")
    if not isinstance(data, list) or len(data) != expected:
        raise EmbeddingError(
            f"expected {expected} embeddings, got "
            f"{len(data) if isinstance(data, list) else type(data).__name__}"
        )

    by_index: dict[int, Any] = {}
    for item in data:
        if not isinstance(item, dict) or "index" not in item:
            raise EmbeddingError("embedding result is missing its `index`")
        idx = item["index"]
        # bool is an int subclass; a True index is a bug, not position 1.
        if not isinstance(idx, int) or isinstance(idx, bool):
            raise EmbeddingError(f"embedding `index` is not an integer: {idx!r}")
        if not 0 <= idx < expected:
            raise EmbeddingError(
                f"embedding `index` {idx} out of range for a batch of {expected}"
            )
        if idx in by_index:
            raise EmbeddingError(f"duplicate embedding `index` {idx}")
        by_index[idx] = item

    if set(by_index) != set(range(expected)):  # pragma: no cover - defensive
        missing = sorted(set(range(expected)) - set(by_index))
        raise EmbeddingError(f"missing embeddings for inputs {missing}")

    return [truncate_normalize(by_index[i]["embedding"], dim) for i in range(expected)]


async def embed_texts(
    client: EmbeddingClient,
    texts: Sequence[str],
    *,
    mode: Mode,
    model: str,
    dim: int,
    batch_size: int,
) -> list[list[float]]:
    """Embed `texts` in batches, returning unit-length `dim`-wide vectors in the
    same order as the input."""
    if mode not in ("query", "document"):
        raise EmbeddingError(f"unknown embedding mode {mode!r}")
    if not texts:
        return []

    prepared = [format_query(t) if mode == "query" else t for t in texts]
    out: list[list[float]] = []

    for start in range(0, len(prepared), max(1, batch_size)):
        batch = prepared[start : start + max(1, batch_size)]
        response = await client.embeddings({"model": model, "input": list(batch)})
        out.extend(_ordered_vectors(response, len(batch), dim))

    return out
