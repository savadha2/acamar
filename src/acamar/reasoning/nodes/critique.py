"""Critique — the CRAG grader.

Checks the draft is supported by the retrieved context, and nothing else
— not answer quality/helpfulness, that's the offline Judge's job
(docs/reasoning-pipeline-design.md §2.4). Always bumps `retry_count` when
not grounded, regardless of the retry cap — the graph's conditional edge
(not this node) decides whether that means another attempt or a
best-effort stop.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from acamar.ports.chat_model import ChatModel
from acamar.reasoning.context import RetrievalContext
from acamar.reasoning.state import QueryState

_PROMPT = """\
You are checking whether a draft answer is supported by the context it was
given - not whether it's a good or complete answer, only whether its
claims are grounded in the context below.

Context:
{context}

Draft answer: {draft_answer}

Is every factual claim in the draft answer supported by the context above?
An answer that honestly says it has no information counts as grounded -
that isn't a fabricated claim.
"""

_NO_CONTEXT = "(no relevant documents found)"


class CritiqueVerdict(BaseModel):
    """Structured output of one Critique call."""

    grounded: bool
    reason: str


class CritiqueNode:
    """Grades `state.draft_answer` against `state.retrieved`."""

    def __init__(self, chat_model: ChatModel, context: RetrievalContext | None = None) -> None:
        self._chat_model = chat_model
        self._context = context or RetrievalContext()

    async def __call__(self, state: QueryState) -> dict[str, Any]:
        """Return the grounded/reason update, bumping `retry_count` if ungrounded."""
        context = self._context.build(state.retrieved) or _NO_CONTEXT
        prompt = _PROMPT.format(context=context, draft_answer=state.draft_answer)
        verdict = await self._chat_model.structured(prompt, schema=CritiqueVerdict)

        update: dict[str, Any] = {
            "grounded": verdict.grounded,
            "critique_reason": verdict.reason,
            "final_answer": state.draft_answer,
        }
        if not verdict.grounded:
            update["retry_count"] = state.retry_count + 1
        return update
