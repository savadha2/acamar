"""Retrieval — embeds the question, searches with a widening filter.

Searches with a filter that widens on each CRAG retry. See
docs/reasoning-pipeline-design.md §2.4.

|Attempt|categories      |domains |date window   |limit|
|-------|----------------|--------|--------------|-----|
|0      |Router's own    |Router's own|Router's own|8    |
|1      |Router's own    |Router's own|2x wider    |8    |
|2 final|all             |**all** (None)|2x wider|12   |

Each attempt's window is computed fresh from Router's *original*
`state.date_from`/`date_to` (this node never writes those fields back to
state) — "2x wider" at attempt 1 and attempt 2 both mean the same thing,
not a cumulative widening.

Real bug found 2026-09-24: the final attempt already drops the category
restriction as a safety net against a bad Router guess, but used to keep
Router's original `domains` unchanged even here — so a wrongly-narrowed
domain (e.g. Router guessing `[school]` for a question that's actually
answered by a `gmail`-sourced email) could make retrieval return zero (or
wrong) results *no matter how many retries ran*, since nothing ever
widened it back to "search everywhere." The final attempt now resets
`domains` to `None` too, matching Decision A's own stated default.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from acamar.domain.enums import Category
from acamar.ports.embedder import Embedder
from acamar.ports.repositories import DocumentRepository
from acamar.reasoning.state import MAX_RETRIES, QueryState

_ATTEMPT_LIMIT = 8
_FINAL_LIMIT = 12
_MIN_WIDEN_PAD = timedelta(days=3)
"""Floor on the widening pad so a same-day window ("today") still expands
meaningfully on retry instead of padding by zero."""


def _widen(date_from: date | None, date_to: date | None) -> tuple[date | None, date | None]:
    if date_from is None or date_to is None:
        return date_from, date_to
    pad = max((date_to - date_from) // 2, _MIN_WIDEN_PAD)
    return date_from - pad, date_to + pad


class RetrievalNode:
    """Embeds `state.question`, retrieves with the attempt-appropriate filter."""

    def __init__(self, embedder: Embedder, documents: DocumentRepository) -> None:
        self._embedder = embedder
        self._documents = documents

    async def __call__(self, state: QueryState) -> dict[str, Any]:
        """Return `{"retrieved": [...]}` for the current `state.retry_count` attempt."""
        vectors = await self._embedder.embed([state.question])
        embedding = vectors[0]

        attempt = state.retry_count
        if attempt >= MAX_RETRIES:
            categories = list(Category)
            domains = None  # reset, not just categories — see module docstring
            date_from, date_to = _widen(state.date_from, state.date_to)
            limit = _FINAL_LIMIT
        elif attempt == 1:
            categories = state.categories
            domains = state.domains
            date_from, date_to = _widen(state.date_from, state.date_to)
            limit = _ATTEMPT_LIMIT
        else:
            categories = state.categories
            domains = state.domains
            date_from, date_to = state.date_from, state.date_to
            limit = _ATTEMPT_LIMIT

        retrieved = await self._documents.search(
            embedding,
            categories=categories,
            domains=domains,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
        )
        return {"retrieved": retrieved}
