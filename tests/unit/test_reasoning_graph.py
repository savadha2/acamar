"""The compiled reasoning graph — branch routing and the CRAG retry loop.

All fakes, no real Ollama/Postgres (docs/reasoning-pipeline-design.md §6.2).
The retry-cap test is the single most important one here: an infinite or
excessively long CRAG loop is a real production incident, not just a wrong
answer.
"""

from __future__ import annotations

from datetime import date
from typing import cast
from uuid import uuid4

from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel

from acamar.domain.enums import Category, Domain, OutOfScopeReason, QueryIntent
from acamar.domain.models import Document
from acamar.reasoning.context import RetrievalContext
from acamar.reasoning.graph import build_graph
from acamar.reasoning.nodes.count_answer import CountAnswerNode
from acamar.reasoning.nodes.critique import CritiqueNode, CritiqueVerdict
from acamar.reasoning.nodes.retrieval import RetrievalNode
from acamar.reasoning.nodes.retrieval_grade import RetrievalGrade, RetrievalGradeNode
from acamar.reasoning.nodes.router import RouteDecision, RouterNode
from acamar.reasoning.nodes.synthesis import SynthesisNode, SynthesisOutput
from acamar.reasoning.nodes.weather_answer import WeatherAnswerNode
from acamar.reasoning.state import QueryState
from tests.fakes import (
    FakeChatModel,
    FakeDocumentRepository,
    FakeEmbedder,
    FakeWeatherProvider,
)

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


def _build(
    *,
    chat_responses: dict[type[BaseModel], BaseModel | list[BaseModel]],
    documents: FakeDocumentRepository | None = None,
    weather: FakeWeatherProvider | None = None,
) -> tuple[
    CompiledStateGraph[QueryState, None, QueryState, QueryState],
    FakeChatModel,
    FakeDocumentRepository,
]:
    chat_model = FakeChatModel(chat_responses)
    documents = documents if documents is not None else FakeDocumentRepository()
    embedder = FakeEmbedder()
    context = RetrievalContext(num_ctx=8192)
    graph = build_graph(
        router=RouterNode(chat_model),
        retrieval=RetrievalNode(embedder, documents),
        retrieval_grade=RetrievalGradeNode(chat_model),
        synthesis=SynthesisNode(chat_model, context),
        critique=CritiqueNode(chat_model, context),
        weather_answer=WeatherAnswerNode(weather or FakeWeatherProvider()),
        count_answer=CountAnswerNode(documents),
    )
    return graph, chat_model, documents


_RELEVANT = RetrievalGrade(relevant=True, reason="matches the question")
"""Canned "yes this looks relevant" response — reused by every test below
that isn't specifically exercising RetrievalGradeNode's own retry logic,
so they keep testing what they always tested (Critique's loop, branch
routing) without also having to reason about the new gate."""


def _initial(question: str) -> QueryState:
    return QueryState(question=question, today=TODAY)


async def test_document_branch_reaches_retrieval_synthesis_critique() -> None:
    graph, chat_model, documents = _build(
        chat_responses={
            RouteDecision: RouteDecision(
                intent=QueryIntent.DOCUMENT, categories=[Category.EVENT], date_from=TODAY,
                date_to=TODAY,
            ),
            RetrievalGrade: _RELEVANT,
            SynthesisOutput: SynthesisOutput(answer="The festival is Friday."),
            CritiqueVerdict: CritiqueVerdict(grounded=True, reason="matches the document"),
        },
        documents=FakeDocumentRepository([_doc("Fall Festival")]),
    )

    result = await graph.ainvoke(_initial("what events are coming up?"))

    assert result["final_answer"] == "The festival is Friday."
    assert result["grounded"] is True
    assert len(documents.search_calls) == 1
    assert chat_model.calls == [RouteDecision, RetrievalGrade, SynthesisOutput, CritiqueVerdict]


async def test_weather_branch_never_touches_retrieval_or_chat_model_beyond_router() -> None:
    graph, chat_model, documents = _build(
        chat_responses={RouteDecision: RouteDecision(intent=QueryIntent.WEATHER)},
    )

    result = await graph.ainvoke(_initial("will it rain today?"))

    assert "°F" in result["final_answer"]
    assert result["grounded"] is True
    assert documents.search_calls == []
    assert chat_model.calls == [RouteDecision]  # only Router called an LLM


async def test_count_branch_calls_count_not_search() -> None:
    # _doc()'s documents are dated 2026-09-20 — the window must cover that.
    documents = FakeDocumentRepository([_doc("Permission Slip"), _doc("Fall Festival")])
    graph, chat_model, _ = _build(
        chat_responses={
            RouteDecision: RouteDecision(
                intent=QueryIntent.COUNT, categories=[Category.EVENT], date_from=TODAY,
                date_to=date(2026, 9, 30),
            ),
        },
        documents=documents,
    )

    result = await graph.ainvoke(_initial("how many reminders do I have?"))

    assert result["count_result"] == 2
    assert documents.search_calls == []  # count(), never search()
    assert chat_model.calls == [RouteDecision]


