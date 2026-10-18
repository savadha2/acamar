"""RetrievalGradeNode — relevant/reason pass through; retry_count only
bumps on not-relevant. The retry *cap* itself is a graph-level concern
(test_reasoning_graph.py), not this node's — this node just always bumps
when not relevant, unconditionally.
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

from acamar.domain.enums import Category, Domain
from acamar.domain.models import Document
from acamar.reasoning.nodes.retrieval_grade import RetrievalGrade, RetrievalGradeNode
from acamar.reasoning.state import QueryState
from tests.fakes import FakeChatModel

TODAY = date(2026, 9, 24)


def _doc(title: str) -> Document:
    return Document(
        id=uuid4(),
        source_id=uuid4(),
        domain=Domain.GMAIL,
        category=Category.EVENT,
        student_ref=None,
        title=title,
        content=f"content about {title}",
        headline=None,
        summary=None,
        is_relevant=True,
        organization=None,
        external_id=title,
        event_date=date(2026, 9, 26),
        published_at=None,
    )


def _state(*, retrieved: list[Document] | None = None, retry_count: int = 0) -> QueryState:
    return QueryState(
        question="what is the Noetic Math contest?",
        today=TODAY,
        retrieved=retrieved or [],
        retry_count=retry_count,
    )


async def test_relevant_verdict_does_not_bump_retry_count() -> None:
    verdict = RetrievalGrade(relevant=True, reason="the contest doc is right here")
    node = RetrievalGradeNode(FakeChatModel({RetrievalGrade: verdict}))

    update = await node(_state(retrieved=[_doc("Noetic Math Contest")], retry_count=0))

    assert update["retrieval_relevant"] is True
    assert "retry_count" not in update


async def test_not_relevant_verdict_bumps_retry_count() -> None:
    verdict = RetrievalGrade(relevant=False, reason="nothing about the contest")
    node = RetrievalGradeNode(FakeChatModel({RetrievalGrade: verdict}))

    update = await node(_state(retrieved=[_doc("Unrelated Event")], retry_count=0))

    assert update["retrieval_relevant"] is False
    assert update["retry_count"] == 1


async def test_not_relevant_verdict_bumps_from_the_current_retry_count_not_from_zero() -> None:
    verdict = RetrievalGrade(relevant=False, reason="still nothing")
    node = RetrievalGradeNode(FakeChatModel({RetrievalGrade: verdict}))

    update = await node(_state(retry_count=1))

    assert update["retry_count"] == 2


async def test_empty_retrieval_is_gradeable_not_a_special_case() -> None:
    verdict = RetrievalGrade(relevant=False, reason="no documents were found")
    node = RetrievalGradeNode(FakeChatModel({RetrievalGrade: verdict}))

    update = await node(_state(retrieved=[]))

    assert update["retrieval_relevant"] is False
    assert update["retry_count"] == 1


async def test_prompt_includes_the_question_and_document_titles() -> None:
    verdict = RetrievalGrade(relevant=True, reason="ok")
    chat_model = FakeChatModel({RetrievalGrade: verdict})
    node = RetrievalGradeNode(chat_model)

    await node(_state(retrieved=[_doc("Noetic Math Contest")]))

    assert "what is the Noetic Math contest?" in chat_model.prompts[0]
    assert "Noetic Math Contest" in chat_model.prompts[0]


async def test_prompt_says_no_documents_were_found_when_retrieval_was_empty() -> None:
    verdict = RetrievalGrade(relevant=False, reason="empty")
    chat_model = FakeChatModel({RetrievalGrade: verdict})
    node = RetrievalGradeNode(chat_model)

    await node(_state(retrieved=[]))

    assert "no documents were found" in chat_model.prompts[0]


async def test_uses_headline_over_title_when_present() -> None:
    doc = _doc("Registration Opening Soon: Faria Fall 2026 Noetic Math Contest").model_copy(
        update={"headline": "Noetic Math Contest: Register Sept 26"}
    )
    verdict = RetrievalGrade(relevant=True, reason="ok")
    chat_model = FakeChatModel({RetrievalGrade: verdict})
    node = RetrievalGradeNode(chat_model)

    await node(_state(retrieved=[doc]))

    assert "Noetic Math Contest: Register Sept 26" in chat_model.prompts[0]
