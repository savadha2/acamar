"""SynthesisNode — context assembly reaches the model, output passes through."""

from __future__ import annotations

from datetime import date
from uuid import uuid4

from acamar.domain.enums import Category, Domain
from acamar.domain.models import Document
from acamar.reasoning.nodes.synthesis import SynthesisNode, SynthesisOutput
from acamar.reasoning.state import QueryState
from tests.fakes import FakeChatModel

TODAY = date(2026, 9, 17)


def _doc(title: str) -> Document:
    return Document(
        id=uuid4(),
        source_id=uuid4(),
        domain=Domain.SCHOOL,
        category=Category.EVENT,
        student_ref=None,
        title=title,
        content=f"content about {title}",
        headline=None,
        summary=None,
        is_relevant=True,
        organization=None,
        external_id=title,
        event_date=date(2026, 9, 20),
        published_at=None,
    )


def _state(retrieved: list[Document]) -> QueryState:
    return QueryState(question="what's happening this week?", today=TODAY, retrieved=retrieved)


async def test_draft_answer_passes_through_unchanged() -> None:
    output = SynthesisOutput(answer="The Fall Festival is Friday.")
    node = SynthesisNode(FakeChatModel({SynthesisOutput: output}))

    update = await node(_state([_doc("Fall Festival")]))

    assert update["draft_answer"] == "The Fall Festival is Friday."


async def test_an_honest_no_information_answer_passes_through_unchanged() -> None:
    output = SynthesisOutput(answer="I don't have information about that.")
    node = SynthesisNode(FakeChatModel({SynthesisOutput: output}))

    update = await node(_state([]))

    assert update["draft_answer"] == "I don't have information about that."


async def test_prompt_includes_the_question_and_todays_date() -> None:
    output = SynthesisOutput(answer="x")
    chat_model = FakeChatModel({SynthesisOutput: output})
    node = SynthesisNode(chat_model)

    await node(_state([_doc("Fall Festival")]))

    assert "what's happening this week?" in chat_model.prompts[0]
    assert "2026-09-17" in chat_model.prompts[0]


async def test_prompt_says_no_relevant_documents_when_retrieval_was_empty() -> None:
    output = SynthesisOutput(answer="x")
    chat_model = FakeChatModel({SynthesisOutput: output})
    node = SynthesisNode(chat_model)

    await node(_state([]))

    assert "no relevant documents found" in chat_model.prompts[0]


async def test_retrieved_document_content_reaches_the_prompt() -> None:
    output = SynthesisOutput(answer="x")
    chat_model = FakeChatModel({SynthesisOutput: output})
    node = SynthesisNode(chat_model)

    await node(_state([_doc("Fall Festival")]))

    assert "Fall Festival" in chat_model.prompts[0]
