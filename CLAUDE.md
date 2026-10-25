# ACAMAR — Project Context

This file is scoped to **execution only**: what to build, why, and in what
order. It does not include learning materials, study plans, or career-plan
content — those exist separately and aren't relevant to writing this code.

## What this project is

A dashboard and query system for daily-care logistics. Scrapes/ingests
school and daycare information sources into a shared, queryable store, so
questions like "what's for breakfast today" or "what events are this week"
get answered without checking multiple websites and emails manually.

**Single household.** No families/users/students model — access to the
dashboard/API is gated by opaque bearer tokens (`subscribers` table); a valid
token sees everything. If the tool is ever shared with data separation, a
tenant boundary gets re-added then, not now.

**Multi-domain extensibility stays a goal:** Gmail, daycare, financial, and
product-manual sources plug in as new `sources` rows + one connector class
each, never a new code path.

## Current scope (build this first, nothing else)

**Daughter's school only (Faria Elementary), three sources — plus a fourth,
community one added 2026-10-01:**

1. **Events** — scrape Faria's public calendar at `faria.cusdk8.org`.
   No login required. Confirmed allowed by robots.txt.
2. **Menu** — via a Gmail-parsed auto-subscription email, **not scraping**.
   Both `cusdk8nutrition.com` and `schoolnutritionandfitness.com` (the
   district's nutrition vendors) explicitly disallow automated access in
   their robots.txt. Do not build a scraper against either domain — this
   was a deliberate decision, not an oversight. The menu source is a
   filtered view into a Gmail connector instead. **Also has a second,
   independent path now (2026-09-25): manual PDF upload via the Pilot
   Cockpit** (`sources/manual_menu/`, `(Domain.SCHOOL, Category.MENU)`) —
   built because the Gmail path is still stubbed on a real sample email.
   Both paths can coexist; whichever actually produces documents wins.
   `parse_menu_pdf` was built against two real vendor PDFs (lunch +
   breakfast), not guessed at — see its module docstring for the
   calendar-grid table shape and the one non-obvious finding: **the PDF
   states a year but never a month** anywhere in its content, so the
   operator must supply `year`/`month` at upload time.
3. **Events/reminders from email** — Gmail is not menu-only: school/
   ParentSquare emails that carry an event or a deadline (reminder) are also
   in scope, via the same Gmail connector, not a separate integration.
4. **Cupertino Parks & Rec events** (`sources/cupertino_events/`,
   `(Domain.CITY, Category.EVENT)`) — scrapes the city's **Event Calendar
   listing**, `cupertino.gov/Parks-Recreation/Events/Parks-and-Recreation-Event-Calendar`
   (paged via a `pageindex` query parameter). **Rebuilt 2026-10-25**: the
   original index-page + per-event-detail-page scrape only followed links
   one level under `/Events/` and so missed most dated events (4 stored
   vs. 8 listed). The listing has title, explicit day/month/year,
   description, venue and link per event, so no event page is ever fetched
   (~2 requests/run) and **no LLM is involved**: the connector sets
   `summary` from the listing's own description and the source takes
   `IngestionService._enrich`'s deterministic branch (`_SUMMARY_ONLY_DOMAINS`
   is now empty, kept for a future unstructured source). **Runs from the
   household Mac only**, writing to both local Postgres and Cloud SQL
   (`scripts/run_cupertino_events_ingestion.sh`, which starts its own
   `cloud-sql-proxy`): cupertino.gov's Akamai WAF 403s Google Cloud egress
   (see gotchas). The launchd job fires hourly + at login and only works
   when a target hasn't succeeded since the latest 07:00, so it catches up
   after sleep/shutdown/a failed run; the cloud `cupertino-events` job and
   trigger stay PAUSED. Still shares `PoliteFetcher` (per-URL robots.txt
   check) with `faria_events`, carries `documents.url` for the dashboard
   link, and keeps `reconcile_grace_hours: 48` (a later listing page can
   fail while page 1 succeeds). `Domain.CITY` is deliberately the broadest
   fit for any future municipal source. Design history:
   docs/cupertino-events-source-design.md. Dashboard: a "Show: All /
   School / Parks & City" filter (`st.pills`), a 🌳 icon, and a distinct
   green left-border, all keyed off `organization == "city_recreation"`.