async def test_out_of_scope_branch_declines_without_retrieval_or_synthesis() -> None:
    graph, chat_model, documents = _build(
        chat_responses={
            RouteDecision: RouteDecision(
                intent=QueryIntent.OUT_OF_SCOPE, out_of_scope_reason=OutOfScopeReason.NEVER
            ),
        },
    )

    result = await graph.ainvoke(_initial("can you help with my homework?"))

    assert result["final_answer"] == "That's not something I can help with."
    assert documents.search_calls == []
    assert chat_model.calls == [RouteDecision]


async def test_not_yet_built_reason_gets_a_different_decline_message() -> None:
    graph, _, _ = _build(
        chat_responses={
            RouteDecision: RouteDecision(
                intent=QueryIntent.OUT_OF_SCOPE,
                out_of_scope_reason=OutOfScopeReason.NOT_YET_BUILT,
            ),
        },
    )

    result = await graph.ainvoke(_initial("is daycare closed tomorrow?"))

    assert "planned for later" in result["final_answer"]


async def test_ungrounded_first_attempt_retries_with_a_widened_filter_then_succeeds() -> None:
    graph, _chat_model, documents = _build(
        chat_responses={
            RouteDecision: RouteDecision(
                intent=QueryIntent.DOCUMENT, categories=[Category.EVENT], date_from=TODAY,
                date_to=TODAY,
            ),
            RetrievalGrade: _RELEVANT,
            SynthesisOutput: [
                SynthesisOutput(answer="I don't know."),
                SynthesisOutput(answer="The festival is Friday."),
            ],
            CritiqueVerdict: [
                CritiqueVerdict(grounded=False, reason="no supporting document"),
                CritiqueVerdict(grounded=True, reason="matches the document"),
            ],
        },
        documents=FakeDocumentRepository([_doc("Fall Festival")]),
    )

    result = await graph.ainvoke(_initial("what events are coming up?"))

    assert result["grounded"] is True
    assert result["final_answer"] == "The festival is Friday."
    assert len(documents.search_calls) == 2
    # attempt 1's window is wider than attempt 0's (both TODAY..TODAY).
    first, second = documents.search_calls
    assert cast("date", second["date_from"]) < cast("date", first["date_from"])
    assert cast("date", second["date_to"]) > cast("date", first["date_to"])


async def test_retry_cap_is_reached_not_an_infinite_loop() -> None:
    # Ungrounded every single time — must still terminate at MAX_RETRIES,
    # not loop forever or raise.
    graph, _chat_model, documents = _build(
        chat_responses={
            RouteDecision: RouteDecision(
                intent=QueryIntent.DOCUMENT, categories=[Category.EVENT], date_from=TODAY,
                date_to=TODAY,
            ),
            RetrievalGrade: _RELEVANT,
            SynthesisOutput: [SynthesisOutput(answer="I don't know.") for _ in range(5)],
            CritiqueVerdict: [
                CritiqueVerdict(grounded=False, reason="no supporting document") for _ in range(5)
            ],
        },
    )

    result = await graph.ainvoke(_initial("what events are coming up?"))

    assert result["grounded"] is False
    assert result["final_answer"] == "I don't know."
    assert len(documents.search_calls) == 3  # attempts 0, 1, 2 - then stops
    # attempt 2 dropped the category filter entirely.
    assert documents.search_calls[2]["categories"] == list(Category)


async def test_domains_pass_through_to_every_retrieval_attempt() -> None:
    graph, _chat_model, documents = _build(
        chat_responses={
            RouteDecision: RouteDecision(
                intent=QueryIntent.DOCUMENT, categories=[Category.EVENT], domains=[Domain.SCHOOL],
                date_from=TODAY, date_to=TODAY,
            ),
            RetrievalGrade: _RELEVANT,
            SynthesisOutput: SynthesisOutput(answer="x"),
            CritiqueVerdict: CritiqueVerdict(grounded=True, reason="ok"),
        },
    )

    await graph.ainvoke(_initial("what's on the school calendar?"))

    assert documents.search_calls[0]["domains"] == [Domain.SCHOOL]


async def test_a_wrong_router_guess_degrades_gracefully_not_catastrophically() -> None:
    # Decision F's whole premise: a wrong DOCUMENT guess for an
    # out-of-scope-shaped question should resolve to an honest "I don't
    # know", not an exception or a fabricated answer.
    honest_no_answer = SynthesisOutput(answer="I don't have information about that.")
    empty_corpus = FakeDocumentRepository([])  # nothing to retrieve for a mis-routed question
    graph, _, _documents = _build(
        chat_responses={
            RouteDecision: RouteDecision(
                intent=QueryIntent.DOCUMENT, categories=[Category.EVENT], date_from=TODAY,
                date_to=TODAY,
            ),
            RetrievalGrade: _RELEVANT,
            SynthesisOutput: honest_no_answer,
            CritiqueVerdict: CritiqueVerdict(grounded=True, reason="an honest non-answer"),
        },
        documents=empty_corpus,
    )

    result = await graph.ainvoke(_initial("who's driving carpool?"))

    assert result["final_answer"] == "I don't have information about that."
    assert result["grounded"] is True


