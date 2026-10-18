"""The text-to-vector port. The core never calls an embedding model directly."""

from __future__ import annotations

from typing import Protocol

from acamar.domain.models import Vector


class EmbeddingError(RuntimeError):
    """The embedding model was unreachable or returned an unusable result."""


class Embedder(Protocol):
    """Turns document text into vectors for similarity search."""

    async def embed(self, texts: list[str]) -> list[Vector]:
        """Return one vector per input, in the same order.

        ``embed([])`` returns ``[]``.

        Raises:
            EmbeddingError: the model failed or returned the wrong dimension.
        """
        ...
