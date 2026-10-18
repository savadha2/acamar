# Reasoning pipeline — requirements + architecture (2026-09-17)

Status: **implemented and verified, same day.** Covers CLAUDE.md
build-order steps 5 (Router/Retrieval + `POST /query`), 8 (CRAG
critique/re-retrieval loop), and 9 (LLM-as-judge evaluator + golden Q&A
set) — built together since they're sequential layers of one pipeline,
not independent features. §4's 8-step build sequence is all done; §7 (new) records what
actually happened running it against real Ollama/Postgres, including two
real findings the golden-set run surfaced and one still-open follow-up.

Builds on the design already sketched in
[docs/architecture.md](architecture.md) §1.5/§4.5 — this fills that in to
buildable detail and flags what's changed since it was written (mainly: the
domain-filter gap below, and that structured LLM calls in this codebase
turned out to use plain `ollama` + JSON schema, not LangChain's
`.with_structured_output()` — see §2.2).

---

## 1. Requirements

### 1.1 Functional

- **FR1** — A free-text question in, a grounded answer out, via `POST /query`.
- **FR2** — Router classifies the question into a retrieval filter: which
  categories are relevant, and a date window. No domain filter — see
  **Decision A**.
- **FR3** — Retrieval embeds the question and fetches the nearest live
  documents matching that filter (reusing `DocumentRepository.search`).
- **FR4** — Synthesis drafts an answer from retrieved context **only**,
  explicitly instructed to say it doesn't know rather than fill gaps.
- **FR5** — Critique (CRAG) checks the draft is actually supported by the
  retrieved documents. If not, and retries remain, loop back to Retrieval
  with a **widened** filter. After the retry cap, return a best-effort
  answer flagged low-confidence rather than erroring.
- **FR6** — The response cites which documents it used (id/title/date) —
  answers about parenting logistics should show their work.
- **FR7** — Streamlit's currently-disabled "Ask" box calls `POST /query` and
  renders the answer, its sources, and a visible low-confidence flag when
  CRAG never got a grounded answer.
- **FR8** — A small hand-authored golden Q&A set + an offline LLM-as-judge
  script that scores correctness and groundedness against it. Run on
  demand, not a CI gate (the corpus is tiny — a numeric score would be
  noise, not signal, this early).

### 1.2 Non-functional

- **NF1 — Fail loud, not open.** An outright LLM/transport failure (as
  opposed to "not grounded," which has a defined fallback in FR5) should
  surface as an error to the caller. This is the **opposite** of
  enrichment's fail-open policy, deliberately: enrichment is an unattended
  background job where hiding a document would be worse than a missing
  headline; a query is a live person watching a text box, and a wrong or
  fabricated answer is worse than "something went wrong, try again."
- **NF2 — No conversation memory for v1.** Each question is independent —
  see **Decision B**. Simplifies the graph a lot (no checkpointer wiring
  despite `langgraph-checkpoint-postgres` already being a dependency).
- **NF3 — Respect `ollama_num_ctx`.** One `RetrievalContext` builder owns
  truncation, the same centralization principle already in
  `docs/architecture.md` §3.4.
- **NF4 — Model choice is config, never inline.** New `ollama_judge_model`
  setting (defaults to `ollama_model` — same "separate setting even when
  equal today" pattern already used for `ollama_summary_model`).
- **NF5 — One `ChatModel` port, no node calls `ollama` directly** — matches
  the existing `Embedder`/`Enricher` convention.

### 1.3 Testing

- Unit: every node driven by a `FakeChatModel` with canned structured
  responses — state transitions, and specifically that the CRAG loop
  actually stops at the retry cap (not an infinite loop, not zero retries).
- Unit: `RetrievalContext` truncation against a small `ollama_num_ctx`.
- E2E: FastAPI `TestClient` + real DB + `FakeChatModel` — `POST /query`
  shape, auth (401 without/with a bad token), citations present.
- Eval: golden set + judge script — tracked over time, not pass/fail.

---

## 2. Architecture

### 2.1 New ports/adapters (file layout matches `docs/architecture.md` §2)

```
ports/chat_model.py            ChatModel protocol, ChatModelError
adapters/llm/ollama_chat_model.py   OllamaChatModel
reasoning/
    state.py                  QueryState
    context.py                RetrievalContext (num_ctx budget)
    graph.py                  builds + compiles the StateGraph
    nodes/
        router.py             RouteDecision + prompt
        retrieval.py          retry-aware filter widening
        synthesis.py          SynthesisOutput + prompt
        critique.py           CritiqueVerdict + prompt
    prompts.py                (or per-node, per architecture.md §3.4)
api/routers/query.py           POST /query
api/schemas.py                 QueryRequest/QueryResponse (gains entries)
scripts/run_eval.py            golden-set runner (LLM-as-judge)
tests/eval/golden_qa.jsonl     hand-authored question/reference pairs
```

### 2.2 `ChatModel` — one generic port, not four

Router/Synthesis/Critique/Judge are four different calls with different
inputs and outputs, happening at different points in a sequential
pipeline — unlike `Enricher`, they can't be merged into one call (Synthesis
needs Retrieval's output, which needs Router's). But the *shape* of each
call is identical: send a prompt, get back one parsed pydantic object. One
port, one adapter, four call-sites each supplying their own prompt + schema
— mirrors `Embedder`/`Enricher` (one capability, many callers), not one
port per node.

```python
# ports/chat_model.py
class ChatModelError(RuntimeError): ...

class ChatModel(Protocol):
    async def structured(self, prompt: str, *, schema: type[T]) -> T: ...
```

