"""RouterNode — mechanical correctness with FakeChatModel.

Does NOT test real classification accuracy — that needs a real Ollama call
and lives in scripts/_spot_check_router.py instead (docs/reasoning-
pipeline-design.md §6.3).
"""

from __future__ import annotations

from datetime import date

from acamar.domain.enums import Category, Domain, OutOfScopeReason, QueryIntent
from acamar.reasoning.nodes.router import RouteDecision, RouterNode
from acamar.reasoning.state import QueryState
from tests.fakes import FakeChatModel

TODAY = date(2026, 9, 17)


def _state(question: str) -> QueryState:
    return QueryState(question=question, today=TODAY)


async def test_document_intent_populates_the_filter_fields() -> None:
    decision = RouteDecision(
        intent=QueryIntent.DOCUMENT,
        categories=[Category.MENU],
        date_from=TODAY,
        date_to=TODAY,
    )
    node = RouterNode(FakeChatModel({RouteDecision: decision}))

    update = await node(_state("What's for lunch today?"))

    assert update["intent"] == QueryIntent.DOCUMENT
    assert update["categories"] == [Category.MENU]
    assert update["domains"] is None
    assert update["date_from"] == TODAY
    assert update["date_to"] == TODAY
    assert update["out_of_scope_reason"] is None


async def test_weather_intent_carries_no_category_filter() -> None:
    decision = RouteDecision(intent=QueryIntent.WEATHER)
    node = RouterNode(FakeChatModel({RouteDecision: decision}))

    update = await node(_state("Will it rain today?"))

    assert update["intent"] == QueryIntent.WEATHER
    assert update["categories"] == []


async def test_document_intent_with_no_categories_falls_back_to_all() -> None:
    # A model slip (intent=document but an empty categories list) must not
    # silently search zero rows — that reads as a confidently "grounded"
    # empty answer to Critique, so CRAG's retry loop never catches it.
    decision = RouteDecision(intent=QueryIntent.DOCUMENT, date_from=TODAY, date_to=TODAY)
    node = RouterNode(FakeChatModel({RouteDecision: decision}))

    update = await node(_state("What's happening?"))

    assert update["categories"] == list(Category)


async def test_count_intent_with_no_categories_falls_back_to_all() -> None:
    decision = RouteDecision(intent=QueryIntent.COUNT, date_from=TODAY, date_to=TODAY)
    node = RouterNode(FakeChatModel({RouteDecision: decision}))

    update = await node(_state("How many things do I have?"))

    assert update["categories"] == list(Category)


async def test_count_intent_carries_categories_and_a_date_window() -> None:
    decision = RouteDecision(
        intent=QueryIntent.COUNT,
        categories=[Category.REMINDER],
        date_from=date(2026, 9, 1),
        date_to=date(2026, 9, 30),
    )
    node = RouterNode(FakeChatModel({RouteDecision: decision}))

    update = await node(_state("How many reminders do I have this month?"))

    assert update["intent"] == QueryIntent.COUNT
    assert update["categories"] == [Category.REMINDER]


async def test_out_of_scope_carries_a_reason() -> None:
    decision = RouteDecision(
        intent=QueryIntent.OUT_OF_SCOPE, out_of_scope_reason=OutOfScopeReason.NEVER
    )
    node = RouterNode(FakeChatModel({RouteDecision: decision}))

    update = await node(_state("Can you help me with my daughter's homework?"))

    assert update["intent"] == QueryIntent.OUT_OF_SCOPE
    assert update["out_of_scope_reason"] == OutOfScopeReason.NEVER


async def test_domains_narrows_only_when_the_model_says_so() -> None:
    decision = RouteDecision(
        intent=QueryIntent.DOCUMENT, categories=[Category.EVENT], domains=[Domain.SCHOOL]
    )
    node = RouterNode(FakeChatModel({RouteDecision: decision}))

    update = await node(_state("What's on the school calendar this month?"))

    assert update["domains"] == [Domain.SCHOOL]


async def test_prompt_includes_todays_date_and_the_question() -> None:
    decision = RouteDecision(intent=QueryIntent.WEATHER)
    chat_model = FakeChatModel({RouteDecision: decision})
    node = RouterNode(chat_model)

    await node(_state("Do I need an umbrella?"))

    assert chat_model.calls == [RouteDecision]
    assert "2026-09-17" in chat_model.prompts[0]
    assert "Do I need an umbrella?" in chat_model.prompts[0]
