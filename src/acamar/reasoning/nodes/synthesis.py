"""Synthesis — drafts an answer from retrieved context only.

The question is assembled outside the truncated context block (never
counted against `RetrievalContext`'s budget) so it can never be silently
dropped by truncation.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from acamar.ports.chat_model import ChatModel
from acamar.reasoning.context import RetrievalContext
from acamar.reasoning.state import QueryState

_PROMPT = """\
You are answering a parent's question about their child's school, using
ONLY the context below. Today's date: {today}.

Context:
{context}

Include every concrete fact already stated in the context (dates, times,
locations, costs, contact info, links) - do not compress them away into a
vague summary. Do not add any explanation, definition, or detail that is
not explicitly stated in the context, even if asked to "elaborate" -
elaborating means surfacing everything already there, never inventing more
(tested 2026-09-24: an earlier version of this instruction that just said
"answer completely" measurably caused fabricated detail on a small model;
this phrasing was verified not to, against the same real question).

You cannot display an image. If asked to show/send a picture, poster, or
flyer, say so plainly - and if the context includes a link to it, share
that link.

If the context above does not contain the answer, say so plainly - do not
guess or make anything up beyond what's stated.

Question: {question}
"""

_NO_CONTEXT = "(no relevant documents found)"


class SynthesisOutput(BaseModel):
    """Structured output of one Synthesis call."""

    answer: str


class SynthesisNode:
    """Drafts `state.draft_answer` from `state.retrieved`."""

    def __init__(self, chat_model: ChatModel, context: RetrievalContext | None = None) -> None:
        self._chat_model = chat_model
        self._context = context or RetrievalContext()

    async def __call__(self, state: QueryState) -> dict[str, Any]:
        """Return `{"draft_answer": ...}`."""
        context = self._context.build(state.retrieved) or _NO_CONTEXT
        prompt = _PROMPT.format(
            today=state.today.isoformat(), context=context, question=state.question
        )
        output = await self._chat_model.structured(prompt, schema=SynthesisOutput)
        return {"draft_answer": output.answer}
