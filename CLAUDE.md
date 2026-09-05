# ACAMAR — Project Context

This file is scoped to **execution only**: what to build, why, and in what
order. It does not include learning materials, study plans, or career-plan
content — those exist separately and aren't relevant to writing this code.

## What this project is

A dashboard and query system for daily-care logistics. Scrapes/ingests
school and daycare information sources into a shared, queryable store, so
questions like "what's for breakfast today" or "what events are this week"
get answered without checking multiple websites and emails manually.

Designed for multi-tenancy (other parents at the same school can eventually
share event/menu data) and multi-domain extensibility (Gmail, financial,
product-manual sources) from day one — even though only one school's data
is implemented first. New source types should be addable as data (new
`sources` rows), not new code paths.

## Current scope (build this first, nothing else)

**Daughter's school only (Faria Elementary), two sources:**

1. **Events** — scrape Faria's public calendar at `faria.cusdk8.org`.
   No login required. Confirmed allowed by robots.txt.
2. **Menu** — via a Gmail-parsed auto-subscription email, **not scraping**.
   Both `cusdk8nutrition.com` and `schoolnutritionandfitness.com` (the
   district's nutrition vendors) explicitly disallow automated access in
   their robots.txt. Do not build a scraper against either domain — this
   was a deliberate decision, not an oversight. The menu source is a
   filtered view into a Gmail connector instead.

Son's daycare, other parents, Gmail/financial/product-manual domains beyond
the menu use case: **out of scope until the daughter's MVP is working
end-to-end.** The schema and architecture already support them; the code
does not need to yet.

## Architecture

Four layers:

```
Source agents  ->  Document store  ->  Reasoning agents  ->  Delivery
(scrapers,          (Postgres +         (router, retrieval,   (FastAPI,
 Gmail filters)      pgvector)           synthesis, critique)  Streamlit,
                                                                push alerts)
```

**Reasoning agent flow:** Router (classifies domain + category) ->
Retrieval (pulls tagged documents, filtered by student/family/school
ownership) -> Synthesis (drafts answer from retrieved context) ->
Critique/Grader (CRAG-style: checks the answer is actually supported by
retrieved context; loops back to Retrieval if not) -> Final answer.

**Why multi-agent, not one pipeline:** each source has fundamentally
different ingestion logic but must produce the same normalized document
shape; sensitivity varies by domain (a school menu is shared, financial
data is private) and needs to be enforced consistently; routing by domain
first, then category, stays clean as more domains are added. Not every
"agent" needs to be an LLM call — source agents are mostly plain code
behind a shared interface; the LLM-based agents are Router, Synthesis,
Critique, and Evaluator.

## Data model

Full DDL lives in `db/schema.sql` — apply it, don't recreate it from
scratch. Key design decisions, so they aren't accidentally undone:

- **Visibility is derived, not a stored flag.** A `sources` row belongs to
  EITHER a school (shared with every family enrolled there) OR a family
  (private) — enforced by a `CHECK` constraint, never both, never neither.
  Every document inherits this from its source. Do not add a separate
  `sensitivity`/`visibility` column to `documents` — that would let it
  drift out of sync with the actual ownership.
- **`documents.student_id` is nullable.** Not every document is about a
  specific kid (financial, product-manual docs won't be). Don't make this
  required.
- **`documents.external_id` + `UNIQUE (source_id, external_id)`** is the
  dedup key. Every ingestion path (scraper, Gmail parser) must set this
  to something stable, or re-running ingestion will insert duplicates.
- **`domain`/`category` are denormalized onto `documents`** (not only
  reachable via a join through `sources`) because the Router/Retrieval
  agents filter on them on every single query.
- **Vector index: HNSW**, not IVFFlat — already set this way in
  `schema.sql`. Don't switch back; IVFFlat needs a non-trivial amount of
  data before it clusters well, HNSW doesn't have that constraint.
- **LangGraph's own checkpointer/state tables are separate from this
  schema.** They live in the same Postgres instance but are managed by
  LangGraph's `PostgresSaver`, not defined here.

## Stack

- **Language:** Python 3.13 (not 3.14 — langchain-core has open
  compatibility issues on 3.14 as of this writing)
- **Orchestration:** LangChain / LangGraph
- **Database:** PostgreSQL + pgvector (extension name is `vector`, not
  `pgvector` — easy to get wrong)
- **Backend:** FastAPI
- **Dashboard:** Streamlit for the MVP
- **LLM:** local via Ollama (Llama 3 / Phi-3)
- **Scraping:** requests + BeautifulSoup (Faria's calendar is static
  HTML; confirm this is still true before assuming — sites change)

## Environment setup (already documented in README.md)

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in DATABASE_URL, Ollama config
createdb acamar
psql acamar -f db/schema.sql
```

## Build order (do not reorder without a reason)

1. Apply `db/schema.sql` to a local Postgres instance; confirm tables +
   `vector` extension exist (`\dt`, `\dx` in psql).
2. Faria calendar scraper (events) — inspect the real page structure
   first, don't guess selectors. Must populate `external_id` per event
   for dedup.
3. Gmail connector + menu subscription parsing — set up the auto-email
   subscription first (manual, one-time), then build the parser against
   real sample emails.
4. Ingestion pipeline: normalize both sources into `documents` rows,
   generate embeddings, handle dedup via `external_id`.
5. LangGraph router + retrieval (menu / events / activities / reminders
   categories for now — activities/reminders may have no data yet, route
   for them anyway).
6. FastAPI backend: `GET /health`, `GET /dashboard/{student_id}`,
   `POST /query`, `GET /sources/{source_id}/status`.
7. Streamlit dashboard: today's menu/events/reminders at a glance, plus a
   free-text query box.
8. CRAG critique/re-retrieval loop.
9. LLM-as-judge evaluator + a small golden dataset (menu/events Q&A).
10. Notification agent + per-user push toggle (`notification_preferences`
    table already exists for this).
11. Portfolio page + system diagram (PRD-style format).

## Known gotchas (don't rediscover these the hard way)

- The two nutrition vendor sites are scrape-blocked by robots.txt — see
  "Current scope" above. This is settled, not open for debate.
- Ollama's `num_ctx` limits how much retrieved context actually fits in a
  prompt — if retrieval starts returning several documents per query,
  check this isn't silently truncating context.
- `sources.last_run_at` / `last_status` exist specifically so a broken
  scraper (e.g. after a site redesign) is detectable via a query, not
  discovered by "the dashboard looks stale."
