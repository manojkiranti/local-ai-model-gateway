"""Embedding helper. Pure — a fake client, no network.

Locks the three silent-failure modes: query/document asymmetry, truncation +
renormalization, and batch result ordering.
"""

import asyncio
import math

import pytest

from app.rag.embedding import (
    EmbeddingError,
    QUERY_INSTRUCTION,
    embed_texts,
    format_query,
    truncate_normalize,
)

DIM = 1536


class FakeClient:
    """Returns deterministic, DIRECTIONALLY DISTINCT vectors.

    A constant vector like [k, k, k, ...] normalizes to the same unit vector for
    every k, which would make an ordering test vacuous — the vectors would be
    identical no matter how they were shuffled. So input i gets a one-hot-ish
    vector with its marker in slot i, which survives normalization.
    """

    def __init__(self, native_dim=2560, shuffle=False, dim_override=None,
                 bad_index=None, duplicate_index=False):
        self.native_dim = native_dim
        self.shuffle = shuffle
        self.dim_override = dim_override
        self.bad_index = bad_index
        self.duplicate_index = duplicate_index
        self.payloads = []

    def _vector(self, i, n):
        vec = [0.0] * n
        vec[i % n] = 1.0        # direction depends on i, survives normalization
        vec[(i + 1) % n] = 0.5
        return vec

    async def embeddings(self, payload):
        self.payloads.append(payload)
        n = self.dim_override or self.native_dim
        data = [
            {"index": i, "embedding": self._vector(i, n)}
            for i, _ in enumerate(payload["input"])
        ]
        if self.duplicate_index:
            for item in data:
                item["index"] = 0
        if self.bad_index is not None:
            data[0]["index"] = self.bad_index
        if self.shuffle:
            data = list(reversed(data))  # index is authoritative, order is not
        return {"data": data}


def _argmax(vec):
    return max(range(len(vec)), key=lambda i: vec[i])


def _run(coro):
    return asyncio.run(coro)


def test_query_mode_uses_the_instruction_prefix():
    formatted = format_query("how much annual leave?")
    assert formatted.startswith("Instruct: ")
    assert QUERY_INSTRUCTION in formatted
    assert "Query: how much annual leave?" in formatted


def test_documents_are_embedded_raw():
    client = FakeClient()
    _run(embed_texts(client, ["a policy paragraph"], mode="document",
                     model="m", dim=DIM, batch_size=8))
    assert client.payloads[0]["input"] == ["a policy paragraph"]


def test_queries_are_embedded_with_the_prefix():
    client = FakeClient()
    _run(embed_texts(client, ["annual leave"], mode="query",
                     model="m", dim=DIM, batch_size=8))
    sent = client.payloads[0]["input"][0]
    assert sent.startswith("Instruct: ") and "annual leave" in sent


def test_unknown_mode_is_rejected():
    client = FakeClient()
    with pytest.raises(EmbeddingError):
        _run(embed_texts(client, ["x"], mode="sideways",
                         model="m", dim=DIM, batch_size=8))


def test_truncate_normalize_yields_unit_length():
    out = truncate_normalize([3.0] * 2560, DIM)
    assert len(out) == DIM
    assert math.isclose(math.sqrt(sum(x * x for x in out)), 1.0, rel_tol=1e-9)


def test_truncate_keeps_the_leading_dimensions():
    """MRL packs the most significant dimensions first — take the head."""
    vec = [float(i) for i in range(2560)]
    out = truncate_normalize(vec, 4)
    assert out[0] < out[1] < out[2] < out[3]   # order of 0,1,2,3 preserved


def test_a_short_vector_is_an_error_not_a_pad():
    with pytest.raises(EmbeddingError):
        truncate_normalize([1.0] * 768, DIM)


def test_a_zero_vector_is_an_error_not_a_divide_by_zero():
    with pytest.raises(EmbeddingError):
        truncate_normalize([0.0] * 2560, DIM)


