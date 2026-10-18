# ACAMAR — Architecture & Engineering Plan

Status: **living document.** Build step 2 (Faria events connector + parser) is
implemented; steps 3+ are design. §7 is the decisions log — blocking ones are
settled, the rest flagged ⏳. Sections describing unbuilt layers (reasoning,
delivery, most adapters) are the plan, not the code.

---

## 1. Architecture

### 1.1 The four layers (from README / CLAUDE.md)

```
┌─────────────────┐   ┌──────────────────┐   ┌─────────────────────┐   ┌────────────┐
│  Source agents  │──▶│  Document store  │──▶│  Reasoning agents   │──▶│  Delivery  │
│  (connectors)   │   │  Postgres +      │   │  Router→Retrieval→  │   │  FastAPI,  │
│  scrape / Gmail │   │  pgvector        │   │  Synthesis→Critique │   │  Streamlit │
└─────────────────┘   └──────────────────┘   └─────────────────────┘   └────────────┘
```

The design goal from CLAUDE.md: **a new source type is added as data (a new
`sources` row) + one new class, never a new code path through the pipeline.**
That requirement drives the ports-and-adapters (hexagonal) structure below.

### 1.2 Hexagonal / ports-and-adapters

The core (`domain`, `ingestion`, `reasoning`) depends only on **abstract ports**.
Concrete tech (psycopg, Ollama, requests, BeautifulSoup) lives in **adapters**
that implement those ports. This is what makes SOLID's Dependency-Inversion
concrete here and keeps the core unit-testable with fakes.

Ports:

| Port | Responsibility | Adapters |
|---|---|---|
| `SourceConnector` | fetch + parse a source into `RawDocument`s | `FariaEventsConnector`, `GmailMenuConnector` |
| `Embedder` | text → vector | `OllamaEmbedder`, `FakeEmbedder` (tests) |
| `ChatModel` | prompt → structured response | `OllamaChatModel`, `FakeChatModel` (tests) |
| `DocumentRepository` | upsert / vector-search / reconcile `Document`s | `PostgresDocumentRepository` |
| `SourceRepository` | load `Source`, update run status | `PostgresSourceRepository` |
| `SubscriberRepository` | look up an active token by hash | `PostgresSubscriberRepository` |
| `Clock` | `now()` (deterministic tests) | `SystemClock`, `FrozenClock` |

