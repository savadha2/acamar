"""Out-of-scope branch — a fixed decline, not a model call at all.

A plain function, not a class: no injected dependency, a pure transform of
state (architecture.md §2.2 — classes for state/swappable implementations,
functions for pure transforms). Templated by `out_of_scope_reason`
(Decision H) so "not yet, but planned" reads differently from "never will".
"""

from __future__ import annotations

from typing import Any

from acamar.domain.enums import OutOfScopeReason
from acamar.reasoning.state import QueryState

_NEVER = "That's not something I can help with."
_NOT_YET_BUILT = "I don't have that information yet, but it's planned for later."


async def decline_node(state: QueryState) -> dict[str, Any]:
    """Return `{"final_answer": ...}`, worded by `state.out_of_scope_reason`."""
    message = (
        _NOT_YET_BUILT if state.out_of_scope_reason == OutOfScopeReason.NOT_YET_BUILT else _NEVER
    )
    return {"final_answer": message, "grounded": True}