Son's daycare, financial, and product-manual domains: **out of scope until
the daughter's MVP is working end-to-end.** The schema and architecture
already support them; the code does not need to yet.

## Architecture

Four layers:

```
Source agents  ->  Document store  ->  Reasoning agents  ->  Delivery
(scrapers,          (Postgres +         (router, retrieval,   (FastAPI,
 Gmail filters)      pgvector)           synthesis, critique)  Streamlit,
                                                                push alerts)
```

**Reasoning agent flow:** Router (classifies domain + category) ->
Retrieval (pulls documents filtered by domain/category + date window, live
rows only) -> Synthesis (drafts answer from retrieved context) ->
Critique/Grader (CRAG-style: checks the answer is actually supported by
retrieved context; loops back to Retrieval if not) -> Final answer.

**Why multi-agent, not one pipeline:** each source has fundamentally
different ingestion logic but must produce the same normalized document
shape; routing by domain first, then category, stays clean as more domains
are added. Not every "agent" needs to be an LLM call — source agents are
mostly plain code behind a shared interface; the LLM-based agents are
Router, Synthesis, Critique, and Evaluator.

## Data model

Full DDL lives in `db/schema.sql` — apply it, don't recreate it from
scratch. Key design decisions, so they aren't accidentally undone:

- **Access is token-gated, not data-scoped.** The `subscribers` table holds
  sha256 hashes of bearer tokens; an active token (`revoked_at IS NULL`)
  authorizes every read. No per-token filtering of `documents` — add that
  only if the tool is shared with data separation.
- **`sources.school_id` is `NOT NULL`.** Every source is anchored to a
  school (only Faria for now). No `family_id` / owner CHECK anymore.
- **`documents.student_ref` is free text, nullable, no FK.** A label like
  `'daughter'`, or `NULL` for whole-school docs. Students aren't modelled.
- **`documents.external_id` + `UNIQUE (source_id, external_id)`** is the
  dedup key. Every ingestion path must set this to something stable, or
  re-running ingestion inserts duplicates.
- **`domain`/`category` are denormalized onto `documents`** (not only
  reachable via a join through `sources`) because the Router/Retrieval
  agents filter on them on every query.
- **Soft delete, not hard.** `updated_at` / `last_seen_at` / `deleted_at`
  let ingestion reconcile upstream removals (a cancelled "No School" day
  must stop being an answer). Retrieval reads live rows only
  (`deleted_at IS NULL`).
- **Vector index: HNSW**, not IVFFlat. `embedding` is `vector(768)` for
  `nomic-embed-text`; change the column with the model.
- **LangGraph's own checkpointer/state tables are separate from this
  schema** — managed by `PostgresSaver`, not defined here.

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

## Environment setup

```bash
uv sync                       # Python 3.13 venv + deps (uv.lock)
cp .env.example .env          # DATABASE_URL, Ollama, SCRAPER_*
createdb acamar && psql acamar -f db/schema.sql
uv run python -m scripts.seed        # school + one access token + faria_events source
```

Access tokens are created/revoked with `scripts/token.py` and stored (hashed)
in `subscribers` — not in `.env`.

Dev tooling: `uv run ruff check`, `uv run mypy`, `uv run pytest`. Coding
conventions that keep those green: `docs/coding-standards.md`.

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
   for them anyway). Retrieval filters domain/category/date, live rows only.
   *Built (2026-09-17) — see docs/reasoning-pipeline-design.md. Router's
   first decision is actually a 4-way intent (document/weather/count/
   out_of_scope); categories only apply within the document intent.*
6. FastAPI backend: `GET /health`, `GET /dashboard`, `POST /query`,
   `GET /sources/{source_id}/status`. All except `/health` require a valid
   bearer token (`subscribers` lookup). *Built, including `/query`.*
