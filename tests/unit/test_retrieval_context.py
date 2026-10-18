"""RetrievalContext — truncates to a token budget, always keeps the first doc."""

from __future__ import annotations

from datetime import date
from uuid import uuid4

from acamar.domain.enums import Category, Domain
from acamar.domain.models import Document
from acamar.reasoning.context import RetrievalContext


def _doc(title: str, content: str) -> Document:
    return Document(
        id=uuid4(),
        source_id=uuid4(),
        domain=Domain.SCHOOL,
        category=Category.EVENT,
        student_ref=None,
        title=title,
        content=content,
        headline=None,
        summary=None,
        is_relevant=True,
        organization=None,
        external_id=title,
        event_date=date(2026, 9, 20),
        published_at=None,
    )


def test_empty_documents_gives_empty_context() -> None:
    context = RetrievalContext(num_ctx=8192)
    assert context.build([]) == ""


def test_all_documents_fit_within_a_generous_budget() -> None:
    context = RetrievalContext(num_ctx=8192)
    docs = [_doc("A", "short"), _doc("B", "also short")]

    built = context.build(docs)

    assert "A" in built
    assert "B" in built


def test_truncates_once_the_budget_is_exceeded() -> None:
    # num_ctx=1024 (reserved) + 50 tokens budget = 200 chars (4 chars/token).
    context = RetrievalContext(num_ctx=1024 + 50)
    docs = [_doc("A", "x" * 80), _doc("B", "y" * 80), _doc("C", "z" * 80)]

    built = context.build(docs)

    assert "A" in built
    assert "C" not in built  # the third document pushed past the 200-char budget


def test_first_document_is_kept_even_if_it_alone_exceeds_the_budget() -> None:
    context = RetrievalContext(num_ctx=1024 + 10)  # 40-char budget
    docs = [_doc("Huge", "x" * 500)]

    built = context.build(docs)

    assert "Huge" in built  # an over-long single match beats an empty context


def test_prefers_summary_over_content_when_present() -> None:
    context = RetrievalContext(num_ctx=8192)
    doc = _doc("Fall Festival", "raw scraped content")
    doc = doc.model_copy(update={"summary": "short gloss"})

    built = context.build([doc])

    assert "short gloss" in built
    assert "raw scraped content" not in built
