"""`Embedder` backed by a local Ollama model (default: nomic-embed-text)."""

from __future__ import annotations

import ollama

from acamar.config import get_settings
from acamar.domain.models import Vector
from acamar.ports.embedder import EmbeddingError


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
        self._client = client or ollama.AsyncClient(
            host=base_url or settings.ollama_base_url
        )

    async def embed(self, texts: list[str]) -> list[Vector]:
        if not texts:
            return []
        try:
            response = await self._client.embed(model=self._model, input=texts)
        except Exception as exc:
            # any transport / model failure surfaces as a typed EmbeddingError
            raise EmbeddingError(f"ollama embed ({self._model}): {exc}") from exc

        vectors: list[Vector] = [list(v) for v in response.embeddings]
        if len(vectors) != len(texts):
            raise EmbeddingError(
                f"ollama returned {len(vectors)} vectors for {len(texts)} inputs"
            )
        if vectors and len(vectors[0]) != self._expected_dim:
            raise EmbeddingError(
                f"{self._model} produced dim {len(vectors[0])}, "
                f"expected {self._expected_dim} (see EMBEDDING_DIM / db/schema.sql)"
            )
        return vectors