7. Streamlit dashboard: today's menu/events/reminders at a glance, plus a
   free-text query box. Prompts for the access token. *Built; query box
   live (2026-09-17).*
8. CRAG critique/re-retrieval loop. *Built (2026-09-17) — see
   docs/reasoning-pipeline-design.md §2.4/§7; one known limitation logged
   under "Known gotchas" below.*
9. LLM-as-judge evaluator + a small golden dataset (menu/events Q&A).
   *Built (2026-09-17) — `scripts/run_eval.py` + `tests/eval/golden_qa.jsonl`.*
10. Notification agent + delivery (channel + on/off stored in
    `subscribers`/`sources.config` — no separate prefs table).
    **Deprioritized (2026-09-17):** push notifications aren't needed on the
    current web/API + Streamlit surface — revisit when a mobile app is
    built, since that's the natural place push delivery actually lands.
11. Portfolio page + system diagram (PRD-style format).
12. **Pilot Cockpit** — a separate admin Streamlit app/process (not the
    household-facing dashboard), gated by its own `cockpit_token` (not a
    `subscribers` token). *Built (2026-09-25):* manual menu-PDF upload
    (`sources/manual_menu/`, `POST /admin/menu-upload`, runs
    `IngestionService` inline — same pipeline as every other source);
    observability (`GET /admin/status` — sources health + an in-memory,
    non-durable recent-traffic ring buffer, `api/middleware.py`); a
    reversible pause (`POST /admin/pause`/`/resume`, 503s every non-admin
    route) and a hard-stop kill switch (`POST /admin/shutdown`, sends this
    process `SIGTERM` — irreversible from the cockpit, see gotchas below).
    `src/acamar/dashboard/cockpit_app.py`,
    `uv run streamlit run src/acamar/dashboard/cockpit_app.py --server.port 8502`.

## Known gotchas (don't rediscover these the hard way)

- The two nutrition vendor sites are scrape-blocked by robots.txt — see
  "Current scope" above. This is settled, not open for debate.
- **A connector with per-item fault isolation cannot safely use plain
  "not seen this run = reconcile it away" deletion logic** — real, live
  bug found by review (2026-10-02): `cupertino_events`' own per-event
  fault isolation (deliberate, see its connector docstring) means a single
  transient failure makes `fetch()` legitimately return a *partial* set,
  indistinguishable from the upstream actually removing that item. Fixed
  with `DocumentRepository.reconcile_deletions`'s `grace: timedelta`
  parameter (`IngestionService._reconcile_grace`, driven by
  `source.config["reconcile_grace_hours"]`) — a source whose connector
  can legitimately miss items on an otherwise-healthy run needs a nonzero
  grace (cupertino_events: 48h, ~2 missed once-daily runs); a source whose
  `fetch()` either sees everything or raises outright (Faria's single-page
  scrape) is correctly left at the default zero. Set this for any *future*
  per-item-isolated connector too, not just this one.
- **`urllib.robotparser` (Python stdlib) does not implement the `*`/`$`
  wildcard extension real robots.txt files actually use** — confirmed
  directly while building `cupertino_events` (2026-10-01): a rule like
  `Disallow: /Events/Monster-Mash/*` (cupertino.gov's own real, verbatim
  shape) gets its literal `*` percent-encoded to `%2A` when the stdlib
  parses it, so it silently never matches any real request path — a
  `can_fetch()` check that looks like it's enforcing robots.txt can
  actually be a complete no-op. `adapters/polite_http.py` (used by every
  scraper connector) uses `adapters/robots.py` — a small matcher this
  project wrote instead — not `urllib.robotparser`. Don't reintroduce the
  stdlib one for a future scraper without re-reading that module's
  docstring.
