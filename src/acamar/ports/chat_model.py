"""The structured-chat port. The core never calls a chat model directly.

One capability, many callers: Router/Synthesis/Critique/Judge are four
different calls with different prompts and schemas, but the same shape —
send a prompt, get back one parsed pydantic object. Mirrors
`Embedder`/`Enricher` (one port, one adapter, many call sites), not one
port per node. See docs/reasoning-pipeline-design.md §2.2.
"""

from __future__ import annotations

from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class ChatModelError(RuntimeError):
    """The chat model was unreachable or returned an unusable result."""


class ChatModel(Protocol):
    """Turns one prompt into one structured, typed response."""

    async def structured(self, prompt: str, *, schema: type[T]) -> T:
        """Return ``schema``-validated structured output for ``prompt``.

        Raises:
            ChatModelError: the model failed or returned something that
                doesn't validate against ``schema``.
        """
        ...
