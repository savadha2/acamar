# ACAMAR

A dashboard and query system for daily-care logistics: ingests school/daycare
information sources into a shared, queryable store, so questions like
"what's for breakfast today" or "what events are this week" get answered
without checking multiple websites and emails manually.

Single household — access is gated by opaque bearer tokens, not a
families/users model. 

## Current scope

Daughter's school (Faria Elementary) only, four sources:

- **Events** — scraped from Faria's public calendar (static HTML, no login
  required, confirmed allowed by robots.txt).
- **Menu** — manual PDF upload via the Pilot Cockpit admin app
  (`sources/manual_menu/`), parsed against real vendor PDFs. The district's
  nutrition vendor sites explicitly disallow automated access in their
  robots.txt, so there is no scraper against either of them — a deliberate
  decision. A Gmail-subscription path for the menu was explored and later
  cancelled; manual upload is the sole menu source.
- **Events/reminders from email** — school and ParentSquare emails that
  carry an event or a deadline, via a Gmail connector (also the same
  connector the (cancelled) Gmail menu path used).
- **Cupertino Parks & Rec events** — scrapes `cupertino.gov/Parks-Recreation/Events`
  for community events, independent of the school. The first source outside
  `Domain.SCHOOL`.

Son's daycare, financial, and product-manual domains: schema and
architecture already support them; not built until the daughter's MVP is
fully working end-to-end.

## Architecture

Four layers: source agents (scrapers, Gmail filters) → a shared document
store (Postgres + pgvector) → reasoning agents (router → retrieval →
synthesis → CRAG-style critique/re-retrieval) → delivery (FastAPI, a
Streamlit household dashboard, and a separate admin "Pilot Cockpit" app).
See `db/schema.sql` for the full data model and `CLAUDE.md` for the current
build order and standing gotchas.

Access is token-gated, not data-scoped: any active bearer token sees
everything (no families/schools/tenant model — see `CLAUDE.md` if that ever
needs to change). Multi-domain extensibility is still a goal: new sources
plug in as a `sources` row + one connector class, never a new code path.

## Stack

- **Language:** Python 3.13
- **Orchestration:** LangChain / LangGraph (router, retrieval, synthesis,
  critique as a LangGraph graph; an LLM-as-judge evaluator over a golden
  Q&A set)
- **Database:** PostgreSQL + pgvector (HNSW index, `vector(768)` for
  `nomic-embed-text`)
- **Backend:** FastAPI
- **Dashboard:** Streamlit — the household-facing dashboard, plus a
  separate admin "Pilot Cockpit" app (manual menu upload, source health,
  pause/resume, kill switch)
- **LLM:** local via Ollama (`llama3` by default)
- **Scraping:** `httpx` + BeautifulSoup, with a hand-written robots.txt
  matcher (the stdlib's doesn't support real sites' `*`/`$` wildcards)

## Setup

```bash
uv sync                       # Python 3.13 venv + deps (uv.lock)
cp .env.example .env           # DATABASE_URL, Ollama, SCRAPER_*, COCKPIT_TOKEN
createdb acamar && psql acamar -f db/schema.sql
uv run python -m scripts.seed  # school + an access token + the faria_events source
```

Access tokens are created/revoked with `scripts/token.py` (stored hashed,
not in `.env`). Dev tooling: `uv run ruff check`, `uv run mypy`,
`uv run pytest` — see `docs/coding-standards.md` for the conventions that
keep those green.

## Project status

Past the MVP: all four sources above are built and ingesting; the full
reasoning pipeline (router → retrieval → synthesis → CRAG critique/retry →
judge-evaluated) is live behind `POST /query`; the Streamlit dashboard and
the Pilot Cockpit admin app are both running. 453+ tests passing,
ruff/mypy clean.

Runs entirely bare-metal/local today (API by hand, both Streamlit apps by
hand, ingestion on a local `launchd` cron, local Postgres, local Ollama).
**Deploying this to GCP is the current top backlog priority** — containerizing
the app and choosing real hosting for Postgres+pgvector, LLM serving, and
secrets are still open design questions. Full detail, and everything else in
flight, is tracked in the project's backlog (ask for it, or see
`CLAUDE.md`'s build order).
