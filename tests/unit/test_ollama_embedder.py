"""OllamaEmbedder — the ollama client is faked."""

from __future__ import annotations

from typing import Any

import ollama
import pytest

from acamar.adapters import cloud_run_auth
from acamar.adapters.cloud_run_auth import is_cloud_run_host
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


# --- Cloud Run identity-token auth (2026-10-24, interim CPU ollama-embed) ---
# Ollama has zero auth of its own; a *.run.app base_url must get a Google-
# signed identity token attached, and -- the critical safety property --
# nothing else (localhost, docker compose) may ever attempt that fetch.


def test_is_cloud_run_host_true_for_a_run_app_url() -> None:
    assert is_cloud_run_host("https://ollama-embed-abc123-uc.a.run.app") is True


def test_is_cloud_run_host_false_for_localhost() -> None:
    assert is_cloud_run_host("http://localhost:11434") is False


def test_is_cloud_run_host_false_for_an_unrelated_host() -> None:
    assert is_cloud_run_host("https://example.com") is False


async def test_cloud_run_host_fetches_and_attaches_an_identity_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def _fake_token(audience: str) -> str:
        captured["audience"] = audience
        return "fake-id-token"

    class _FakeAsyncClient:
        def __init__(self, *, host: str, headers: dict[str, str]) -> None:
            captured["host"] = host
            captured["headers"] = headers

        async def embed(self, *, model: str, input: list[str]) -> object:
            return _FakeResponse([[0.1, 0.2, 0.3]])

    monkeypatch.setattr(cloud_run_auth, "identity_token", _fake_token)
    monkeypatch.setattr(ollama, "AsyncClient", _FakeAsyncClient)

    embedder = OllamaEmbedder(base_url="https://ollama-embed-abc123-uc.a.run.app", expected_dim=3)
    await embedder.embed(["a"])

    assert captured["audience"] == "https://ollama-embed-abc123-uc.a.run.app"
    assert captured["headers"] == {"Authorization": "Bearer fake-id-token"}


async def test_local_host_never_attempts_a_token_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    # The GCP metadata server this would hit doesn't exist outside GCP
    # compute -- this must never even try, or local dev / docker compose
    # would hang or fail on every single embed call.
    called = False

    async def _should_not_be_called(audience: str) -> str:
        nonlocal called
        called = True
        return "unexpected"

    class _FakeAsyncClient:
        def __init__(self, *, host: str, headers: dict[str, str]) -> None:
            self.host = host
            self.headers = headers

        async def embed(self, *, model: str, input: list[str]) -> object:
            return _FakeResponse([[0.1, 0.2, 0.3]])

    monkeypatch.setattr(cloud_run_auth, "identity_token", _should_not_be_called)
    monkeypatch.setattr(ollama, "AsyncClient", _FakeAsyncClient)

    embedder = OllamaEmbedder(base_url="http://localhost:11434", expected_dim=3)
    await embedder.embed(["a"])

    assert called is False
