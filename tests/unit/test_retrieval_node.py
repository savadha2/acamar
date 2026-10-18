"""RetrievalNode — the retry-widening table, via FakeDocumentRepository's
recorded call args (docs/reasoning-pipeline-design.md §2.4/§6.1).
"""

from __future__ import annotations

from datetime import date
from typing import cast
from uuid import uuid4

from acamar.domain.enums import Category, Domain
from acamar.domain.models import Document
from acamar.reasoning.nodes.retrieval import RetrievalNode
from acamar.reasoning.state import QueryState
from tests.fakes import FakeDocumentRepository, FakeEmbedder

TODAY = date(2026, 9, 17)


def _doc() -> Document:
    return Document(
        id=uuid4(),
        source_id=uuid4(),
        domain=Domain.SCHOOL,
        category=Category.EVENT,
        student_ref=None,
        title="Fall Festival",
        content="content about Fall Festival",
        headline=None,
        summary=None,
        is_relevant=True,
        organization=None,
        external_id="fall-festival",
        event_date=date(2026, 9, 20),
        published_at=None,
    )


def _window(call: dict[str, object]) -> tuple[date, date]:
    return cast("date", call["date_from"]), cast("date", call["date_to"])


def _state(*, retry_count: int = 0) -> QueryState:
    return QueryState(
        question="what's happening this week?",
        today=TODAY,
        categories=[Category.EVENT],
        domains=[Domain.SCHOOL],
        date_from=date(2026, 9, 15),
        date_to=date(2026, 9, 21),
        retry_count=retry_count,
    )


async def test_attempt_0_uses_routers_own_filter_unchanged() -> None:
    documents = FakeDocumentRepository()
    node = RetrievalNode(FakeEmbedder(), documents)

    await node(_state(retry_count=0))

    call = documents.search_calls[0]
    assert call["categories"] == [Category.EVENT]
    assert call["domains"] == [Domain.SCHOOL]
    assert call["date_from"] == date(2026, 9, 15)
    assert call["date_to"] == date(2026, 9, 21)
    assert call["limit"] == 8


async def test_attempt_1_widens_the_date_window_keeps_categories() -> None:
    documents = FakeDocumentRepository()
    node = RetrievalNode(FakeEmbedder(), documents)

    await node(_state(retry_count=1))

    call = documents.search_calls[0]
    widened_from, widened_to = _window(call)
    assert call["categories"] == [Category.EVENT]  # unchanged from attempt 0
    assert widened_from < date(2026, 9, 15)  # widened earlier
    assert widened_to > date(2026, 9, 21)  # widened later
    assert call["limit"] == 8


async def test_final_attempt_drops_category_filter_and_raises_the_limit() -> None:
    documents = FakeDocumentRepository()
    node = RetrievalNode(FakeEmbedder(), documents)

    await node(_state(retry_count=2))

    call = documents.search_calls[0]
    widened_from, widened_to = _window(call)
    assert call["categories"] == list(Category)  # every category, not just EVENT
    assert call["domains"] is None  # reset too — see test below for why this matters
    assert widened_from < date(2026, 9, 15)
    assert widened_to > date(2026, 9, 21)
    assert call["limit"] == 12


async def test_widening_is_not_cumulative_across_attempts() -> None:
    # Attempt 2's window is computed fresh from the *original* Router
    # window, not from attempt 1's already-widened one.
    documents = FakeDocumentRepository()
    node = RetrievalNode(FakeEmbedder(), documents)

    await node(_state(retry_count=1))
    await node(_state(retry_count=2))

    assert _window(documents.search_calls[0]) == _window(documents.search_calls[1])


async def test_domains_pass_through_unchanged_on_the_non_final_attempts() -> None:
    documents = FakeDocumentRepository()
    node = RetrievalNode(FakeEmbedder(), documents)

    for retry_count in (0, 1):
        await node(_state(retry_count=retry_count))

    assert all(call["domains"] == [Domain.SCHOOL] for call in documents.search_calls)


async def test_final_attempt_resets_domains_too_not_just_categories() -> None:
    # Real bug (2026-09-24): the final attempt already widens categories to
    # `list(Category)` as a safety net against a bad Router guess, but used
    # to leave `domains` untouched — so a wrongly-narrowed domain (Router
    # guessing `[school]` for a question actually answered by a
    # `gmail`-sourced email) meant retrieval could return zero/wrong
    # results no matter how many retries ran, since nothing ever widened
    # the domain filter back to "search everywhere."
    documents = FakeDocumentRepository()
    node = RetrievalNode(FakeEmbedder(), documents)

    await node(_state(retry_count=2))

    assert documents.search_calls[0]["domains"] is None


async def test_embeds_the_question_exactly_once() -> None:
    embedder = FakeEmbedder()
    node = RetrievalNode(embedder, FakeDocumentRepository())

    await node(_state())

    assert embedder.calls == [["what's happening this week?"]]


async def test_same_day_window_still_widens_meaningfully_on_retry() -> None:
    # A single-day window (e.g. "today") has zero width — the widen pad
    # must have a floor, not `width // 2 == 0`.
    documents = FakeDocumentRepository()
    node = RetrievalNode(FakeEmbedder(), documents)
    state = QueryState(
        question="what's for lunch today?",
        today=TODAY,
        categories=[Category.MENU],
        date_from=TODAY,
        date_to=TODAY,
        retry_count=1,
    )

    await node(state)

    widened_from, widened_to = _window(documents.search_calls[0])
    assert widened_from < TODAY
    assert widened_to > TODAY


async def test_dismissed_documents_are_excluded_from_results() -> None:
    # A document the user marked "done" on the dashboard shouldn't resurface
    # as an Ask-box citation — search() must exclude it the same way
    # count()/list_documents() already do (a real gap: the real _SEARCH SQL
    # was missing `dismissed_at IS NULL` until this was found in review).
    doc = _doc()
    documents = FakeDocumentRepository([doc])
    await documents.dismiss(doc.id)
    node = RetrievalNode(FakeEmbedder(), documents)

    update = await node(
        QueryState(
            question="what events are coming up?",
            today=TODAY,
            categories=[Category.EVENT],
        )
    )

    assert update["retrieved"] == []
