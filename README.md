# ACAMAR

A dashboard and query system for daily-care logistics: scrapes and ingests
school/daycare portals (and, eventually, Gmail, financial, and product-manual
sources) into a shared, queryable store, so questions like "what's for
breakfast today" or "what events are this week" get answered without digging
through five different websites and emails.

Built as a hands-on technical proving ground alongside a career transition
into technical product management at a frontier AI lab -- see
`docs/portfolio.md` (added later) for the full write-up.

## Current scope (MVP)

Daughter's school (Faria Elementary) only, two sources:

- **Events** -- scraped from Faria's public calendar (Finalsite site, no
  login required, robots.txt allows it)
- **Menu** -- parsed from an auto-subscribed email (the district's nutrition
  vendor's site disallows scraping, so this goes through the Gmail
  connector instead)

## Architecture

Four layers: source agents -> shared document store -> reasoning agents
(router / retrieval / synthesis / critique) -> delivery (dashboard, API,
push notifications). See `db/schema.sql` for the full data model.

Designed for multi-tenancy (other parents at the same school can eventually
share event/menu data) and multi-domain extensibility (Gmail, financial,
product-manual sources plug in as additional `sources` rows without a schema
change) from day one, even though only the daughter's school is implemented
first.

## Stack

- **Orchestration:** LangChain / LangGraph
- **Database:** PostgreSQL + pgvector
- **Backend:** FastAPI
- **Dashboard:** Streamlit (MVP) -> mobile-reachable web app later
- **LLM:** local via Ollama (Llama 3 / Phi-3) initially

## Setup

```bash
# 1. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
# edit .env with your local Postgres connection string

# 4. Create the database and apply the schema
createdb acamar
psql acamar -f db/schema.sql
```

## Project status

Phase 1 (foundations): repo + schema set up. Scraper and Gmail ingestion
not yet built.
