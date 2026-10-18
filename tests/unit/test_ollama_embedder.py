"""OllamaEmbedder — the ollama client is faked."""

from __future__ import annotations

from typing import Any

import pytest

from acamar.adapters.embedding.ollama_embedder import OllamaEmbedder
from acamar.ports.embedder import EmbeddingError


class _FakeResponse:
    def __init__(self, embeddings: list[list[float]]) -> None:
        self.embeddings = embeddings


class _FakeClient:
    def __init__(self, response: object = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.calls: list[dict[str, Any]] = []

    async def embed(self, *, model: str, input: list[str]) -> object:
        self.calls.append({"model": model, "input": input})
        if self._error is not None:
            raise self._error
        return self._response


def _embedder(client: _FakeClient, *, dim: int = 3) -> OllamaEmbedder:
    return OllamaEmbedder(client=client, model="test-embed", expected_dim=dim)  # type: ignore[arg-type]


async def test_empty_input_short_circuits() -> None:
    client = _FakeClient()
    assert await _embedder(client).embed([]) == []
    assert client.calls == []


async def test_returns_one_vector_per_text() -> None:
    client = _FakeClient(_FakeResponse([[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]))
    vectors = await _embedder(client).embed(["a", "b"])
    assert vectors == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    assert client.calls[0] == {"model": "test-embed", "input": ["a", "b"]}


async def test_transport_failure_becomes_embedding_error() -> None:
    client = _FakeClient(error=RuntimeError("connection refused"))
    with pytest.raises(EmbeddingError, match="connection refused"):
        await _embedder(client).embed(["a"])


async def test_wrong_vector_count_raises() -> None:
    client = _FakeClient(_FakeResponse([[0.1, 0.2, 0.3]]))  # 1 for 2 inputs
    with pytest.raises(EmbeddingError, match="1 vectors for 2"):
        await _embedder(client).embed(["a", "b"])


async def test_wrong_dimension_raises() -> None:
    client = _FakeClient(_FakeResponse([[0.1, 0.2]]))  # dim 2, expected 3
    with pytest.raises(EmbeddingError, match="dim 2"):
        await _embedder(client).embed(["a"])