**`sources/` is its own layer**, not under `adapters/`: a source agent is a
first-class concept here (CLAUDE.md's "source agents" layer), it bundles a
connector + a parser + fixtures, and it implements the `SourceConnector` port
using whatever tech it needs (`httpx`, `bs4`). Everything else in the core still
imports only `ports`.

### 1.3 Domain model (pure, no I/O)

- **`RawDocument`** — what a connector emits (frozen pydantic). Fields are
  exactly the ingestable `documents` columns: `external_id`, `title`,
  `content`, `domain`, `category`, `event_date`, `published_at`, plus an
  optional `student_ref` hint. **No `metadata` field** — `documents` has no
  JSON column, so a connector renders any structured detail (times, upstream
  ids, location) into `content` as prose. `embedding_text` = title + content,
  de-duplicated. *(built)*
- **`Document`** — the persisted entity, 1:1 with the `documents` table.
- **`Source`** — 1:1 with `sources` (always `school_id`-anchored); exposes a
  typed view over the `config` JSONB (e.g. `FariaEventsConfig(calendar_url=...)`).
- **`Subscriber`** — 1:1 with `subscribers`. Access is a token check, not data
  scoping: `AuthService.authenticate(bearer_token)` hashes and looks up an
  active row, else 401. No `Visibility` model — single household.
- **`QueryState`** — LangGraph state (see 1.5).
- **Enums**: `Domain` (`school`, `daycare`, `gmail`, `financial`,
  `product_manual`), `Category` (`event`, `menu`, `reminder`, `activity`),
  `SourceStatus` (`ok`, `error`, `never_run`).

### 1.4 Ingestion pipeline (build step 4)

`IngestionService.run(source_id)` — the one code path all sources share:

1. `SourceRepository.get(source_id)` → `Source`
2. `ConnectorFactory.for_source(source)` → `SourceConnector`
   (registry keyed on `(domain, category)`; connector's ctor args come from a
   typed view over `source.config` — e.g. `FariaEventsConfig(calendar_url=…)`.
   Open/Closed — register new class, no factory edit beyond one line)
3. `connector.fetch()` → `list[RawDocument]`
4. `Embedder.embed([doc.embedding_text for doc in raw])` → vectors
5. `DocumentRepository.upsert(documents)` — `INSERT ... ON CONFLICT
   (source_id, external_id) DO UPDATE`, bumping `last_seen_at` always and
   `updated_at` only when `content`/`event_date`/`title` changed; clears
   `deleted_at` if the row had been soft-deleted.
6. **Reconcile deletions** — `UPDATE documents SET deleted_at = now() WHERE
   source_id = ? AND deleted_at IS NULL AND event_date >= today AND
   last_seen_at < run_started_at` (a cancelled "No School" day must stop
   being an answer).
7. `SourceRepository.mark_run(source_id, status, at=clock.now())`
   — wraps the whole thing in try/except so a crashing connector sets
   `last_status='error'` (CLAUDE.md gotcha: broken scraper must be
   *queryable*, not "dashboard looks stale")

Steps 1–7 run inside one try/except; a `ConnectorError` (the connector
self-enforces `robots.txt` and raises on a missing calendar container) sets
`last_status='error'`.

`external_id` is the connector's contract: **stable across re-runs** or step 5
inserts duplicates. Faria: Finalsite's `data-occur-id`
(`{eventId}_{startUTC}_{endUTC}`) used verbatim. Gmail: RFC-822 `Message-ID`.

The Faria grid gives **title, date and time only** — no event location or
description (those sit behind the robots-disallowed JSON endpoints). Don't plan
retrieval around fields we can't get.

**Ingestion cadence** — per source, stored in `sources.config` (e.g.
`{"schedule": "weekly:sat@06:00"}`), driven by an external scheduler
(`launchd`/cron → `scripts/run_ingestion.py --source <id>`), not by the
connector. Current sources:

| Source | Cadence | Why |
|---|---|---|
| `faria_events` | **weekly, Saturday ~06:00 Pacific** | school events are planned weeks ahead; a weekly refresh over the weekend has the dashboard current for the week |
| `gmail_menu` (later) | TBD — likely daily | menus turn over daily |

**Reference only — do not scrape:** `cusdk8nutrition.com`,
`schoolnutritionandfitness.com` (menu URL e.g.
`cusdk8nutrition.com/index.php?...&page=menus`). Both serve
`robots.txt: Disallow: /` (re-confirmed 2026-09-07). The menu reaches ACAMAR
via the Gmail connector, never a scraper — settled in CLAUDE.md.

A manual `run_ingestion.py --source <id>` is always available for
out-of-band updates. Later optimisation: conditional requests (store
`ETag`/`Last-Modified` in `sources.config`, skip parse on `304`).

### 1.5 Reasoning graph (LangGraph, build steps 5 + 8)

**Built 2026-09-17 — this section predates the build and is stale on two
specifics (not the four-layer shape).** See
[docs/reasoning-pipeline-design.md](reasoning-pipeline-design.md) for what
was actually shipped: Router's first decision is a 4-way intent
(document/weather/count/out_of_scope), not a single `(domain, category)`
pair — categories only apply within the `document` intent, and there are
three no-LLM branches (weather/count/decline) alongside the
Retrieval→Synthesis→Critique loop below (§2.3 there). Structured output
also ended up going through plain `ollama` + JSON schema, not
`.with_structured_output()` — §3.4 below is stale on that point too, see
Decision D.

```
        ┌────────┐     ┌───────────┐     ┌───────────┐     ┌──────────┐
 query─▶│ Router │────▶│ Retrieval │────▶│ Synthesis │────▶│ Critique │──▶ answer
        └────────┘     └───────────┘     └───────────┘     └────┬─────┘
                            ▲                                   │ not grounded
                            └───────────────────────────────────┘ (retry ≤ N)
```

- **`QueryState`** (pydantic): `question`, `domain`, `category`,
  `date_from` / `date_to`, `retrieved: list[Document]`, `draft_answer`,
  `critique: CritiqueVerdict | None`, `retry_count`, `final_answer`.
- **RouterNode** — `ChatModel.structured(RouteDecision)` → `(domain, category)`.
  Constrained pydantic output; no text parsing.
- **RetrievalNode** — builds a filter from state, calls
  `DocumentRepository.search(embedding, filter)`. No ownership check (single
  household); the filter is just:
  `WHERE d.deleted_at IS NULL`
  `AND d.domain = :domain AND d.category = :category`
  `AND (d.event_date BETWEEN :from AND :to OR d.event_date IS NULL)`
- **SynthesisNode** — `ChatModel` drafts from retrieved context only.
  Context assembly (and `num_ctx` truncation — CLAUDE.md gotcha) lives in one
  `RetrievalContext` builder.
- **CritiqueNode** — CRAG grader: `ChatModel.structured(CritiqueVerdict)`
  checks the draft is supported by retrieved docs. If not and
  `retry_count < N`: bump count, loop to Retrieval (widen date window / drop
  category). Else return best-effort answer with a low-confidence flag.
- Every node is a small class; its LLM/repo collaborators are injected. No node
  calls Ollama or psycopg directly.

### 1.6 Delivery (build steps 6 + 7) — BUILT

- **FastAPI** — routers are thin; each endpoint delegates to a service
  (`QueryService`, `DashboardService`, `SourceStatusService`). Services and
  repos provided via `Depends`. Endpoints: `GET /health` (open),
  `GET /dashboard`, `POST /query`, `GET /sources/{source_id}/status`.
- **Auth** — a `require_token` dependency on every route except `/health`:
  reads `Authorization: Bearer <token>`, sha256, `SubscriberRepository`
  lookup for an active row, else `401`. Tokens are created/revoked with
  `scripts/token.py` (writes `subscribers`).
- **Streamlit** — calls the FastAPI endpoints over HTTP with the token, never
  the DB directly. One data-access path; the dashboard stays a pure view.

### 1.7 Gmail menu connector (build step 3) — BUILT except the parser

Built 2026-09-15: `MailClient` port, `GoogleGmailClient` adapter (OAuth +
MIME→`RawEmail`), `GmailMenuConnector` shell, registered in
`connector_factory`. Still blocked on the district's menu actually being
subscribed to email and a real sample to parse against — `parser.py` stays a
stub until then. What follows is buildable **without** the sample; the parser
is explicitly deferred.

**New port — `MailClient`** (`ports/mail_client.py`), for the same reason every
other integration has one: `GmailMenuConnector` shouldn't know it's talking to
Gmail API v1 specifically, and needs to be unit-testable without real OAuth.

```python
class RawEmail(BaseModel):          # frozen
    message_id: str      # RFC822 Message-ID header — stable, feeds external_id
    subject: str
    sender: str
    received_at: datetime
    text_body: str | None
    html_body: str | None

class MailClient(Protocol):
    async def search(self, query: str, *, max_results: int = 20) -> list[RawEmail]: ...
```

`search` returns fully-hydrated `RawEmail`s, not just ids — the port hides
Gmail's list-then-get two-step API. Reusable by any future Gmail-sourced
connector (not just the menu).

**Adapter — `GoogleGmailClient`** (`adapters/gmail/client.py`):
- OAuth via `google-auth-oauthlib`: loads `secrets/gmail_credentials.json`,
  opens a one-time local-browser consent, caches the refresh token at
  `secrets/gmail_token.json` (both gitignored). Later runs load + auto-refresh
  — no repeated logins.
- The Gmail API client library is **sync**; wrap its calls in
  `asyncio.to_thread(...)` to keep `search` async, consistent with the rest of
  the codebase (decision #5).
- Internally: `messages.list(q=query)` → ids → `messages.get(id, format="full")`
  per message → mapped to `RawEmail` by a **pure function**
  `_to_raw_email(message: dict) -> RawEmail`, which calls
  `_extract_bodies(payload: dict)` (recursively walks MIME `parts` for
  `text/plain` / `text/html`, base64url-decodes `body.data`).
- Keeping the dict→`RawEmail` mapping pure mirrors `parse_calendar_html`:
  testable against synthetic Gmail-API-shaped dicts, no network/OAuth needed
  for those tests. Same "inspect real structure, keep parsing pure" discipline
  — just one layer removed (mail transport, not menu content).

**Connector — `GmailMenuConnector`** (`sources/gmail_menu/connector.py`):
implements `SourceConnector` (`name="gmail_menu"`). Self-defaults a
`GoogleGmailClient` from `get_settings()` if none is injected — same pattern
as `FariaEventsConnector` defaulting its `httpx.AsyncClient`. `fetch()`:
`mail.search(query)` → `parse_menu_email(email)` per message → flatten →
any parser exception wrapped in `ConnectorError` (same broad-catch-and-re-raise
shape as Faria's connector).

**`sources/gmail_menu/parser.py` is a stub** (`raise NotImplementedError`)
until a real email is captured — not guessed at.

**`external_id` decision (settled now — it affects the dedup constraint even
before the parser exists):** one email typically carries a whole week, so the
key can't be the bare `Message-ID`. Use:
```
external_id = f"{message_id}::{event_date.isoformat()}"
```
one row per calendar day per email.

**Config additions:**
```python
gmail_credentials_path: str = "secrets/gmail_credentials.json"
gmail_token_path: str = "secrets/gmail_token.json"
```
No default search query — it must be set from the real sender once known;
leaving it unset should fail loudly rather than silently matching nothing.

**Source row (once live):** `domain=GMAIL, category=MENU,
config={"query": "from:<real sender>", "schedule": "daily@06:00"}` — daily,
since menus turn over daily (already noted in §1.4's cadence table).

**Shipped vs. still blocked:**

| Shipped (no sample needed) | Blocked on a real sample |
|---|---|
| `MailClient` port | `sources/gmail_menu/parser.py` |
| `GoogleGmailClient` (OAuth, search, MIME→`RawEmail`) | the real Gmail search `query` |
| config + deps (`google-api-python-client`, `google-auth-httplib2`, `google-auth-oauthlib`) | the `sources` row's `config` |
| `GmailMenuConnector` orchestration shell, registered in `connector_factory` | |
| `FakeMailClient` + unit tests for the MIME mapping | |

OAuth client secret is at `secrets/gmail_credentials.json` (Google Cloud app
created 2026-09-15); `secrets/gmail_token.json` doesn't exist yet — the
one-time local-browser consent hasn't been run. That happens automatically
the first time `GoogleGmailClient.search` actually runs against a real
`sources` row.

**Test plan:** `_extract_bodies` / `_to_raw_email` against synthetic message
dicts (plain-text only, HTML only, multipart, nested multipart, missing
`Message-ID`, malformed base64) — no live Gmail needed. `FakeMailClient` for
`GmailMenuConnector`/`IngestionService` tests. A
`TestGmailMenuConnectorContract(SourceConnectorContract)` once the parser is
real, alongside the Faria one.

---

## 2. Project structure

`src/` layout (not flat) — forces testing the installed package, kills
import ambiguity, standard for modern packaging.

```
acamar/
├── pyproject.toml            # single source of config: deps, ruff, mypy, pytest
├── uv.lock                   # the lockfile (decision #4)
├── .python-version           # 3.13, pinned for uv / CI / Docker
├── requirements.txt          # generated: `uv export` — for non-uv consumers
├── .pre-commit-config.yaml
├── .env.example
├── docker-compose.yml
├── Dockerfile
├── .dockerignore
├── README.md
├── CLAUDE.md
├── db/
│   └── schema.sql            # single source of truth for the schema (decision #3)
├── docs/
│   ├── architecture.md       # this file
│   └── portfolio.md          # build step 11
├── src/acamar/
│   ├── config.py             # pydantic-settings: env → typed Settings
│   ├── domain/               # pure models + enums, zero I/O imports
│   │   ├── models.py
│   │   └── enums.py
│   ├── ports/                # ABCs / Protocols only
│   │   ├── source_connector.py
│   │   ├── embedder.py
│   │   ├── chat_model.py
│   │   ├── repositories.py
│   │   └── clock.py
│   ├── adapters/
│   │   ├── db/
│   │   │   ├── pool.py
│   │   │   ├── document_repository.py
│   │   │   ├── source_repository.py
│   │   │   └── subscriber_repository.py
│   │   ├── embedding/ollama_embedder.py
│   │   ├── llm/ollama_chat_model.py
│   │   └── http/httpx_client.py
│   ├── sources/
│   │   ├── faria_events/           # BUILT
│   │   │   ├── connector.py
│   │   │   └── parser.py
│   │   └── gmail_menu/
│   │       ├── connector.py
│   │       └── parser.py
│   ├── ingestion/
│   │   ├── connector_factory.py
│   │   └── ingestion_service.py
│   ├── reasoning/
│   │   ├── graph.py
│   │   ├── state.py
│   │   ├── context.py             # RetrievalContext + num_ctx budget
│   │   └── nodes/{router,retrieval,synthesis,critique}.py
│   ├── api/
│   │   ├── app.py
│   │   ├── auth.py                # require_token dependency (subscribers lookup)
│   │   ├── dependencies.py
│   │   ├── schemas.py             # request/response pydantic models
│   │   └── routers/{health,query,dashboard,sources}.py
│   └── dashboard/streamlit_app.py
├── scripts/
│   ├── seed.py                    # Faria school + one token + faria_events source
│   ├── token.py                   # create / revoke a subscriber token
│   ├── reset_db.sh               # dropdb / createdb / apply schema / re-ingest
│   └── run_ingestion.py           # CLI: python -m ... --source <id>
└── tests/
    ├── conftest.py
    ├── fixtures/faria_events/     # captured page + expected_events.json snapshot
    ├── fakes/                     # FakeChatModel, FakeEmbedder, in-memory repos
    ├── contract/                  # suite every SourceConnector must pass  (BUILT: Faria)
    ├── unit/                      # BUILT: parser, connector, config, models, enums
    ├── integration/              # real Postgres via testcontainers
    └── e2e/
```

Test HTML lives under `tests/fixtures/`, not in the package — it isn't shipped
in the wheel. pytest runs in `--import-mode=importlib` (no `__init__.py` in
`tests/`).

### 2.1 How SOLID maps to this layout

- **S** — a connector only fetches+parses; embedding, persistence, status
  tracking are separate classes. Parser is split from connector (HTTP vs
  HTML logic).
- **O** — new source = new `sources/<name>/` package + `@register(domain,
  category)` on the class. `IngestionService`, factory, repos untouched.
- **L** — every `SourceConnector` returns `list[RawDocument]`; every
  `ChatModel` returns the requested pydantic type. Fakes are drop-in.
- **I** — ports are single-purpose. Incremental-sync sources implement an
  extra `SupportsCursor` protocol rather than bloating `SourceConnector`.
- **D** — core imports `ports`, never `adapters`. Wiring happens once in
  `api/dependencies.py` and `scripts/`.

### 2.2 OO vs. "functions with types"

Classes where there's **state or a swappable implementation**: connectors,
repositories, services, graph nodes, adapters. Plain functions for **pure
transforms**: parsers' field extractors, filter/SQL builders, context
assembly. Not every function becomes a class — that's Java-in-Python and hurts
readability.

---

## 3. Tooling, linting, naming

### 3.1 Stack

| Concern | Tool | Notes |
|---|---|---|
| Lint + format | **Ruff** | replaces flake8 / isort / black / pyupgrade / pep8-naming |
| Types | **mypy** `--strict` on `src/acamar` | LangChain: targeted `ignore_missing_imports` |
| Boundary data | **pydantic v2** | config, API I/O, LLM structured output, `RawDocument` |
| Hooks | **pre-commit** | ruff, ruff-format, mypy, detect-secrets, check-yaml, EOF/whitespace |
| Package mgr | **uv** (decision #4) | `uv sync`, `uv add`, `uv run` |
| Task runner | `make` or `uv run` scripts | `make lint test up seed` |
| Migrations | none (MVP) — `db/schema.sql` + `scripts/reset_db.sh` (decision #3) | |

Concrete lint/type patterns to write to first-time-clean:
[`docs/coding-standards.md`](coding-standards.md).

### 3.2 Ruff rule sets (in `pyproject.toml`)

`E,F,W` (pycodestyle/pyflakes), `I` (imports), `N` (pep8-naming),
`UP` (pyupgrade), `B` (bugbear), `SIM` (simplify), `PTH` (use pathlib),
`ANN` (require annotations), `D` (pydocstyle, `convention="google"`,
relaxed for tests), `RUF`, `ASYNC`, `S` (bandit — security; Gmail creds).
Line length 100.

### 3.3 Naming conventions (enforced by Ruff `N` + review)

- `snake_case` funcs/vars/modules, `PascalCase` classes, `UPPER_SNAKE`
  module constants, `_leading_underscore` for non-public.
- Domain-meaningful, unabbreviated: `raw_document` not `rd`, `event_date`
  not `dt`, `connector` not `conn` (except the DB `connection`).
- Allowed short names: `db`, `id`, `url`, `llm`, `ctx` (only in the context
  builder), loop `i`.
- Ports named for the role (`Embedder`), adapters for role+tech
  (`OllamaEmbedder`). Test doubles prefixed `Fake`.
- No Hungarian notation, no `Manager`/`Helper`/`Util` grab-bag classes.

### 3.4 AI-project practices

- **Model names & params only in `config.py`** — never inline string literals.
- **All model calls go through a port** (`ChatModel`, `Embedder`) — a node
  calling `ollama.chat()` directly is a review-block. Makes tests
  deterministic and swaps Ollama→hosted trivial later.
- **Structured output via `pydantic`** — no regex/JSON-scraping of free
  text. Originally planned through LangChain's `.with_structured_output()`;
  what actually got built (`OllamaChatModel`) calls
  `ollama.AsyncClient.chat(..., format=schema.model_json_schema())`
  directly instead, matching `OllamaEnricher`'s already-proven pattern —
  see docs/reasoning-pipeline-design.md Decision D.
- **Prompts are versioned constants** in `reasoning/nodes/<node>/prompts.py`,
  not scattered f-strings. Each has a short changelog comment.
- **Context budget centralized** — one `RetrievalContext` owns truncation to
  `settings.ollama_num_ctx` (CLAUDE.md gotcha).
- **Embedding dimension is a config constant** asserted at startup against the
  actual `documents.embedding` column — fail fast on mismatch. (For pgvector,
  read the dim via `format_type(atttypid, atttypmod)` or
  `pg_catalog` — plain `atttypmod` needs care; verify the exact query when
  building the check.)
- **No secrets in code or logs** — `detect-secrets` hook; structured logging
  with a redaction filter for anything from `gmail_menu`.

### 3.5 Logging & run observability

The scraper runs weekly and unattended — a failed or empty run has to be
*visible*, not just recorded. Each ingestion run emits one structured log line
(JSON) with: `source`, `duration_ms`, `fetched`, `upserted`, `deleted`,
`status`, and on failure the `ConnectorError` message + `source` attribute.
`sources.last_run_at` / `last_status` are the queryable version of the same
thing. A run that ingests **0** events where the last run had >0 is logged at
`WARNING`.

---

## 4. Test strategy

Pyramid, fakes at the core, one real Postgres for the persistence edge.

### 4.1 Unit (most tests, no I/O, milliseconds)

- **Parsers** — synthetic minimal HTML for edge cases (entities, no end time,
  all-day-wins, month-boundary multi-day, missing container → raises), plus a
  **snapshot test** of the real captured page against
  `tests/fixtures/faria_events/expected_events.json`, plus idempotency.
- **Retrieval filter builder** — assert the generated SQL + params enforce
  ownership, domain/category, date window.
- **Graph nodes** — inject `FakeChatModel` with canned structured verdicts;
  assert state transitions and that the CRAG loop is capped at `N`.
- **Domain models** — validation (e.g. `Source` rejects "both owners" the way
  the DB CHECK does, so we fail before the round-trip).

### 4.2 Integration (real Postgres + pgvector; fake LLM / HTTP)

- **testcontainers-python** starts `pgvector/pgvector:pg17` once per session;
  `db/schema.sql` applied; **each test in a rolled-back transaction**.
- `DocumentRepository.upsert` dedup: same `external_id` twice → 1 row, fields
  updated, not duplicated.
- Vector search returns nearest neighbours **and** excludes soft-deleted rows
  and rows outside the domain/category/date filter.
- `IngestionService.run` with a stub connector → rows land, `last_status='ok'`;
  a second run missing one future event → that row gets `deleted_at`; stub
  raises → `last_status='error'`, `last_run_at` still set.
- `AuthService.authenticate` — valid active token → `Subscriber`; unknown /
  revoked / malformed → raises, no timing oracle (constant-time compare).

### 4.3 Connector contract suite

One parametrized test module every `SourceConnector` implementation is run
through: emits well-formed `RawDocument`s, stable `external_id`, tolerates an
empty result, raises a typed `ConnectorError` (not a bare exception) on
fetch failure.

### 4.4 E2E (few, `@pytest.mark.e2e`, opt-in)

FastAPI `TestClient` + real DB + `FakeChatModel`. Seed a subscriber token,
ingest from a stub connector, hit `POST /query` and `GET /dashboard` with the
token → shape assertions; without / with a bad token → `401`.

### 4.5 Eval harness (build step 9 — separate from CI pytest)

Golden Q&A set (`tests/eval/golden_qa.jsonl`, spans all four intents, not
just menu/events), LLM-as-judge scores grounding + correctness via
`scripts/run_eval.py`. Run on demand; track score over time. **Not a
pass/fail gate** while the corpus is tiny.

### 4.6 Live smoke (scheduled, not CI)

A job actually hits `faria.cusdk8.org`, runs the real connector, asserts >0
events and expected fields. This is the "detect a site redesign" net.

**Run it daily**, even though ingestion is weekly — a weekly job that breaks
won't retry for a week, so the smoke test is the fast-feedback path. It writes
`sources.last_status` so breakage is queryable, and the connector's own
robots-check + calendar-container assertion mean most breakage already surfaces
as a `ConnectorError` rather than a silent empty result.

### 4.7 Tooling & targets

pytest, pytest-cov, pytest-asyncio, testcontainers, `respx` (httpx mocking),
plain builder helpers in `tests/fakes/`. Coverage **≥ 85 % on `src/acamar`**,
`dashboard/` excluded. CI: ruff → mypy → unit+integration → (e2e on label).

---

## 5. Schema

[`db/schema.sql`](../db/schema.sql) — **rewritten 2026-09-08** for the
single-household / token-auth model (see §7). Four tables:

| Table | Purpose |
|---|---|
| `schools` | anchor for a source (only Faria) |
| `subscribers` | sha256 hashes of bearer tokens; `revoked_at IS NULL` = active |
| `sources` | per-connector ingestion config; `school_id NOT NULL` |
| `documents` | shared retrieval table; `student_ref` free text, soft-delete via `deleted_at` |

Dropped: `families`, `users`, `students`, `notification_preferences`, the
`sources_single_owner` CHECK, `documents.student_id` FK.
Added: `subscribers`, `documents.{updated_at, last_seen_at, deleted_at}`.
`embedding` is `vector(768)` (nomic-embed-text). HNSW index retained.

**Not yet applied to the running DB** — needs `dropdb acamar && createdb
acamar && psql -f db/schema.sql` (no data to preserve).

### Open observations

1. **`documents.domain/category` are free `text`.** Denormalised on purpose.
   Consider a CHECK against the enum values so a connector typo can't create
   an unroutable document.
2. **No index on `sources.last_status`.** Fine at 1–5 sources; revisit later.

---

## 6. Containerization (for discussion)

### 6.1 Proposal: compose the infra now, host-run the app until deploy

| Service | Now | Image | Why |
|---|---|---|---|
| `db` | **containerize** | `pgvector/pgvector:pg17` | reproducible, extension baked in, matches future RDS/Cloud SQL, removes the "install Postgres" friction we just hit |
| `ollama` | **host** (Mac) / container (Linux CI) | `ollama/ollama` | Mac Docker has no Metal GPU passthrough; native Ollama is much faster. Abstracted by `OLLAMA_BASE_URL` |
| `api` | host venv (`uvicorn --reload`) | build later from `Dockerfile` | fast iteration during active dev |
| `dashboard` | host venv | build later | same |
| `db-init` | one-shot compose service | postgres image | applies `db/schema.sql` on `up` |

`docker-compose.yml` now contains just `db` (+ `ollama` on Linux). App
Dockerfiles land when we approach build step ~10 / deployment.

### 6.2 Production image (later)

Multi-stage: `python:3.13-slim` builder installs into a venv with `uv`;
runtime stage copies venv + `src`, runs as non-root, `HEALTHCHECK` hits
`/health`. One image, different `CMD` for api / dashboard / ingestion worker.

### 6.3 Decision needed — host Postgres conflict

We installed `postgresql@17` via brew and it's **running on 5432**. If we
adopt the `db` container, we must free that port:
`brew services stop postgresql@17` (keep it installed as a fallback, or
`brew uninstall` for a clean single-source-of-truth). Can't run both on 5432.

---

## 7. Decisions log

| # | Decision | Resolution | Status |
|---|---|---|---|
| 4 | Package manager | **uv** — `pyproject.toml` + `uv.lock`; `requirements.txt` exported for compat | ✅ done (2026-09-07) |
| 5 | Sync vs async | **async end to end** — `psycopg_pool.AsyncConnectionPool`, async graph nodes, async FastAPI, `pytest-asyncio` | ✅ agreed |
| 3 | Migrations | **No migration tool for the MVP.** `db/schema.sql` stays the single source of truth; schema change = edit + `scripts/reset_db.sh` (dropdb / createdb / apply / re-ingest), since all data is cheaply re-derivable. Revisit (plain numbered `.sql` + runner, or Alembic if SQLAlchemy is adopted) at **first cloud deploy** or **first expensive-to-rebuild data** (e.g. paid embedding API + large corpus). | ✅ agreed |
| 6 | Config | **pydantic-settings** — `src/acamar/config.py` maps env → typed `Settings` | ✅ agreed (dep added) |
| 10 | Tenant model | **Single household. No families/users/students.** Access = opaque bearer tokens in `subscribers` (sha256, `revoked_at`); any active token authorizes every read. Re-add a tenant boundary only if shared with data separation. Departs from CLAUDE.md's original "multi-tenant from day one" — CLAUDE.md + `schema.sql` updated to match. | ✅ agreed (2026-09-08) |
| 1 | Embedding model + vector dim | **`nomic-embed-text` → `vector(768)`** — in the rewritten `schema.sql`; change the column if the model changes. | ✅ folded into the schema rewrite |
| 7 | `documents` timestamps | **`updated_at` + `last_seen_at` + `deleted_at`** added — soft-delete + deletion reconcile (§1.4 step 6). | ✅ folded into the schema rewrite |
| 9 | Denormalise ownership onto `documents` | **Dropped** — no ownership to denormalise (decision #10). Retrieval filters `deleted_at IS NULL` + domain/category/date. | ✅ moot |
| 2 | Postgres: host vs container | recommend **`pgvector/pgvector:pg17` via compose**, `brew services stop postgresql@17` (can't share port 5432). | ⏳ open (non-blocking — host PG works today) |
| 8 | Containerize app now | recommend **defer** app images; compose only `db`. | ⏳ open (non-blocking) |

**Done:** step 2 — `src/acamar/{domain,ports,config,sources/faria_events}`,
`parse_calendar_html` + `FariaEventsConnector` (robots-check, retry, throttle,
redesign detection), unit + contract tests, captured fixture + snapshot.
Schema rewritten for decision #10 (not yet applied to the running DB).

**Next (P0/P1):** apply the new schema, `scripts/seed.py` (school + one token +
`faria_events` source), then the ingestion slice — repositories +
`IngestionService` + `run_ingestion.py` + `OllamaEmbedder`.
