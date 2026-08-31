-- ACAMAR database schema (Postgres + pgvector)
-- Implements the reviewed ERD: families/users/schools/students own the tenant
-- boundary; sources own ingestion config and are scoped to EITHER a school
-- (shared) or a family (private); documents are the shared retrieval table
-- every source writes into.

CREATE EXTENSION IF NOT EXISTS pgvector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ---------------------------------------------------------------------------
-- Tenant boundary
-- ---------------------------------------------------------------------------

CREATE TABLE families (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE users (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    family_id   UUID NOT NULL REFERENCES families(id) ON DELETE CASCADE,
    email       TEXT NOT NULL UNIQUE,
    role        TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('owner', 'member')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE schools (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE students (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    family_id   UUID NOT NULL REFERENCES families(id) ON DELETE CASCADE,
    school_id   UUID REFERENCES schools(id) ON DELETE SET NULL,
    name        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_users_family_id ON users(family_id);
CREATE INDEX idx_students_family_id ON students(family_id);
CREATE INDEX idx_students_school_id ON students(school_id);

-- ---------------------------------------------------------------------------
-- Sources (per-domain connectors: scrapers, Gmail filters, etc.)
--
-- Ownership determines visibility: a source belongs to EITHER a school
-- (shared with every family enrolled there) OR a family (private). This is
-- the "sensitivity" of every document it produces -- derived, not a
-- separately-tracked flag that could drift out of sync.
-- ---------------------------------------------------------------------------

CREATE TABLE sources (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    school_id       UUID REFERENCES schools(id) ON DELETE CASCADE,
    family_id       UUID REFERENCES families(id) ON DELETE CASCADE,
    domain          TEXT NOT NULL,   -- e.g. 'school', 'daycare', 'gmail', 'financial', 'product_manual'
    category        TEXT NOT NULL,   -- e.g. 'event', 'menu', 'reminder', 'activity'
    config          JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_run_at     TIMESTAMPTZ,
    last_status     TEXT CHECK (last_status IN ('ok', 'error', 'never_run')) DEFAULT 'never_run',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- exactly one owner: a source is shared (school) or private (family), never both, never neither
    CONSTRAINT sources_single_owner CHECK (
        (school_id IS NOT NULL AND family_id IS NULL) OR
        (school_id IS NULL AND family_id IS NOT NULL)
    )
);

CREATE INDEX idx_sources_school_id ON sources(school_id);
CREATE INDEX idx_sources_family_id ON sources(family_id);
CREATE INDEX idx_sources_domain_category ON sources(domain, category);

-- ---------------------------------------------------------------------------
-- Documents (the shared retrieval table every source writes into)
-- ---------------------------------------------------------------------------

CREATE TABLE documents (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_id       UUID NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    student_id      UUID REFERENCES students(id) ON DELETE CASCADE,  -- nullable: not every doc is about a specific kid

    -- denormalized from sources for fast filtering on every retrieval query
    domain          TEXT NOT NULL,
    category        TEXT NOT NULL,

    title           TEXT NOT NULL,
    content         TEXT NOT NULL,
    external_id     TEXT NOT NULL,  -- dedup key: e.g. calendar event id, email message id

    event_date      DATE,           -- the date this document is ABOUT (menu date, event date) -- not when it was ingested
    published_at    TIMESTAMPTZ,    -- when the source itself published/sent this
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    embedding       vector(1536),   -- adjust dimension to match your chosen embedding model

    CONSTRAINT documents_dedup UNIQUE (source_id, external_id)
);

CREATE INDEX idx_documents_student_id ON documents(student_id);
CREATE INDEX idx_documents_domain_category ON documents(domain, category);
CREATE INDEX idx_documents_event_date ON documents(event_date);

-- Approximate nearest-neighbor index for semantic search.
-- ivfflat needs ANALYZE after the table has real data to pick good `lists`;
-- fine to defer this until there's a non-trivial number of rows.
CREATE INDEX idx_documents_embedding ON documents
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- ---------------------------------------------------------------------------
-- Notification preferences (per user, per domain/category push toggle)
-- ---------------------------------------------------------------------------

CREATE TABLE notification_preferences (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    domain          TEXT NOT NULL,
    category        TEXT NOT NULL,
    push_enabled    BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT notification_preferences_unique UNIQUE (user_id, domain, category)
);

CREATE INDEX idx_notification_preferences_user_id ON notification_preferences(user_id);

-- ---------------------------------------------------------------------------
-- Note: LangGraph's own checkpointer/state tables are intentionally NOT
-- defined here. They live in the same Postgres instance but are managed by
-- LangGraph's PostgresSaver, not by this application schema.
-- ---------------------------------------------------------------------------
