"""`ChatModel` backed by a local Ollama model, using structured (JSON) output.

Same proven pattern as `adapters/enrichment/ollama_enricher.py` — plain
`ollama.AsyncClient` + JSON-schema-constrained output, not LangChain's
`.with_structured_output()` (docs/reasoning-pipeline-design.md Decision D).
One instance per configured model (router/synthesis/critique/judge each
get their own) — not a shared multi-model service object, same as
`OllamaEnricher` is bound to one model.
"""

from __future__ import annotations

from typing import TypeVar

import ollama
from pydantic import BaseModel

from acamar.config import get_settings
from acamar.ports.chat_model import ChatModelError

T = TypeVar("T", bound=BaseModel)


class OllamaChatModel:
    def __init__(
        self,
        *,
        model: str,
        base_url: str | None = None,
        client: ollama.AsyncClient | None = None,  # inject in tests
    ) -> None:
        settings = get_settings()
        self._model = model
        self._client = client or ollama.AsyncClient(host=base_url or settings.ollama_base_url)

    async def structured(self, prompt: str, *, schema: type[T]) -> T:
        try:
            response = await self._client.chat(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                format=schema.model_json_schema(),
            )
        except Exception as exc:
            # any transport / model failure surfaces as a typed ChatModelError
            raise ChatModelError(f"ollama chat ({self._model}): {exc}") from exc

        content = response.message.content
        if not content:
            raise ChatModelError(f"ollama chat ({self._model}) returned no content")
        try:
            return schema.model_validate_json(content)
        except Exception as exc:
            # malformed/non-conforming JSON from a small model — treat like any
            # other model failure, not a bug in this adapter
            raise ChatModelError(
                f"ollama chat ({self._model}) returned unparseable output: {exc}"
            ) from exc
