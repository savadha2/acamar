"""CritiqueNode — grounded/reason pass through; retry_count only bumps on
ungrounded. The retry *cap* itself is a graph-level concern (§6.2), not
this node's — this node just always bumps when ungrounded, unconditionally.
"""

from __future__ import annotations

from datetime import date

from acamar.reasoning.nodes.critique import CritiqueNode, CritiqueVerdict
from acamar.reasoning.state import QueryState
from tests.fakes import FakeChatModel

TODAY = date(2026, 9, 17)


def _state(*, retry_count: int = 0, draft_answer: str = "The festival is Friday.") -> QueryState:
    return QueryState(
        question="when is the festival?", today=TODAY, draft_answer=draft_answer,
        retry_count=retry_count,
    )


async def test_grounded_verdict_does_not_bump_retry_count() -> None:
    verdict = CritiqueVerdict(grounded=True, reason="matches the retrieved event")
    node = CritiqueNode(FakeChatModel({CritiqueVerdict: verdict}))

    update = await node(_state(retry_count=0))

    assert update["grounded"] is True
    assert "retry_count" not in update


async def test_ungrounded_verdict_bumps_retry_count() -> None:
    verdict = CritiqueVerdict(grounded=False, reason="no supporting document")
    node = CritiqueNode(FakeChatModel({CritiqueVerdict: verdict}))

    update = await node(_state(retry_count=0))

    assert update["grounded"] is False
    assert update["retry_count"] == 1


async def test_ungrounded_verdict_bumps_from_the_current_retry_count_not_from_zero() -> None:
    verdict = CritiqueVerdict(grounded=False, reason="still not grounded")
    node = CritiqueNode(FakeChatModel({CritiqueVerdict: verdict}))

    update = await node(_state(retry_count=1))

    assert update["retry_count"] == 2


async def test_reason_passes_through() -> None:
    verdict = CritiqueVerdict(grounded=False, reason="the date doesn't match any document")
    node = CritiqueNode(FakeChatModel({CritiqueVerdict: verdict}))

    update = await node(_state())

    assert update["critique_reason"] == "the date doesn't match any document"


async def test_an_honest_no_information_draft_is_treated_as_gradeable_not_special_cased() -> None:
    # The prompt instructs the model to treat "I don't know" as grounded —
    # this test just confirms the node doesn't second-guess whatever
    # verdict comes back (no node-level override logic to get wrong).
    verdict = CritiqueVerdict(grounded=True, reason="an honest non-answer, not a fabrication")
    node = CritiqueNode(FakeChatModel({CritiqueVerdict: verdict}))

    update = await node(_state(draft_answer="I don't have information about that."))

    assert update["grounded"] is True
