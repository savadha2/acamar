"""Router — the intent + document-filter classifier.

Classifies a question into an intent, and (for DOCUMENT questions) a
category/domain/date filter for Retrieval. Two-level decision, not one:
an earlier version of this node only picked a category, which silently
assumed every question is a document question. Weather has no matching
document, ever; some questions aren't answerable by this system at all.
See docs/reasoning-pipeline-design.md §2.3/§2.4/Decisions C/F/G/H — the
worked examples in §2.8/§2.9 are this prompt's few-shot source, not just
documentation.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel

from acamar.domain.enums import Category, Domain, OutOfScopeReason, QueryIntent
from acamar.ports.chat_model import ChatModel
from acamar.reasoning.state import QueryState

_CATEGORY_GUIDE = """\
- "event": something happening on a specific date (a festival, a meeting,
  a field trip).
- "menu": what's being served, breakfast/lunch.
- "reminder": a deadline, a form, a task, or standing school
  policy/procedure with no specific event date.
- "holiday": a no-school date.
- "activity": an out-of-school activity/program for kids (camps, Parks &
  Rec) — may have no data yet, route here anyway."""

_EXAMPLES = """\
Examples:
- "What's for lunch today?" -> intent=document, categories=[menu],
  date_from=date_to=today
- "What's happening this week?" -> intent=document,
  categories=[event, reminder, holiday], date window = this week
- "Will it rain today?" -> intent=weather
- "How many reminders do I have this month?" -> intent=count,
  categories=[reminder], date window = this month
- "Where can I buy art material for my daughter's project?" ->
  intent=out_of_scope, out_of_scope_reason=never
- "We want to do a stars and constellation project for social studies,
  where can I find the raw material for the project board?" ->
  intent=out_of_scope, out_of_scope_reason=never (even though it names a
  real project - it's still asking where to buy something)
- "Is daycare closed for Labor Day?" -> intent=out_of_scope,
  out_of_scope_reason=not_yet_built
- "Can you remind me about the festival tomorrow?" -> intent=out_of_scope,
  out_of_scope_reason=never (this asks the assistant to DO something, not
  answer a question)"""

_PROMPT = f"""\
You are routing a parent's question to the right part of a
school-logistics assistant.

Today's date: {{today}} (use this for any relative date in the question -
"this week", "tomorrow", "next Friday").

First decide the intent:
- "document": the question is about WHEN/WHAT/WHETHER something school- or
  daycare-related is happening, due, or true. Route here even if you don't
  know whether a matching document actually exists - a zero-result answer
  is fine and expected.
- "weather": the question is only about weather (rain, temperature, what
  to wear because of weather) AND does not name a different city/location
  - this assistant only knows the weather for its one household's location.
  A question naming another city ("weather in Hawaii") is out_of_scope,
  not weather.
- "count": the question asks for an exact NUMBER of things ("how many
  reminders", "how many events are left") - never guess a count from
  "document", always use "count" for a "how many" question.
- "out_of_scope": the question asks for a KIND of information this
  assistant's data could never contain, no matter how it's answered -
  where to buy something, how to make/craft something, general knowledge,
  homework help, another family's schedule, a child's grades. This is
  about the ASK TYPE, not whether the question mentions something real:
  "where do I buy materials for the stars project" is still out_of_scope
  even if that project is a real assignment, because no school email ever
  contains a store recommendation.

When uncertain between "document" and "out_of_scope", prefer "document" -
a wrong document guess safely resolves to "I don't know"; a wrong decline
never even tries.

If out_of_scope, also set out_of_scope_reason:
- "not_yet_built": the topic is daycare, financial (tuition/payments), or
  a product/account manual question - these are planned, just not built
  yet.
- "never": anything else out of scope (homework help, another family's
  schedule, general knowledge, shopping/how-to, asking the assistant to DO
  something like set a reminder).

If intent is "document", also set:
- categories (1 or more):
{_CATEGORY_GUIDE}
  A broad question ("what's happening this week") should include several
  categories, not just one.
- domains: leave null (search everywhere) unless the question specifically
  names a source ("on the school calendar" vs. some other place) - this is
  rare, default to null.
- date_from / date_to: the date window implied by the question, computed
  from today's date above. A question with no clear window ("is there a
  Fall Festival this year") can use a wide window. Do NOT default to
  "today" for a question asking WHEN something is due/happening ("when is
  X due", "is there a Y project") - the whole point of that question is to
  discover a date you don't know yet, so it needs a WIDE window (e.g.
  several months out), not just today. When a deadline/project/task could
  plausibly be filed as either "event" or "reminder", include both rather
  than guessing one.

{_EXAMPLES}

Question: {{question}}
"""


class RouteDecision(BaseModel):
    """Structured output of one Router call."""

    intent: QueryIntent
    categories: list[Category] = []
    domains: list[Domain] | None = None
    date_from: date | None = None
    date_to: date | None = None
    out_of_scope_reason: OutOfScopeReason | None = None


_CATEGORY_REQUIRED_INTENTS = frozenset({QueryIntent.DOCUMENT, QueryIntent.COUNT})
"""Intents where an empty `categories` list is a model slip, not a real
answer. Unlike `domains: None` (a deliberate "search everywhere"),
`category = ANY('{}')` matches zero rows by construction — a DOCUMENT/COUNT
question with no categories would silently produce an empty result instead
of a real one, and (for DOCUMENT) that empty result reads as a *grounded*
"I don't know" to Critique, so the CRAG retry loop never fires to correct
it. Degrade the same way the final retry attempt already does: search every
category rather than none (Decision F's "wrong guess should still recover"
philosophy, extended to this case)."""


class RouterNode:
    """Classifies `state.question` into an intent + document filter."""

    def __init__(self, chat_model: ChatModel) -> None:
        self._chat_model = chat_model

    async def __call__(self, state: QueryState) -> dict[str, Any]:
        """Return the state update: intent + filter fields from `RouteDecision`."""
        prompt = _PROMPT.format(today=state.today.isoformat(), question=state.question)
        decision = await self._chat_model.structured(prompt, schema=RouteDecision)
        categories = decision.categories
        if not categories and decision.intent in _CATEGORY_REQUIRED_INTENTS:
            categories = list(Category)
        return {
            "intent": decision.intent,
            "categories": categories,
            "domains": decision.domains,
            "date_from": decision.date_from,
            "date_to": decision.date_to,
            "out_of_scope_reason": decision.out_of_scope_reason,
        }
