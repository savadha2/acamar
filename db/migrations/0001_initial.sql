-- Migration 0001 — initial schema.
--
-- The baseline: db/schema.sql's full DDL, captured verbatim as of
-- 2026-10-19 (the first Cloud SQL deploy — see docs/gcp-aws-deployment-
-- proposal.md §8, "this deploy is the trigger [migration tooling] was
-- waiting for"). Applied once per database by scripts/migrate.py and
-- recorded in `schema_migrations`; never edited after the fact — a schema
-- change from here on is a NEW numbered file (0002_*.sql, ...), not an edit
-- to this one or to db/schema.sql.
--
-- db/schema.sql itself is kept as-is for the fast local bare-metal
-- `createdb && psql -f db/schema.sql` path (CLAUDE.md's build order item 1,
-- README's Setup section) — it is not derived from this file at runtime.
-- The two will drift once 0002+ exists; treat the migrations directory as
-- the source of truth for "what's actually been applied to a real
-- database" from this point on, and reconcile db/schema.sql by hand if it
-- is still worth keeping in sync for the quick-start path.

-- ACAMAR database schema (Postgres + pgvector)
--
-- Single-household tool. Access to the dashboard/API is gated by opaque bearer
-- tokens (the `subscribers` table); there is no per-user data scoping -- a valid
-- token sees everything. Sources are anchored to a school and are the only place
-- ingestion config lives; `documents` is the shared retrieval table every source
-- writes into.
--
-- (Earlier revisions modelled families/users/students for multi-tenancy. Dropped
-- 2026-09-08 -- see docs/architecture.md and CLAUDE.md. Re-add a tenant boundary
-- only if the tool is ever shared beyond one household with data separation.)

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ---------------------------------------------------------------------------
-- Schools -- the anchor a source's events/menus belong to
-- ---------------------------------------------------------------------------

CREATE TABLE schools (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Subscribers -- bearer tokens for the dashboard/API.
--
-- Store only a hash of the token, never the token itself. A row with
-- `revoked_at IS NULL` is active. No per-token scoping yet: any active token
-- authorizes every read.
-- ---------------------------------------------------------------------------

CREATE TABLE subscribers (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    token_hash  TEXT NOT NULL UNIQUE,   -- sha256 hex of the bearer token
    label       TEXT NOT NULL,          -- who this token is for ('me', 'partner', ...)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at  TIMESTAMPTZ             -- set to disable a token without losing history
);

CREATE INDEX idx_subscribers_active
    ON subscribers (token_hash) WHERE revoked_at IS NULL;

-- ---------------------------------------------------------------------------
-- Sources (per-domain connectors: scrapers, Gmail filters, etc.)
--
-- A new source type is added as a row here + one connector class, never a new
-- code path. `config` (JSONB) holds per-source settings: schedule, calendar URL,
-- Gmail filter, ETag cache, etc.
-- ---------------------------------------------------------------------------

CREATE TABLE sources (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    school_id       UUID NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
    domain          TEXT NOT NULL,   -- 'school', 'daycare', 'gmail', 'financial', 'product_manual', 'city'
    category        TEXT NOT NULL,   -- 'event', 'menu', 'reminder', 'activity'
    config          JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_run_at     TIMESTAMPTZ,
    last_status     TEXT NOT NULL DEFAULT 'never_run'
                    CHECK (last_status IN ('ok', 'error', 'never_run')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_sources_school_id ON sources(school_id);
CREATE INDEX idx_sources_domain_category ON sources(domain, category);

-- ---------------------------------------------------------------------------
-- Documents (the shared retrieval table every source writes into)
-- ---------------------------------------------------------------------------

CREATE TABLE documents (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_id       UUID NOT NULL REFERENCES sources(id) ON DELETE CASCADE,

    -- denormalized from sources: the Router/Retrieval agents filter on these
    -- on every query, so they must not require a join through `sources`.
    domain          TEXT NOT NULL,
    category        TEXT NOT NULL,

    -- free-text label for which kid this concerns ('daughter'); NULL = whole-school.
    -- Not a FK: students aren't modelled.
    student_ref     TEXT,

    title           TEXT NOT NULL,
    content         TEXT NOT NULL,
    headline        TEXT,           -- short assistant-style card title (not the raw subject)
    summary         TEXT,           -- LLM one/two-sentence gloss of `content`, for dashboard cards
    is_relevant     BOOLEAN NOT NULL DEFAULT true,  -- dashboard filter only; retrieval ignores this
    organization    TEXT,           -- closed taxonomy, see domain.enums.Organization
    external_id     TEXT NOT NULL,  -- dedup key: calendar occurrence id, email Message-ID
    url             TEXT,           -- source page, if the connector has one (e.g. cupertino_events'
                                     -- event page) — NULL for sources with nothing to link to
                                     -- (Faria's calendar has no stable per-event URL, Gmail has no
                                     -- public one, manual_menu has no web page at all)

    event_date      DATE,           -- the date this document is ABOUT (not when ingested)
    published_at    TIMESTAMPTZ,    -- when the source itself published/sent it

    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),  -- last ingest that changed this row's content
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),  -- last ingest run that still produced it
    deleted_at      TIMESTAMPTZ,    -- soft-delete: upstream removed it (e.g. a cancelled event)
    dismissed_at    TIMESTAMPTZ,    -- user marked "done" on the dashboard — deliberately separate
                                    -- from deleted_at: a future re-ingest must not undo this the way
                                    -- reconcile_deletions legitimately clears deleted_at

    embedding       vector(768),    -- nomic-embed-text (768-dim); change with the model

    CONSTRAINT documents_dedup UNIQUE (source_id, external_id)
);

CREATE INDEX idx_documents_domain_category ON documents(domain, category);
CREATE INDEX idx_documents_event_date ON documents(event_date);
-- the hot path: live (not soft-deleted) docs in a date window
CREATE INDEX idx_documents_live ON documents(event_date) WHERE deleted_at IS NULL;

-- Approximate nearest-neighbour index for semantic search.
-- HNSW over IVFFlat: no "needs real data first to cluster well" quirk.
CREATE INDEX idx_documents_embedding ON documents
    USING hnsw (embedding vector_cosine_ops);

-- ---------------------------------------------------------------------------
-- Note: LangGraph's own checkpointer/state tables are intentionally NOT
-- defined here. They live in the same Postgres instance but are managed by
-- LangGraph's PostgresSaver, not by this application schema.
-- ---------------------------------------------------------------------------
