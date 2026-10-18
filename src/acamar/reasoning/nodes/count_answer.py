"""Count branch — one exact SQL `count(*)`, no retrieval, no LLM call.

Retrieval's top-K is a similarity rank, not exhaustive — synthesizing a
count from it risks a confidently wrong number. This bypasses that
entirely (docs/reasoning-pipeline-design.md Decision G).
"""

from __future__ import annotations

from typing import Any

from acamar.ports.repositories import DocumentRepository
from acamar.reasoning.state import QueryState


class CountAnswerNode:
    """Answers a `COUNT`-intent question with an exact number."""

    def __init__(self, documents: DocumentRepository) -> None:
        self._documents = documents

    async def __call__(self, state: QueryState) -> dict[str, Any]:
        """Return `{"count_result": n, "final_answer": ...}`."""
        n = await self._documents.count(
            categories=state.categories,
            domains=state.domains,
            date_from=state.date_from,
            date_to=state.date_to,
        )
        noun = "item" if n == 1 else "items"
        return {
            "count_result": n,
            "final_answer": f"You have {n} matching {noun}.",
            "grounded": True,
        }
