"""Retrieval grading — canonical CRAG's actual mechanism.

Yan, Gu, Zhu, Ling (2024), "Corrective Retrieval Augmented Generation"
(arXiv:2401.15884) grades the *retrieved documents* before generation.
`CritiqueNode` in this codebase grades the *drafted answer's* groundedness
after generation instead — closer to Self-RAG's reflection tokens than to
CRAG proper. That gap is exactly why a bad retrieval used to slip through
uncaught: an honest "I don't know" synthesized from an empty or
off-topic retrieval reads as *grounded* to Critique (it isn't a
fabrication), so the widen-and-retry safety net never fired for the single
most common real failure — a too-narrow Router guess that missed the
right document outright. This node catches that earlier, before a
synthesis call is even made on a doomed retrieval. See docs/sota.md §1
and the backlog memory.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from acamar.ports.chat_model import ChatModel
from acamar.reasoning.state import QueryState

_PROMPT = """\
A parent asked a school-logistics question. Below are the titles of
documents a search found for it. Judge only whether these documents
plausibly contain information that answers the question - not whether an
answer from them would be complete, well-written, or exactly precise.

Question: {question}

Retrieved document titles:
{titles}

If the list is empty, or none of the titles are plausibly about the same
subject as the question, say not relevant.
"""

_NO_DOCS = "(no documents were found)"


class RetrievalGrade(BaseModel):
    """Structured output of one retrieval-grading call."""

    relevant: bool
    reason: str


class RetrievalGradeNode:
    """Grades `state.retrieved` against `state.question`, before Synthesis runs.

    Titles only, not the full `RetrievalContext` — this is a coarse
    topical-match check, not a synthesis-quality one, and titles are
    enough to judge that cheaply.
    """

    def __init__(self, chat_model: ChatModel) -> None:
        self._chat_model = chat_model

    async def __call__(self, state: QueryState) -> dict[str, Any]:
        """Return the relevance update, bumping `retry_count` if not relevant."""
        titles = "\n".join(f"- {doc.headline or doc.title}" for doc in state.retrieved)
        prompt = _PROMPT.format(question=state.question, titles=titles or _NO_DOCS)
        verdict = await self._chat_model.structured(prompt, schema=RetrievalGrade)

        update: dict[str, Any] = {"retrieval_relevant": verdict.relevant}
        if not verdict.relevant:
            update["retry_count"] = state.retry_count + 1
        return update