- **`cupertino.gov` sits behind an Akamai edge WAF that has, at least once,
  403'd a direct `curl`/`httpx` request regardless of User-Agent** —
  confirmed while building `cupertino_events`. **Update (2026-10-02): the
  same kind of direct request later succeeded fine** (real
  `scripts.run_ingestion` runs against the live site worked, more than
  once) — so this is confirmed *inconsistent/path-dependent*, not a
  reliable block. Don't assume either way; if a real run ever fails with a
  403 against this host, check this gotcha before assuming the connector
  itself regressed. **From GCP it's a consistent block (2026-10-24):** the
  `cupertino-events` Cloud Run Job got 403 on `robots.txt` *and* the index
  page, 2 of 2 attempts — Akamai evidently treats Google datacenter egress
  as a bot. Inconsistent from a home connection, blocked from GCP.
- **Re-running `scripts/gmail_auth.py` locally makes the cloud's
  `gmail-token` secret stale** — real, 2026-10-24: the first cloud
  `gmail-school` run failed with an old refresh token (past the OAuth app's
  7-day "Testing" cap) while the local, newer one refreshed fine. After any
  local re-auth, upload `secrets/gmail_token.json` as a new `gmail-token`
  version (`gcloud secrets versions add`). The job runs with
  `GMAIL_INTERACTIVE_AUTH=false`, so a dead token fails the run in seconds
  with that instruction, rather than hanging (backlog item 48).
- **A schema column every source's read path touches is not safely
  deferred as "a step for the user to apply later"** — real incident,
  2026-10-02: `documents.url` was added to `schema.sql` and the shared
  `_UPSERT`/`_COLUMNS` SQL in the same change, planning to leave the live
  `ALTER TABLE` as a manual step (this project has no migration tooling,
  item 17). Consequence: `GET /dashboard` itself started 500ing
  immediately, not just the next ingestion write — the whole household
  dashboard was down until the column was actually added. Also: adding the
  column doesn't retroactively populate it (or any other newly-wired field
  like `summary`) on already-ingested rows — those need a real
  re-ingestion run, not just the schema fix, before the new field shows up
  anywhere. Assess the real blast radius of a schema change (who reads
  this column, immediately) before deciding whether it's safe to defer.
- The API has **no process supervisor today** — it's run by hand
  (`uvicorn --reload`), not a launchd job like the ingestion cron. This is
  *why* the Pilot Cockpit's kill switch actually stops it (nothing races to
  relaunch it), but also why it's genuinely irreversible from the cockpit —
  don't add launchd supervision for the API without re-checking whether that
  breaks the kill switch's assumption.
- `pdfplumber` and `python-multipart` were added to `pyproject.toml` for the
  Pilot Cockpit (2026-09-25) via direct `pip install` into `.venv`, not
  `uv add` (`uv` wasn't on PATH in that shell) — `uv.lock` is stale until
  someone runs `uv sync` for real.
- Ollama's `num_ctx` limits how much retrieved context actually fits in a
  prompt — if retrieval starts returning several documents per query,
  check this isn't silently truncating context.
- `sources.last_run_at` / `last_status` exist specifically so a broken
  scraper (e.g. after a site redesign) is detectable via a query, not
  discovered by "the dashboard looks stale."
- **Fixed 2026-09-24** (was open here since 2026-09-17): CRAG's retry loop
  used to only fire when Critique calls the *draft answer* "ungrounded" —
  an honest "I don't know" from a too-narrow Router guess counts as
  grounded, so the widen-and-retry safety net never fired for the single
  most common real failure. `RetrievalGradeNode` now grades the *retrieved
  documents* before Synthesis ever runs (canonical CRAG's actual
  mechanism, not the post-hoc check this codebase originally built) —
  see `reasoning/nodes/retrieval_grade.py` and docs/sota.md §1.
- The final CRAG retry attempt widens `categories` to every value as a
  safety net against a bad Router guess — make sure `domains` gets reset
  to `None` there too (fixed 2026-09-24, was a real bug: a wrongly
  narrowed domain survived every retry attempt including the "final,
  maximally-widened" one, since only categories/dates were ever widened).
