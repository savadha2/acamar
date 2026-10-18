"""Builds the retrieved-document text handed to Synthesis/Critique.

Truncated to fit `settings.ollama_num_ctx` — the CLAUDE.md gotcha this
whole module exists for. One builder, reused by both nodes, so the two
never see a different view of "the context" for the same documents.
"""

from __future__ import annotations

from acamar.config import get_settings
from acamar.domain.models import Document

_RESERVED_TOKENS = 1024
"""Budget left for the prompt template + question + the model's own answer."""
_CHARS_PER_TOKEN = 4.0
"""Rough estimate — good enough for a truncation budget, not exact tokenization."""


class RetrievalContext:
    """Formats `Document`s into one context block, budget-truncated.

    Stops once the token budget would be exceeded. Always includes at
    least the first document, even if it alone exceeds the budget — an
    over-long single match beats an empty context (Synthesis would
    otherwise always say "I don't know", even when one real relevant
    document existed).
    """

    def __init__(self, *, num_ctx: int | None = None) -> None:
        settings = get_settings()
        self._num_ctx = num_ctx if num_ctx is not None else settings.ollama_num_ctx

    def build(self, documents: list[Document]) -> str:
        """Return the context block, or `""` if `documents` is empty."""
        budget_chars = max(0, self._num_ctx - _RESERVED_TOKENS) * _CHARS_PER_TOKEN
        parts: list[str] = []
        used = 0.0
        for doc in documents:
            block = _format(doc)
            if parts and used + len(block) > budget_chars:
                break
            parts.append(block)
            used += len(block)
        return "\n\n".join(parts)


def _format(doc: Document) -> str:
    when = f" ({doc.event_date.isoformat()})" if doc.event_date else ""
    body = doc.summary or doc.content
    return f"- {doc.headline or doc.title}{when}: {body}"