# ---------------------------------------------------------------------------
# RetrievalGradeNode's own gate (2026-09-24) — canonical CRAG's actual
# mechanism: catch a bad retrieval *before* Synthesis ever runs on it,
# rather than only after, and only if the honest resulting non-answer
# happens not to look "grounded" to Critique (real bug: it always does).
# ---------------------------------------------------------------------------


async def test_not_relevant_retrieval_retries_before_ever_synthesizing() -> None:
    # The actual fix: an irrelevant attempt-0 retrieval must never reach
    # Synthesis at all — unlike the old critique-only design, which could
    # only notice a bad retrieval after wasting a synthesis call on it,
    # and even then only when the honest result didn't read as "grounded".
    graph, chat_model, documents = _build(
        chat_responses={
            RouteDecision: RouteDecision(
                intent=QueryIntent.DOCUMENT, categories=[Category.EVENT], date_from=TODAY,
                date_to=TODAY,
            ),
            RetrievalGrade: [
                RetrievalGrade(relevant=False, reason="nothing found"),
                RetrievalGrade(relevant=True, reason="found the festival"),
            ],
            SynthesisOutput: SynthesisOutput(answer="The festival is Friday."),
            CritiqueVerdict: CritiqueVerdict(grounded=True, reason="matches the document"),
        },
        documents=FakeDocumentRepository([_doc("Fall Festival")]),  # event_date 2026-09-20
    )

    result = await graph.ainvoke(_initial("what events are coming up?"))

    assert result["grounded"] is True
    assert result["final_answer"] == "The festival is Friday."
    # attempt 0 (Sept 17-17, misses the Sept 20 doc), attempt 1 (widened, finds it)
    assert len(documents.search_calls) == 2
    assert chat_model.calls == [
        RouteDecision,
        RetrievalGrade,
        RetrievalGrade,
        SynthesisOutput,  # exactly once — never wasted on the irrelevant attempt 0
        CritiqueVerdict,
    ]


async def test_retrieval_grade_retry_cap_is_reached_not_an_infinite_loop() -> None:
    # Never relevant, ever — must still terminate at MAX_RETRIES retrieval
    # attempts and fall through to Synthesis/Critique exactly once at the
    # end with a best-effort answer, not loop forever or raise.
    graph, chat_model, documents = _build(
        chat_responses={
            RouteDecision: RouteDecision(
                intent=QueryIntent.DOCUMENT, categories=[Category.EVENT], date_from=TODAY,
                date_to=TODAY,
            ),
            RetrievalGrade: [RetrievalGrade(relevant=False, reason="nope") for _ in range(5)],
            SynthesisOutput: SynthesisOutput(answer="I don't have information about that."),
            CritiqueVerdict: CritiqueVerdict(grounded=True, reason="an honest non-answer"),
        },
    )

    result = await graph.ainvoke(_initial("what events are coming up?"))

    assert result["final_answer"] == "I don't have information about that."
    assert len(documents.search_calls) == 3  # attempts 0, 1, 2 — then stops, same cap as Critique's
    assert documents.search_calls[2]["categories"] == list(Category)  # final attempt still widened
    assert chat_model.calls.count(SynthesisOutput) == 1  # only reached once, on the fallthrough


async def test_retrieval_grade_and_critique_share_one_retry_budget() -> None:
    # Two different gates (RetrievalGrade before Synthesis, Critique after)
    # draw from the *same* retry_count/MAX_RETRIES — this is what stops
    # them from silently doubling the real retry cap (e.g. 3 + 3 attempts
    # instead of 3 total) if both happen to fire in the same run.
    graph, _chat_model, documents = _build(
        chat_responses={
            RouteDecision: RouteDecision(
                intent=QueryIntent.DOCUMENT, categories=[Category.EVENT], date_from=TODAY,
                date_to=TODAY,
            ),
            RetrievalGrade: [
                RetrievalGrade(relevant=False, reason="attempt 0 empty"),  # bumps retry_count to 1
                RetrievalGrade(relevant=True, reason="attempt 1 found it"),
                RetrievalGrade(relevant=True, reason="attempt 2 found it"),
            ],
            SynthesisOutput: [
                SynthesisOutput(answer="Draft 1."),
                SynthesisOutput(answer="Final answer."),
            ],
            CritiqueVerdict: [
                CritiqueVerdict(grounded=False, reason="not quite"),  # bumps retry_count to 2
                CritiqueVerdict(grounded=True, reason="good now"),
            ],
        },
        documents=FakeDocumentRepository([_doc("Fall Festival")]),  # event_date 2026-09-20
    )

    result = await graph.ainvoke(_initial("what events are coming up?"))

    assert result["grounded"] is True
    assert result["final_answer"] == "Final answer."
    # 1 retry from RetrievalGrade + 1 from Critique, capped at 3 total attempts
    assert len(documents.search_calls) == 3