**Implementation note — a real deviation from `docs/architecture.md`'s
plan:** that doc assumed LangChain's `.with_structured_output()`. The
enrichment port built since then (`OllamaEnricher`) instead calls
`ollama.AsyncClient.chat(..., format=schema.model_json_schema())` +
`schema.model_validate_json(...)` directly — no LangChain chat wrapper at
all — and it's already working, tested, and lint/type-clean. `OllamaChatModel`
should copy that exact proven pattern rather than introduce a second,
different way of talking to Ollama. LangGraph is still very much used —
for the *graph* (state, edges, the retry loop) — just not
`langchain-ollama`'s chat abstraction underneath it. See **Decision D**.

### 2.3 `QueryState` and the graph

Router's job isn't only "which document categories" — that silently
assumed every question is answerable by retrieving a document. It isn't:
weather has no matching document, ever (it deliberately bypasses
`sources`/`documents` — see `docs/weather-design-proposal.md`), and some
questions aren't answerable by this system at all yet. Router picks an
**intent** first; the category classification only applies within one of
those intents.

```python
class QueryIntent(StrEnum):
    """Closed taxonomy, same reasoning as `Organization` — an open-ended
    intent label would be unfilterable and prone to sprawl."""

    DOCUMENT = "document"        # needs retrieval — event/menu/reminder/holiday/activity
    WEATHER = "weather"          # answerable directly from WeatherProvider, no retrieval
    COUNT = "count"              # exact aggregation — direct SQL count, no retrieval (§2.9 #26)
    OUT_OF_SCOPE = "out_of_scope"  # decline rather than force a bad answer through retrieval

class OutOfScopeReason(StrEnum):
    """Why OUT_OF_SCOPE fired — lets the decline message be honest instead
    of one flat unhelpful string (§2.9, 27-29 vs. 19/20/23/24)."""

    NEVER = "never"              # no plausible future version of this system answers this
    NOT_YET_BUILT = "not_yet_built"  # the domain/category is reserved, just has no source yet

class QueryState(BaseModel):
    question: str
    today: date                       # from Clock, for Router/Synthesis/the weather branch
    intent: QueryIntent | None = None
    categories: list[Category] = []   # only meaningful when intent == DOCUMENT
    domains: list[Domain] | None = None  # None = search all; see Decision A
    date_from: date | None = None
    date_to: date | None = None
    retrieved: list[Document] = []
    count_result: int | None = None
    draft_answer: str = ""
    critique: CritiqueVerdict | None = None
    retry_count: int = 0
    final_answer: str = ""
    grounded: bool = True   # weather/count/out-of-scope answers are trivially "grounded"
    out_of_scope_reason: OutOfScopeReason | None = None
```

```
                    ┌─ DOCUMENT ──▶ Retrieval ──▶ Synthesis ──▶ Critique ──▶ answer
                    │                   ▲                           │
query ──▶ Router ───┤                   └─── retry (≤ N), widened ───┘ not grounded
                    ├─ WEATHER ──▶ format WeatherProvider.today() + weather_advice() ──▶ answer
                    │              (plain Python, no LLM call — same as the dashboard chip)
                    ├─ COUNT ──▶ DocumentRepository.count(...) ──▶ format the number ──▶ answer
                    │            (plain SQL, no vector op, no LLM call — exact by construction)
                    └─ OUT_OF_SCOPE ──▶ decline message, worded by out_of_scope_reason ──▶ answer
```

