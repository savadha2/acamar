"""One-off: run RouterNode against real gemma3:1b for all 31 worked
questions from docs/reasoning-pipeline-design.md §2.8/§2.9, and print
expected vs. actual side by side for a human to eyeball before trusting
Router inside the full graph (§4 step 3, §6.3).

    uv run python -m scripts._spot_check_router
"""

from __future__ import annotations

import asyncio
from datetime import date

from acamar.adapters.llm.ollama_chat_model import OllamaChatModel
from acamar.config import get_settings
from acamar.domain.enums import QueryIntent
from acamar.reasoning.nodes.router import RouterNode
from acamar.reasoning.state import QueryState

_TODAY = date(2026, 9, 17)

# (question, expected_intent) — categories/domains aren't checked here,
# just the coarse intent, since that's the riskiest classification.
_QUESTIONS: list[tuple[str, QueryIntent]] = [
    ("What's for lunch today?", QueryIntent.DOCUMENT),
    ("What's on the menu this week?", QueryIntent.DOCUMENT),
    ("What events are coming up this month?", QueryIntent.DOCUMENT),
    ("Is there a Fall Festival this year?", QueryIntent.DOCUMENT),
    ("When is the next FEEF meeting?", QueryIntent.DOCUMENT),
    ("Do I need to sign anything this week?", QueryIntent.DOCUMENT),
    ("Are there any permission slips due soon?", QueryIntent.DOCUMENT),
    ("When is the next school holiday?", QueryIntent.DOCUMENT),
    ("Is there school tomorrow?", QueryIntent.DOCUMENT),
    ("What time does the Fall Festival start?", QueryIntent.DOCUMENT),
    ("What's happening this week?", QueryIntent.DOCUMENT),
    ("Anything I need to do before Friday?", QueryIntent.DOCUMENT),
    ("Will it rain today?", QueryIntent.WEATHER),
    ("Do I need to send an umbrella?", QueryIntent.WEATHER),
    ("Is it going to be hot this weekend?", QueryIntent.WEATHER),
    ("What activities are there for kids this weekend?", QueryIntent.DOCUMENT),
    ("Are there any camps during winter break?", QueryIntent.DOCUMENT),
    ("Is there a Parks and Rec event in Cupertino this weekend?", QueryIntent.DOCUMENT),
    ("Can you help me with my daughter's homework?", QueryIntent.OUT_OF_SCOPE),
    ("What's the weather like in Hawaii next week?", QueryIntent.OUT_OF_SCOPE),
    ("Where can I buy art material for my daughter's project?", QueryIntent.OUT_OF_SCOPE),
    (
        "We want to do a stars and constellation project for social studies — "
        "where can I find the raw material for the project board?",
        QueryIntent.OUT_OF_SCOPE,
    ),
    ("Who's driving carpool for the field trip?", QueryIntent.OUT_OF_SCOPE),
    ("How's my daughter doing in math this year?", QueryIntent.OUT_OF_SCOPE),
    ("Can you remind me about the festival tomorrow?", QueryIntent.OUT_OF_SCOPE),
    ("How many reminders do I have this month?", QueryIntent.COUNT),
    ("How much do I owe for the field trip?", QueryIntent.OUT_OF_SCOPE),
    ("How do I reset my password on the daycare portal?", QueryIntent.OUT_OF_SCOPE),
    ("Is daycare closed for Labor Day?", QueryIntent.OUT_OF_SCOPE),
    ("What's my daughter's teacher's email?", QueryIntent.DOCUMENT),
    ("How do I report that my daughter will be absent today?", QueryIntent.DOCUMENT),
]


async def main() -> None:
    settings = get_settings()
    router = RouterNode(OllamaChatModel(model=settings.ollama_router_model))

    correct = 0
    for question, expected in _QUESTIONS:
        state = QueryState(question=question, today=_TODAY)
        try:
            update = await router(state)
            actual = update["intent"]
        except Exception as exc:  # noqa: BLE001 - spot-check script, report and keep going
            actual = f"ERROR: {exc}"
        mark = "OK" if actual == expected else "MISMATCH"
        correct += actual == expected
        print(f"[{mark:>8}] expected={expected.value:<12} actual={actual!s:<12} {question}")

    print(f"\n{correct}/{len(_QUESTIONS)} matched expected intent ({settings.ollama_router_model})")


if __name__ == "__main__":
    asyncio.run(main())