def test_every_returned_vector_is_exactly_dim_wide():
    client = FakeClient()
    out = _run(embed_texts(client, ["a", "b", "c"], mode="document",
                           model="m", dim=DIM, batch_size=8))
    assert [len(v) for v in out] == [DIM, DIM, DIM]


def test_results_are_reordered_by_index_not_array_position():
    """The backend may return objects in any order; `index` is authoritative.

    FakeClient encodes input position as the vector's DIRECTION (slot i), which
    survives normalization — a constant-magnitude encoding would not.
    """
    ordered = FakeClient(shuffle=False)
    shuffled = FakeClient(shuffle=True)
    a = _run(embed_texts(ordered, ["a", "b", "c"], mode="document",
                         model="m", dim=DIM, batch_size=8))
    b = _run(embed_texts(shuffled, ["a", "b", "c"], mode="document",
                         model="m", dim=DIM, batch_size=8))
    assert a == b                       # reordering undoes the shuffle exactly
    assert [_argmax(v) for v in a] == [0, 1, 2]


def test_out_of_range_index_is_rejected():
    with pytest.raises(EmbeddingError):
        _run(embed_texts(FakeClient(bad_index=99), ["a", "b"], mode="document",
                         model="m", dim=DIM, batch_size=8))


def test_negative_index_is_rejected():
    with pytest.raises(EmbeddingError):
        _run(embed_texts(FakeClient(bad_index=-1), ["a", "b"], mode="document",
                         model="m", dim=DIM, batch_size=8))


def test_duplicate_indexes_are_rejected():
    """Two objects claiming index 0 would silently drop an input."""
    with pytest.raises(EmbeddingError):
        _run(embed_texts(FakeClient(duplicate_index=True), ["a", "b"],
                         mode="document", model="m", dim=DIM, batch_size=8))


def test_a_missing_index_field_is_rejected_not_defaulted():
    class NoIndex(FakeClient):
        async def embeddings(self, payload):
            full = await super().embeddings(payload)
            for item in full["data"]:
                item.pop("index")
            return full

    with pytest.raises(EmbeddingError):
        _run(embed_texts(NoIndex(), ["a", "b"], mode="document",
                         model="m", dim=DIM, batch_size=8))


def test_a_non_integer_index_is_rejected():
    class StringIndex(FakeClient):
        async def embeddings(self, payload):
            full = await super().embeddings(payload)
            for item in full["data"]:
                item["index"] = str(item["index"])
            return full

    with pytest.raises(EmbeddingError):
        _run(embed_texts(StringIndex(), ["a", "b"], mode="document",
                         model="m", dim=DIM, batch_size=8))


def test_batching_splits_requests_and_preserves_overall_order():
    client = FakeClient()
    texts = [f"t{i}" for i in range(10)]
    out = _run(embed_texts(client, texts, mode="document",
                           model="m", dim=DIM, batch_size=4))
    assert len(out) == 10
    assert [len(p["input"]) for p in client.payloads] == [4, 4, 2]


def test_empty_input_makes_no_request():
    client = FakeClient()
    assert _run(embed_texts(client, [], mode="document",
                            model="m", dim=DIM, batch_size=4)) == []
    assert client.payloads == []


def test_a_backend_returning_too_few_dimensions_is_an_error():
    """Guards against silently storing a wrong-width vector if the model or the
    backend changes underneath us."""
    client = FakeClient(dim_override=768)
    with pytest.raises(EmbeddingError):
        _run(embed_texts(client, ["a"], mode="document",
                         model="m", dim=DIM, batch_size=4))


def test_a_missing_result_is_an_error_not_a_short_list():
    class Truncating(FakeClient):
        async def embeddings(self, payload):
            full = await super().embeddings(payload)
            full["data"] = full["data"][:-1]
            return full

    with pytest.raises(EmbeddingError):
        _run(embed_texts(Truncating(), ["a", "b"], mode="document",
                         model="m", dim=DIM, batch_size=8))