Three of the four branches skip Retrieval/Synthesis/Critique entirely —
there's nothing to retrieve or critique for a live number or a refusal.
WEATHER reuses the already-built `WeatherProvider`/`weather_advice`
verbatim (a template sentence like `f"{temp}°F, {condition} — {advice}"`,
matching weather's existing "deterministic, not LLM-narrated" design).
COUNT is one exact SQL `count(*)` — see §2.9 #26 for why retrieval-then-
synthesize is actively unsafe for "how many" questions specifically.
OUT_OF_SCOPE is a decline templated by `out_of_scope_reason`, not a model
call at all — cheaper and more honest than letting Synthesis improvise
from irrelevant retrieved docs and hoping Critique catches it after the
fact.

`graph.add_conditional_edges("router", after_router, {"document":
"retrieval", "weather": "weather_answer", "count": "count_answer",
"out_of_scope": "decline"})` and, within the DOCUMENT branch,
`graph.add_conditional_edges("critique",
after_critique, {"retry": "retrieval", "done": END})` where `after_critique`
checks `state.critique.grounded or state.retry_count >= _MAX_RETRIES`. No
checkpointer compiled in (Decision B) — a bare `graph.compile()`, one
`ainvoke()` per `/query` call.

### 2.4 Node behavior

- **RouterNode** — `ChatModel.structured(prompt, schema=RouteDecision)`.
  ```python
  class RouteDecision(BaseModel):
      intent: QueryIntent
      categories: list[Category] = []      # 1+ when intent == DOCUMENT; a broad
                                            # question gets more than one (Decision C)
      domains: list[Domain] | None = None  # only narrow when the question implies
                                            # a specific source ("at school" vs
                                            # general); None = search all (Decision A)
      date_from: date | None = None
      date_to: date | None = None
  ```
  Prompt includes `today` so "this week"/"next Friday" resolve the same way
  the enricher's narrative-date extraction already does, plus a couple of
  worked examples of each intent (§2.8 doubles as the prompt's few-shot
  examples and a golden-set seed — see below).

- **RetrievalNode** — embeds `state.question` once, calls
  `DocumentRepository.search(...)` with a filter that **widens on retry**:
  | Attempt | categories | date window | limit |
  |---|---|---|---|
  | 0 (Router's own) | routed | routed | 8 |
  | 1 | routed | 2× wider, same center | 8 |
  | 2 (final, `_MAX_RETRIES=2`) | **all** | 2× wider | 12 |

  Requires `search()`'s signature to change — see **Decision A**.

- **SynthesisNode** — `ChatModel.structured(prompt, schema=SynthesisOutput)`
  where `class SynthesisOutput(BaseModel): answer: str` (a one-field wrapper,
  kept for interface uniformity with the other three call-sites — a real
  `FakeChatModel` doesn't need a special case for "the one node that returns
  free text"). Context assembly goes through `RetrievalContext`, which
  formats each retrieved `Document` (title/date/summary-or-content) and
  stops adding documents once the running token estimate would exceed
  `settings.ollama_num_ctx` minus a reserved margin for the prompt +
  question — the CLAUDE.md gotcha this whole builder exists for. Prompt is
  explicit: answer only from the context below; if it doesn't cover the
  question, say so plainly rather than guessing.

- **CritiqueNode** — `ChatModel.structured(prompt, schema=CritiqueVerdict)`:
  ```python
  class CritiqueVerdict(BaseModel):
      grounded: bool
      reason: str
  ```
  Scoped **narrowly to groundedness** (is the draft supported by the
  retrieved docs?), not answer quality/helpfulness — that broader judgment
  is the offline Judge's job (§2.6), not a per-query cost. This is the
  literal CRAG definition and keeps the live retry loop cheap.

### 2.5 `POST /query`

```python
class QueryRequest(BaseModel):
    question: str

class QuerySource(BaseModel):
    id: UUID
    title: str
    event_date: date | None

class QueryResponse(BaseModel):
    answer: str
    sources: list[QuerySource]
    grounded: bool   # false = best-effort fallback after exhausting retries
```

Router/Synthesis/Critique failures (`ChatModelError`) propagate as a 502 —
NF1. Streamlit: enable the "Ask" box, `POST` the question, render the
answer + a small source list, and a subtle "not fully sure" treatment
(reusing the existing amber/urgency color language) when `grounded=false`.

### 2.6 Evaluator (build step 9) — offline, separate from the live graph

- `tests/eval/golden_qa.jsonl` — hand-authored against **real current
  data**. Each line: `question`, `expected_intent`, `expected_categories`,
  `reference_answer`. §2.8's 20 worked queries are the seed for this file —
  they already span every intent and the known gaps (no-data categories,
  genuinely out-of-scope), not just happy-path document questions.
- `scripts/run_eval.py` — runs the golden set through the real compiled
  graph, then a **Judge** call (same `ChatModel` port, its own prompt +
  `JudgeVerdict(correctness: int, grounded: bool, reasoning: str)` schema,
  scoring the generated answer against the reference) using
  `settings.ollama_judge_model`. Prints/writes a score summary; not wired
  into pytest or CI (`docs/architecture.md` §4.5 already settled this).

### 2.7 Model routing

Every node gets its **own** settings field, even where the value matches
another node's today — the same "separate setting even when equal"
pattern `ollama_summary_model` already established, because the whole
point is being able to change one job's model without touching another's.

```python
ollama_router_model:    str = "llama3"      # gemma3:1b tested at 65%, llama3 at 90% (see below)
ollama_model:           str = "llama3"      # Synthesis — existing setting, unchanged
ollama_critique_model:  str = "llama3"      # CRAG grader — a safety net, not the place to cut corners
ollama_judge_model:     str = "llama3"      # offline eval — see Decision E
```

**Per-node reasoning**, grounded in what's actually pulled locally
(`llama3:8b` Q4_0 4.7GB, `gemma3:1b` 0.8GB — nothing bigger) and the
hardware (M4, **16GB RAM**, which rules out anything 70B-class and makes
~13–14B the realistic local ceiling):

- **Router** — narrow closed-set classification (intent + ≤5 categories +
  a date window) from a short question. **Tested empirically, not just
  hypothesized** (`scripts/_spot_check_router.py` against all 31 worked
  questions, 2026-09-17): `gemma3:1b` scored 20/31 (65%), with real
  misclassifications (homework-help routed to `document`, "send an
  umbrella" routed to `out_of_scope`) — same "small model on a nuanced
  classification" failure mode already seen in enrichment.  `llama3`
  scored 27/31 (87%), then 28/31 (90%) after one prompt fix (Hawaii-weather
  was answering with Cupertino's forecast instead of declining — added an
  explicit "only this household's location counts as `weather`" line).
  `ollama_router_model` defaults to `llama3`. The 3 remaining misses
  (another family's carpool, a financial question, the teacher's-email
  question) all degrade safely — two resolve to a correct-but-unhelpful
  "I don't know" via `document`'s zero-result path rather than a wrong
  answer, and the third is functionally identical either way given the
  known sender-field gap (§2.9 #30).
- **Synthesis** — this *is* the user-facing answer. A fabricating small
  model here is the single worst failure mode in the whole pipeline.
  `llama3` — already proven for a similar (summary-writing) job in the
  enricher.
- **Critique** — a safety mechanism, not just another check: if the grader
  is weaker than what it's grading, the retry loop won't fire when it
  should, silently defeating CRAG's whole purpose. At least as capable as
  Synthesis — `llama3`, not `gemma3:1b`.
- **Judge** — see **Decision E**. The one node with no live-latency
  constraint at all (offline, on-demand, ~10 examples) and where the
  entire value of the eval harness depends on the judge being trustworthy
  — the textbook case for spending more capability, not less.

### 2.8 Worked queries — every intent, including the gaps

Twenty realistic questions, each labeled with the routing decision it
should produce. Doubles as the golden-set seed (§2.6) — a question with a
known-correct route *is* a golden example, not separate work.

**Document questions — clean happy path:**

1. "What's for lunch today?" → `DOCUMENT`, `[MENU]`, today
2. "What's on the menu this week?" → `DOCUMENT`, `[MENU]`, this week
3. "What events are coming up this month?" → `DOCUMENT`, `[EVENT]`, this month
4. "Is there a Fall Festival this year?" → `DOCUMENT`, `[EVENT]`, wide-open window
5. "When is the next FEEF meeting?" → `DOCUMENT`, `[EVENT]`, today onward
6. "Do I need to sign anything this week?" → `DOCUMENT`, `[REMINDER]`, this week
7. "Are there any permission slips due soon?" → `DOCUMENT`, `[REMINDER]`, near-term
8. "When is the next school holiday?" → `DOCUMENT`, `[HOLIDAY]`, today onward
9. "Is there school tomorrow?" → `DOCUMENT`, `[HOLIDAY, EVENT]`, tomorrow
10. "What time does the Fall Festival start?" → `DOCUMENT`, `[EVENT]`, specific lookup

**Document questions — multi-category (tests Decision C):**

11. "What's happening this week?" → `DOCUMENT`, `[EVENT, REMINDER, HOLIDAY]`, this week
12. "Anything I need to do before Friday?" → `DOCUMENT`, `[REMINDER, EVENT]`, through Friday

**Weather — never a document, tests the new branch:**

13. "Will it rain today?" → `WEATHER`
14. "Do I need to send an umbrella?" → `WEATHER`
15. "Is it going to be hot this weekend?" → `WEATHER` — **but exposes a real
    gap**: `WeatherProvider.today()` only covers today. Answering this
    honestly needs either a multi-day forecast added to that port, or
    Synthesis explicitly saying "I can only tell you about today right
    now" rather than guessing at the weekend. Not solved here — flagged so
    it isn't discovered as a bug later.

**Document questions — right category, no data yet (correctly says "I don't know," not out of scope):**

16. "What activities are there for kids this weekend?" → `DOCUMENT`, `[ACTIVITY]`
17. "Are there any camps during winter break?" → `DOCUMENT`, `[ACTIVITY]`
18. "Is there a Parks and Rec event in Cupertino this weekend?" → `DOCUMENT`,
    `[ACTIVITY]` — same bucket as 16/17, not a new one (see below)

**Genuinely out of scope — no plausible future category fixes this:**

19. "Can you help me with my daughter's homework?" → `OUT_OF_SCOPE`
20. "What's the weather like in Hawaii next week?" → `OUT_OF_SCOPE` (wrong
    location — the system is hardcoded to one, Cupertino)
21. "Where can I buy art material for my daughter's project?" →
    `OUT_OF_SCOPE`
22. "We want to do a stars and constellation project for social studies —
    where can I find the raw material for the project board?" →
    `OUT_OF_SCOPE`

**21/22 are the sharpest edge case here, not the easiest.** Both name
something that plausibly *is* a real ingested document (quite possibly the
actual "Social Studies Project" email from earlier this session) — so "no
plausible connection to school" is the wrong test for `OUT_OF_SCOPE`, and
an earlier draft of Decision F said exactly that. The right test is the
**ask type**: even a perfect retrieval of the real assignment document
couldn't answer "where do I buy this" — that information was never in any
school email to begin with. `DOCUMENT` covers when/what/whether something
is happening or due; it never covers where to buy or how to make
something, no matter how specifically the question names a real project.

**Why 16–18 don't need a design change, only a future source.**
`Category.ACTIVITY` already exists in `domain/enums.py` today, unused —
CLAUDE.md's build order literally says "activities/reminders may have no
data yet, route for them anyway." Parks & Rec listings are exactly
document-shaped (title, date, description) — a *new source connector*
feeding the *existing* `documents` table with `category=ACTIVITY`, the
same "new `sources` row + one connector class, never a new code path"
pattern every other source already follows. It is **not** the same kind of
addition as weather (which bypasses documents entirely because a forecast
isn't durable content) — it's the ordinary case this architecture was
already built for. Nothing in this design needs to change to support it
later; it needs data, not a redesign. MVP ships with the current two
sources regardless (Faria + Gmail) — 16–18 are expected to answer "I don't
know" honestly until that source exists, which is the correct behavior,
not a bug.

### 2.9 More questions — most confirm the rule, three break it usefully

**Confirm the existing rule (ask type the corpus could never contain, regardless of topic):**

23. "Who's driving carpool for the field trip?" → `OUT_OF_SCOPE` — needs
    data about *other households*. Structurally impossible here, not just
    unbuilt: single-household by design (`architecture.md` decision #10),
    there is no other family to have data about.
24. "How's my daughter doing in math this year?" → `OUT_OF_SCOPE` —
    academic/performance records. A different flavor of never: not
    "wrong ask type" like 21/22, but a data category (student records)
    nothing in this system's design touches or plans to.
25. "Can you remind me about the festival tomorrow?" / "Turn off
    notifications for FEEF emails" → `OUT_OF_SCOPE` — a **write/action**
    request, not a question. The whole graph is read-only (Router →
    Retrieval → Synthesis → Critique, all reading `documents`); it has no
    concept of scheduling a future notification or changing a setting.
    Note this is the exact capability the notification-agent/delivery
    backlog item would provide — deprioritized until a mobile app exists
    (your call, 2026-09-17). Consistent, not a gap.

**Break the rule — need a real design response, not just a decline:**

26. "How many reminders do I have this month?" — **not `OUT_OF_SCOPE`, and
    not safely `DOCUMENT` either.** Retrieval returns the top-K nearest
    documents by embedding similarity (K=8-12) — a similarity rank, not an
    exhaustive list. Synthesizing "you have 6 reminders" from a
    possibly-truncated top-K is a **confident wrong number**, a worse
    failure than "I don't know" because it's not obviously a guess. This
    needs a **fourth intent**: `QueryIntent.COUNT` → a new, narrow
    `DocumentRepository.count(*, categories, domains, date_from, date_to)
    -> int` (`SELECT count(*) ... WHERE ...`, same filter shape as
    `search()`, no vector op at all) — cheap, exact, and correctly out of
    reach of the hallucination risk retrieval-then-synthesize has for this
    one question shape. Same reasoning as weather: a live/computed answer
    doesn't need an LLM in the loop at all once you know the SQL is exact.
27. "How much do I owe for the field trip?" → `Domain.FINANCIAL` already
    exists in `domain/enums.py`, reserved, per CLAUDE.md's stated
    multi-domain goal — no source ingests it yet.
28. "How do I reset my password on the daycare portal?" →
    `Domain.PRODUCT_MANUAL`, same story — reserved, unbuilt.
29. "Is daycare closed for Labor Day?" → `Domain.DAYCARE` — also reserved,
    and CLAUDE.md is explicit that daycare stays out of scope until the
    daughter's MVP works end-to-end regardless.

**27–29 shouldn't get the same decline as 19/20/23/24.** Those are *never*
— no plausible future version of this system answers "help with homework"
or another family's carpool schedule. 27–29 are *not yet* — the taxonomy
already reserves the slot (`Domain.FINANCIAL`/`PRODUCT_MANUAL`/`DAYCARE`
all exist, unused, today), same as `Category.ACTIVITY` for 16–18. Worth
a `QueryIntent.OUT_OF_SCOPE` sub-reason (`never` vs. `not_yet_built`) so
the decline message can honestly say "I don't have daycare information
yet" instead of a flat "I can't help with that" — cheap to add, real
difference in how trustworthy the system feels.

**One content gap, not a routing problem:**

30. "What's my daughter's teacher's email?" → `DOCUMENT` (a legitimate
    when/what/whether-adjacent "who do I contact" ask), but likely
    **fails today regardless of routing** — `sender` is read off `RawEmail`
    during Gmail parsing and then dropped; it's never persisted onto
    `RawDocument`/`Document` (only used transiently for the
    conversational-mail privacy filter). If this turns out to be a common
    real question, the fix is capturing sender as a real field — a data
    gap to note now, not a Router rule.

**One that confirms `REMINDER`'s definition is already broad enough:**

31. "How do I report that my daughter will be absent today?" →
    `DOCUMENT`, `[REMINDER]`. Worth stating explicitly since it's never
    been written down: `REMINDER` isn't only "has an upcoming deadline" —
    the parser already files anything date-less as `REMINDER` (school
    procedures, standing policy) by construction, and `search()`/
    `list_documents()` never require an `event_date` (a `NULL` one still
    matches). A standing "how do I report an absence" policy email, if one
    was ever ingested, is already retrievable with zero design changes —
    this needed a definition written down, not new code.

---

## 3. Decisions (approved 2026-09-17)

**A. Domain-optional retrieval, defaulting to all (recommended, revised).**
Originally proposed as "drop `domain` entirely" — refined after §2.8: that
was reasoned about a two-domain present (`school`, `gmail`), but Parks &
Rec (or any future `Domain.COMMUNITY`-style source) means a real future
where a household might actually mean "just school" ("what's happening at
school this week") vs. "anywhere" ("something for the kids this weekend").
So: `RouteDecision.domains: list[Domain] | None` — `None` (the default,
used whenever the question doesn't name a source) means search all
domains; Router only populates it when the question textually implies one.
Still a real interface change either way: `search()` currently *requires*
an exact `domain` match, and `EVENT` documents already come from both
`school` and `gmail` today — a domain-locked query silently misses half of
them right now, before any new source exists. Requires editing the
`search()` Protocol, its Postgres SQL (`category = ANY(%s)`, `domain =
ANY(%s) OR %s IS NULL`), and its tests.

**B. No conversation memory for v1 (recommended).** Each `/query` call is
independent — no follow-up-question context carried over. Simpler (skips
wiring the `PostgresSaver` checkpointer entirely, despite it already being
a dependency) and nothing today needs it. Flag if you actually want
follow-up questions to work ("and what about tomorrow?") — that changes
the state/session design meaningfully.

**C. Router outputs 1+ categories, not exactly one (recommended).** Matches
how the dashboard already pools event/reminder/holiday together for
"what's coming up"-style views. Applies within the `DOCUMENT` intent only
— see §2.3's revised `QueryState`.

**Known limitation, logged not solved (backlog):** a single top-K query
across a union of categories isn't balanced — if the corpus has more
event-shaped documents near a question's embedding than reminder-shaped
ones, the top-8 could skew almost entirely to events even when a reminder
was equally relevant, silently under-representing it rather than excluding
it outright. A fully-balanced version (one retrieval per category, merged)
costs more calls/complexity than seems justified at the current corpus
size. **Tracked in the backlog memory, not fixed here** — revisit if a
real "it missed an obviously-relevant reminder" case shows up as the
corpus grows.

**D. `ChatModel` via plain `ollama` + JSON schema, not `langchain-ollama`
(recommended).** Reuses the exact pattern `OllamaEnricher` already proves
works in this codebase. LangGraph still does the actual orchestration
(state, edges, the retry loop) — this only decides how each node makes its
one LLM call underneath.

**E. Judge model tier — start at same-as-everything (`llama3`), revisit
later (recommended).** Three real options, in order of capability and
cost: (1) reuse `llama3` — zero new setup, weakest signal, but
self-detectably so at 10 hand-checkable examples; (2) pull a bigger local
model (~13–14B is close to what 16GB RAM can realistically hold); (3) a
hosted API model for judging only — the most capable option and the one
place in this design where "no live latency, no user data in the request
path" makes an exception to the "local via Ollama" stack decision
defensible. Recommend (1) now, and revisiting once the golden set outgrows
what you can spot-check by eye — that's exactly when judge trustworthiness
stops being something you can verify yourself.

**F. Bias Router toward `DOCUMENT`, not `OUT_OF_SCOPE`, when uncertain
(recommended).** The three-way intent split is a new failure mode that
didn't exist in the single-level design: Router could now wrongly decline
something it actually could have answered. A wrong `DOCUMENT` guess is
cheap to recover from — Retrieval/Critique's existing retry-and-widen loop
degrades gracefully to a low-confidence answer (§2.4). A wrong
`OUT_OF_SCOPE` guess has no recovery path at all; it never even tries.

The classification rule is **the ask type, not the topic** — "does this
mention something school-related" is the wrong test (§2.8's 21/22 mention
a real assignment and are still out of scope). The corpus only ever
contains *when/what/whether* something is happening, due, or true. It
never contains *where to buy* or *how to make* anything — no retrieval,
however perfect, could answer that. So: `DOCUMENT` = asking about
when/what/whether regarding school/daycare logistics, even if no matching
document turns out to exist (that's a zero-result `DOCUMENT` case, §2.8's
16–18, not `OUT_OF_SCOPE`). `OUT_OF_SCOPE` = asking for a *kind* of
information — sourcing/purchasing, how-to/craft instructions, general
knowledge, homework answers — the corpus structurally could never contain,
regardless of whether it names something real.

**G. Add `QueryIntent.COUNT` as a fourth branch (recommended).** Retrieval
is similarity-ranked top-K, not exhaustive — synthesizing a count from it
risks a confidently wrong number, a worse failure than "I don't know"
because it doesn't look like a guess. Needs one new
`DocumentRepository.count(*, categories, domains, date_from, date_to) ->
int` method (plain `SELECT count(*)`, same filter shape as `search()`, no
vector op, no LLM call at all for the actual answer). Small, contained,
and closes a real hallucination risk rather than adding scope for its own
sake.

**H. `OutOfScopeReason` (`never` vs. `not_yet_built`) on the decline
(recommended).** Costs one enum + one prompt instruction; the alternative
is a single flat "I can't help with that" for both "never will" (homework
help) and "not yet, but planned" (daycare/financial/product-manual — all
three already reserved as real enum values today) — a worse, less
trustworthy answer for no real savings.

Everything else in this doc follows from A–H; flag any you'd resolve
differently and I'll adjust the rest before writing code.

---

## 4. Sequencing

Cheapest/most-isolated → riskiest, each step independently testable before
the next depends on it — too large to build in one pass.

1. **`ChatModel` port + `OllamaChatModel`** + `FakeChatModel`. Testable in
   complete isolation (structured call in, pydantic object out) — no
   LangGraph, no graph wiring yet.
2. **`DocumentRepository.search()`/`count()` interface change** (Decisions
   A, G) — SQL + Protocol + `FakeDocumentRepository` + existing tests
   updated. Isolating this from the graph work means a regression here is
   obviously attributable, not tangled up with new node code.
3. **`RouterNode` standalone**, unit-tested with `FakeChatModel` — then a
   real-Ollama spot check against all 31 questions in §2.8/§2.9 before
   trusting it inside the full graph. That list exists for exactly this.
4. **Retrieval/`RetrievalContext`/Synthesis/Critique nodes**, each
   unit-tested individually with fakes before they're wired together.
5. **The LangGraph `StateGraph` itself** — Router's four branches, the
   critique retry loop. First point real LangGraph 1.2.11 API specifics
   get exercised; verify against current docs rather than assume, given
   how much the ecosystem moves.
6. **`POST /query` + Streamlit "Ask" box wiring.**
7. **CRAG retry-loop tests** (build step 8) — force an ungrounded first
   attempt, confirm the widen-and-retry fires and caps at `_MAX_RETRIES`.
8. **Golden set + `scripts/run_eval.py`** (build step 9) — the 31
   questions already have expected routing; still need real reference
   answers written against current data before they're a usable golden
   set.

---

## 5. Implementation guidelines

Everything in `docs/coding-standards.md` applies unmodified — nothing
here is exempt from `D`/`ANN`/`RUF`/mypy strict. This section is only the
patterns specific to *this* build that aren't already written down
elsewhere.

### 5.1 File/module conventions

Follow `docs/architecture.md` §2's planned layout exactly (already
reproduced in §2.1 above) — `ports/chat_model.py`, `adapters/llm/
ollama_chat_model.py`, `reasoning/{state,context,graph}.py`,
`reasoning/nodes/{router,retrieval,synthesis,critique}.py`. One node per
file, matching one-connector-per-file/one-service-per-file everywhere else
in this codebase. `QueryIntent`/`OutOfScopeReason` belong in
`domain/enums.py` alongside `Category`/`Organization` — same closed-
taxonomy reasoning, same place.

### 5.2 Prompts are versioned constants with a stated "why"

Mirror `adapters/enrichment/ollama_enricher.py`'s `_PROMPT` exactly: a
module-level string constant, `.format(...)`'d with the node's inputs, not
an inline f-string buried in a method. Each node's prompt should:

- **Embed a few worked examples from §2.8/§2.9 directly in the prompt**,
  not just describe the rule abstractly — especially Router's `DOCUMENT`
  vs `OUT_OF_SCOPE` boundary (Decision F), where the abstract rule ("ask
  type, not topic") is exactly the kind of nuance a small model needs a
  concrete example for, not just an instruction. #21/22 (names a real
  project, still out of scope) are the two examples most worth including
  verbatim — they're the ones that broke the naive rule.
- **State today's date explicitly**, same as the enricher already does
  (`Received: {received_at} (use this year for any date mentioned with no
  year stated)`) — Router and Synthesis both need `state.today` for
  relative-date resolution.
- **Carry a one-line changelog comment** above the constant when a prompt
  changes after real-data testing (the enricher's model-choice comment in
  `config.py` is the model to follow — record *what* was tried and *why*
  it changed, not just the current version).

### 5.3 Failure handling — the live path fails loud, entirely

NF1 said `ChatModelError` propagates as a 502. Extend that to **every**
exception in the live graph, not just LLM failures — a `DocumentRepository`
error, an embedding failure, anything. The whole point (unlike ingestion's
fail-open) is that a live person is watching; masking a DB error as an
empty/wrong answer is worse than a clean 502. Concretely: `POST /query`'s
handler wraps the entire `graph.ainvoke(...)` call in one
`try/except Exception` → `HTTPException(502, ...)`, not scattered
per-node try/excepts that each decide differently.

### 5.4 Dependency injection — one `OllamaChatModel` instance per model

Given §2.7's per-node model routing, there is **no single shared
`ChatModel` instance** — `OllamaChatModel` is instantiated once per
configured model (router/synthesis/critique/judge), same as `OllamaEnricher`
is one adapter bound to one model, not a multi-model service object. In
`api/dependencies.py`, this looks like four small `get_*_chat_model()`
providers (mirroring `get_weather_provider`'s `@lru_cache` singleton
pattern — no reason to reconstruct an `ollama.AsyncClient` per request),
each feeding the one node that uses it. `get_dashboard_service`-style
composition root wires the graph itself from these four.

### 5.5 Graph construction — no node touches `ollama`/`psycopg` directly

Same Dependency-Inversion rule as everywhere else in this codebase
(`docs/architecture.md` §2.1 "D"): nodes depend on `ChatModel`/
`DocumentRepository`/`WeatherProvider`/`Clock` ports only, injected at
graph-construction time, never importing `ollama`/`psycopg` themselves.
`reasoning/graph.py`'s builder function takes all four as parameters and
returns a compiled graph — this is what makes §6.2's graph-level tests
possible without a real Ollama/Postgres.

---

## 6. Test suite recommendation

### 6.1 Unit — one file per node/module, fakes only, matches the existing pyramid

| File | Covers |
|---|---|
| `tests/unit/test_ollama_chat_model.py` | `OllamaChatModel` — same convention as `tests/unit/test_ollama_enricher.py`: a small hand-rolled `_FakeClient`/`_FakeResponse` standing in for `ollama.AsyncClient` (injected via the constructor's `client=` param), not a mocking library. Worth its own dedicated test rather than only being exercised through node tests — `structured()` is reused by 4+ call sites, and a shared bug in the JSON-schema/parse path would otherwise stay invisible until *all* of them start failing at once. |
| `tests/unit/test_router_node.py` | `RouterNode` with `FakeChatModel` — state transitions only (mechanical correctness: does a canned `RouteDecision` get applied to state correctly), **not** real classification accuracy — that's §6.3. |
| `tests/unit/test_retrieval_node.py` | Filter construction, and specifically the retry-widening table (§2.4) — assert the *n*th retry attempt's filter matches the table exactly, via `FakeDocumentRepository.search()`'s recorded call args (same `self.calls` list pattern already used by `FakeEmbedder`/`FakeEnricher`). |
| `tests/unit/test_retrieval_context.py` | Token-budget truncation against a small `ollama_num_ctx` — assert documents stop being added once the budget would be exceeded, and that truncation doesn't silently drop the *question itself* from the prompt. |
| `tests/unit/test_synthesis_node.py` | Context assembly reaches the model; a `FakeChatModel` canned "I don't have information about that" response passes through unchanged (no node-level post-processing accidentally rewrites an honest non-answer). |
| `tests/unit/test_critique_node.py` | `grounded=True`/`False` both drive state correctly; **the retry cap specifically** — a `FakeChatModel` returning `grounded=False` every time must still stop at `_MAX_RETRIES`, not loop forever (this is the single most important test in the whole suite — an infinite or excessively long CRAG loop is a real production incident, not just a wrong answer). |
| `tests/unit/test_document_repository_search.py` (update) | Decisions A/G — `domains=None` searches all, a populated list narrows; `categories` plural via `ANY`; new `count()` method, same filter shape as `search()`. |

**`FakeChatModel` design** (needed before any node test above can be
written): keyed by requested schema type, not call order — call order
varies across the retry loop and shouldn't be baked into every test.

```python
class FakeChatModel:
    def __init__(self, responses: dict[type[BaseModel], BaseModel | list[BaseModel]]):
        self._responses = responses  # a list = popped in call order (for multi-attempt tests)
        self.calls: list[type[BaseModel]] = []

    async def structured(self, prompt: str, *, schema: type[T]) -> T:
        self.calls.append(schema)
        response = self._responses[schema]
        return response.pop(0) if isinstance(response, list) else response
```

A list value is what makes the retry-cap test possible: `{CritiqueVerdict:
[CritiqueVerdict(grounded=False, ...)] * 3}` — three ungrounded verdicts
queued, asserting the loop stops after exactly `_MAX_RETRIES` and doesn't
raise `IndexError` trying for a fourth.

### 6.2 Graph-level — the compiled graph, still no real Ollama/Postgres

`tests/unit/test_reasoning_graph.py` (or `tests/integration/` if it feels
heavier than the rest of `unit/` — either is defensible since it's still
all-fakes, no real I/O): build the graph per §5.5 with `FakeChatModel`/
`FakeDocumentRepository`/`FakeWeatherProvider`, call `graph.ainvoke(...)`,
assert on final state. Minimum coverage:

- Each of the four `after_router` branches actually reaches its node
  (`DOCUMENT` → retrieval runs, `WEATHER` → `WeatherProvider.today()` was
  called and `ChatModel` was **not**, `COUNT` → `count()` was called and
  retrieval was **not**, `OUT_OF_SCOPE` → neither retrieval nor synthesis
  ran).
- The full retry loop end-to-end: ungrounded → widened retry → grounded →
  `END`, asserting the *widened* filter (not the original) reached the
  second `search()` call.
- Retry cap reached with **still-ungrounded** verdicts throughout →
  `grounded=false` in the final state, not an exception.

### 6.3 A third category this project hasn't needed before: prompt spot-checks

Unit tests with `FakeChatModel` verify RouterNode's *mechanics*, never
whether the real model classifies real questions correctly — that needs an
actual Ollama call, which doesn't belong in pytest (slow, non-deterministic,
requires a running Ollama). Recommend a disposable script, same convention
as this session's `scripts/_one_off_*` sweeps:
`scripts/_spot_check_router.py` — runs all 31 questions from §2.8/§2.9
through the real `RouterNode` (real `gemma3:1b`), prints question →
routing decision side by side, for a human to eyeball against the expected
column before trusting Router inside the full graph. This is step 3 of
§4's sequencing, formalized — not part of CI, not part of the golden-set
Judge (§6.5), a distinct lightweight "does the prompt actually work"
check that happens once, early, before building on top of it.

### 6.4 E2E — `POST /query` through all four branches

Extends `docs/architecture.md` §4.4's existing e2e pattern (FastAPI
`TestClient` + real DB + fakes) to explicitly hit each intent, now that
there are four: one `DOCUMENT` question against seeded real documents, one
`WEATHER` question with a `FakeWeatherProvider` injected, one `COUNT`
question against a known seeded count, one `OUT_OF_SCOPE` question
asserting the decline shape. Plus auth (401 without/with a bad token, per
the existing pattern every other router already tests).

### 6.5 Eval harness (build step 9) — already specced in §2.6, cross-referenced here

Golden set + `scripts/run_eval.py`, on-demand, not CI. §2.8/§2.9's 31
questions are the seed; each still needs a real `reference_answer` written
against current data before it's a usable golden example, not just a
routing test case.

### 6.6 A real gap: `count()`/the widened `search()` SQL has no automated check yet

Unit tests against `FakeDocumentRepository` validate node *behavior*, not
that the real Postgres SQL is actually correct — that's what integration
tests (testcontainers, tracked in the backlog's P3 tier) are for, and they don't exist
yet. Until they do, `count()` and the widened `search()` (Decisions A/G)
are new/changed SQL with **no automated verification path** — a fake that
quietly diverges from the real SQL (e.g., forgetting `dismissed_at IS
NULL`, or the domain `OR %s IS NULL` logic) would pass every unit test
while the real query is wrong. Recommend a manual one-off verification
script against the real local Postgres before trusting either in
production, same spirit as this session's narrative-date sweep scripts —
not a substitute for building the integration-test infra, but honest about
what's actually been checked versus assumed correct by construction.

---

## 7. Implementation notes (2026-09-17) — what actually happened

All 8 steps in §4 built and verified same day: 244 tests passing (up from
196 before this build), ruff/mypy clean throughout, plus verification
beyond unit tests — a real `search()`/`count()` SQL check against the live
DB (§2's `%s::text[] IS NULL` cast was a real bug the fakes couldn't have
caught), a full end-to-end smoke test against live Ollama/Postgres across
all four intents, and the golden set actually run through `scripts.run_eval`.

- **Router model, tested not assumed**: `gemma3:1b` scored 20/31 (65%) on
  the worked questions in §2.8/§2.9 — real misclassifications (homework
  routed to `document`, "send an umbrella" routed to `out_of_scope`).
  `llama3` scored 27/31 (87%), then 28/31 (90%) after fixing a real bug
  (Hawaii-weather was answering with Cupertino's forecast instead of
  declining). `ollama_router_model` defaults to `llama3`.
- **A real retrieval gap, found by the golden-set run, not hypothesized**:
  "When is the Social Studies Project due?" answered "I don't have that
  information" even though the document exists (due Sept 25). Root cause,
  fully diagnosed: Router picked `categories=[reminder]` with a
  `date_from=date_to=today` window — wrong on both counts for a question
  that's inherently asking about an unknown future date. Fixed the
  date-window half (prompt now explicitly says don't default to today for
  "when is X due" questions) — verified fixed (`date_from`/`date_to` now
  `None` for that question). **Category selection is still unreliable**:
  confirmed directly that `search(categories=[reminder, event])` finds the
  document as the #1 result while `search(categories=[reminder])` alone
  misses it entirely, and Router still doesn't reliably include both
  despite an explicit prompt instruction to do so when ambiguous. Open
  follow-up (tracked in the backlog memory) — needs more prompt iteration,
  or a design change (e.g. Retrieval trying likely-adjacent categories
  rather than trusting Router's exact set).
- **A judge reliability finding — exactly what Decision E's "same-tier
  judge, spot-check by eye" approach exists to catch**: the judge (llama3)
  scored the wrong Social-Studies-Project answer above 5/5, and its own
  stated reasoning misread its own supplied reference text (claimed the
  reference "does not provide a specific due date" when the reference
  literally states "September 25, 2026"). Caught by reading the
  transcript, not by any automated check. This is a concrete, real data
  point for Decision E, not a hypothetical one — worth remembering when
  deciding whether to upgrade the judge tier later.
- Decision C's pooling-imbalance limitation (§3) was left exactly as
  approved — not touched, not investigated further, tracked in the backlog
  memory as originally instructed.
