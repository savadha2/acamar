"""`Embedder` backed by a local Ollama model (default: nomic-embed-text)."""

from __future__ import annotations

from urllib.parse import urlparse

import httpx
import ollama

from acamar.config import get_settings
from acamar.domain.models import Vector
from acamar.ports.embedder import EmbeddingError

_METADATA_IDENTITY_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity"
)


def _is_cloud_run_host(url: str) -> bool:
    """Whether `url` points at a Cloud Run service (a `*.run.app` host).

    Gates the identity-token fetch below — must stay False for
    `localhost`/bare-metal/`docker compose`, or local dev would try (and
    hang, then fail) to reach a metadata server that doesn't exist there.
    """
    host = urlparse(url).hostname
    return host is not None and host.endswith(".run.app")


async def _cloud_run_identity_token(audience: str) -> str:
    """A Google-signed ID token for calling another Cloud Run service privately.

    Fetched fresh per call from the GCP metadata server. Added for the
    interim CPU-only `ollama-embed` service (GPU quota still
    ungranted as of 2026-10-24) — Ollama has zero auth of its own, so this
    (not a network/ingress trick — see the deploy notes for why) is the
    real boundary keeping it from being reachable by the open internet.
    Only reachable, and only ever called, when actually running on GCP
    compute (see `_is_cloud_run_host`).
    """
    async with httpx.AsyncClient() as client:
        response = await client.get(
            _METADATA_IDENTITY_URL,
            params={"audience": audience},
            headers={"Metadata-Flavor": "Google"},
            timeout=5.0,
        )
        response.raise_for_status()
        return response.text


class OllamaEmbedder:
    def __init__(
        self,
        *,
        model: str | None = None,
        base_url: str | None = None,
        expected_dim: int | None = None,
        client: ollama.AsyncClient | None = None,  # inject in tests
    ) -> None:
        settings = get_settings()
        self._model = model or settings.ollama_embed_model
        self._expected_dim = expected_dim or settings.embedding_dim
        self._base_url = base_url or settings.ollama_base_url
        self._injected_client = client

    async def _client_for_this_call(self) -> ollama.AsyncClient:
        # Built per-call (not cached on self) specifically so a fresh
        # identity token attaches every time, rather than one fetched at
        # construction going stale — see _cloud_run_identity_token.
        if self._injected_client is not None:
            return self._injected_client
        headers: dict[str, str] = {}
        if _is_cloud_run_host(self._base_url):
            token = await _cloud_run_identity_token(self._base_url)
            headers["Authorization"] = f"Bearer {token}"
        return ollama.AsyncClient(host=self._base_url, headers=headers)

    async def embed(self, texts: list[str]) -> list[Vector]:
        if not texts:
            return []
        try:
            client = await self._client_for_this_call()
            response = await client.embed(model=self._model, input=texts)
        except Exception as exc:
            # any transport / model failure surfaces as a typed EmbeddingError
            raise EmbeddingError(f"ollama embed ({self._model}): {exc}") from exc

        vectors: list[Vector] = [list(v) for v in response.embeddings]
        if len(vectors) != len(texts):
            raise EmbeddingError(f"ollama returned {len(vectors)} vectors for {len(texts)} inputs")
        if vectors and len(vectors[0]) != self._expected_dim:
            raise EmbeddingError(
                f"{self._model} produced dim {len(vectors[0])}, "
                f"expected {self._expected_dim} (see EMBEDDING_DIM / db/schema.sql)"
            )
        return vectors
