"""Typed application settings, loaded from the environment / ``.env``."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Field names map to case-insensitive env vars."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql://localhost:5432/acamar"

    disable_auth: bool = False
    """DEV ONLY — when true, every route (including `/query`,
    `/dashboard`) skips token checking and runs as a synthetic subscriber.
    Never set in an environment reachable off the local machine — the
    Streamlit dev server binds `*:8501` (all interfaces), not just
    localhost, so this is a real exposure on a shared network, not just a
    theoretical one. Meant to be flipped back to `false` (or unset) when
    done; not a permanent setting."""

    api_base_url: str = "http://localhost:8000"
    """Where the Streamlit dashboard reaches the API."""

    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3"
    ollama_num_ctx: int = 8192
    """Context-window budget; the retrieval-context builder truncates to this."""

    ollama_embed_model: str = "nomic-embed-text"
    embedding_dim: int = 768
    """Must equal the ``documents.embedding`` column dimension in db/schema.sql."""

    ollama_summary_model: str = "llama3"
    """Deliberately a separate setting from ``ollama_model`` (even though they
    currently match) — enrichment and future RAG synthesis are different jobs
    and may want different models again later.

    Tried ``gemma3:1b`` first (2026-09-15): ~5-6x faster per call, but its
    ``organization`` classification was wrong even with the sender's org
    named in the first line of the email, and it showed signs of fabricating
    unstated details in longer summaries. ``llama3`` fixed both in testing.
    Change-detection (skip re-enrichment on unchanged content) means the slow
    model only costs real time on a backfill / new mail, not every run.
    """

    # --- Reasoning pipeline (docs/reasoning-pipeline-design.md §2.7) ---
    # One setting per node, even where the value matches another's today —
    # same "separate setting even when equal" reasoning as ollama_summary_model.
    ollama_router_model: str = "llama3"
    """Tried `gemma3:1b` first (2026-09-17, scripts/_spot_check_router.py
    against all 31 worked questions in docs/reasoning-pipeline-design.md
    §2.8/§2.9): only 20/31 (65%) correct, with real misclassifications
    (homework-help routed to `document`, "send an umbrella" routed to
    `out_of_scope`). `llama3` scored 27/31 (87%) on the same set — same
    "test the hypothesis, escalate if wrong" methodology as
    `ollama_summary_model`'s choice.
    """
    ollama_retrieval_grade_model: str = "llama3"
    """Grades retrieved documents before Synthesis runs (2026-09-24, closes
    a real gap — see `QueryState.retrieval_relevant`). Same "at least as
    capable as Synthesis" reasoning as `ollama_critique_model`: a weak
    grader here silently defeats the whole point of catching a bad
    retrieval before wasting a synthesis call on it.
    """
    ollama_critique_model: str = "llama3"
    """The CRAG safety net — at least as capable as ollama_model (Synthesis),
    never weaker. A weak grader silently defeats the whole retry loop.
    """
    ollama_judge_model: str = "llama3"
    """Offline eval only (script, not the live graph) — see Decision E.
    Starts equal to ollama_model; revisit once the golden set outgrows what
    can be spot-checked by eye.
    """

    # --- Faria events connector ---
    faria_calendar_url: str = "https://faria.cusdk8.org/about-us/school-calendar"
    scraper_user_agent: str = "ACAMAR/0.1 (personal daily-care dashboard)"
    """Override in ``.env`` with a real contact URL/email before running unattended."""
    scraper_min_request_interval_seconds: float = 5.0
    """Floor for the crawl delay; the connector uses ``max(this, robots Crawl-delay)``."""
    scraper_max_attempts: int = 3
    """Total tries for a transient fetch failure (connection error / 502-504)."""

    # --- Cupertino events connector (shares scraper_* settings above) ---
    cupertino_events_index_url: str = "https://www.cupertino.gov/Parks-Recreation/Events"
    """Confirmed from a real environment this project's dev machine actually
    gets blocked on: the site's own Akamai edge WAF 403s a direct
    curl/httpx request regardless of User-Agent. Whoever runs this connector
    needs network egress that WAF doesn't block — see
    ``sources.cupertino_events.parser``'s own module docstring."""

    # --- Gmail connector (shared transport; each source has its own query) ---
    gmail_credentials_path: str = "secrets/gmail_credentials.json"
    """OAuth client secret downloaded from Google Cloud Console. Gitignored."""
    gmail_token_path: str = "secrets/gmail_token.json"  # noqa: S105 - a file path, not a secret
    """Cached OAuth refresh token, written after the first local-browser consent."""

    gmail_menu_search_query: str | None = None
    """The real Gmail search query for the menu subscription (e.g.
    ``"from:noreply@schoolnutritionandfitness.com"``).

    No default: unset must fail loudly rather than silently matching nothing.
    """

    gmail_school_search_query: str | None = None
    """The real Gmail search query for school events/reminders (e.g. a label
    filter on the ParentSquare/teacher mail).

    No default, same reasoning as ``gmail_menu_search_query``.
    """

    household_email_addresses: str = ""
    """Comma-separated addresses that count as "sent by us" for the
    gmail_school privacy guardrail (skip self-sent mail and its replies —
    see ``sources.gmail_school.parser``). Empty means nothing matches, not
    "match everything" — unset is a safe no-op, not a wide-open filter.
    """

    # --- Pilot Cockpit (manual menu upload + observability + kill switch) ---
    cockpit_token: str | None = None
    """Bearer token for the Pilot Cockpit's `/admin/*` routes (menu upload,
    traffic snapshot, pause/resume, shutdown) — deliberately separate from
    the `subscribers` table: upload + shutdown are a materially bigger blast
    radius than the read-only dashboard's "any active token sees everything"
    model, so they get their own credential rather than a wider grant on the
    existing one. No default: unset must fail loudly (every `/admin/*` route
    401s), never a silent no-auth surface.
    """

    manual_menu_source_id: str | None = None
    """The `(school, menu)` manual-upload source's `sources.id`, once seeded
    — pre-fills the Pilot Cockpit's upload form so the operator doesn't have
    to paste a UUID by hand every time. The upload endpoint itself still
    takes an explicit `source_id`; this only supplies the cockpit's default.
    """

    cockpit_upload_dir: str = "data/uploads/menu"
    """Where an uploaded menu PDF is staged, under a `breakfast/`/`lunch/`
    subdirectory, for `ManualMenuConnector.fetch()` to read. Filenames are
    `{year}-{month}.pdf` — a re-upload for the same month overwrites, it
    never accumulates duplicates.
    """

    cockpit_max_recent_requests: int = 500
    """Ring-buffer size for the in-memory traffic snapshot (`GET
    /admin/status`) — a live "is it alive, what's it doing" pilot view, not a
    durable log; resets on every API restart by design.
    """

    # --- Weather (docs/weather-design-proposal.md) ---
    weather_latitude: float = 37.3206195
    weather_longitude: float = -122.0401073
    """William Faria Elementary School, Cupertino, CA 95014 — the one
    location this single-household app needs (geocoded 2026-09-16 from
    10155 Barbara Ln). Bypasses ``sources``/``documents`` entirely; see the
    proposal doc for why.
    """
    weather_cache_ttl_seconds: int = 1800
    """How long a fetched forecast is reused before calling Open-Meteo again."""


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings singleton.

    A function (not a module-level instance) so importing :mod:`acamar.config`
    has no side effect and tests can override the environment then
    ``get_settings.cache_clear()``.
    """
    return Settings()
