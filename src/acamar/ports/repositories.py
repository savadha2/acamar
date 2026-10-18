"""Persistence ports. Adapters live in ``acamar.adapters.db``."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Protocol
from uuid import UUID

from acamar.domain.enums import Category, Domain, SourceStatus
from acamar.domain.models import Document, RawDocument, Source, Subscriber, Vector


class SourceNotFoundError(LookupError):
    """No ``sources`` row with the given id."""


class SubscriberRepository(Protocol):
    """Looks up access tokens for the auth layer."""

    async def active_by_token_hash(self, token_hash: str) -> Subscriber | None:
        """The non-revoked subscriber with this token hash, or ``None``."""
        ...


class SourceRepository(Protocol):
    """Reads a source's config; records the outcome of each ingestion run."""

    async def get(self, source_id: UUID) -> Source:
        """Raises :class:`SourceNotFoundError` if the id is unknown."""
        ...

    async def mark_run(
        self, source_id: UUID, status: SourceStatus, *, at: datetime
    ) -> None:
        """Record the outcome and time of an ingestion run."""
        ...

    async def list_all(self) -> list[Source]:
        """Every source row — feeds the Pilot Cockpit's sources-health panel."""
        ...


class DocumentRepository(Protocol):
    """Upserts documents, reconciles upstream deletions, serves similarity search."""

    async def upsert(
        self, source_id: UUID, documents: list[tuple[RawDocument, Vector]]
    ) -> int:
        """Insert or update on ``(source_id, external_id)``.

        Always bumps ``last_seen_at`` and clears ``deleted_at``; bumps
        ``updated_at`` only when title / content / event_date changed. Returns
        the number of rows written.
        """
        ...

    async def reconcile_deletions(
        self,
        source_id: UUID,
        *,
        run_started_at: datetime,
        on_or_after: date,
        grace: timedelta = timedelta(),
    ) -> int:
        """Soft-delete this source's future-dated stale rows.

        Marks ``deleted_at`` on rows with ``event_date >= on_or_after`` whose
        ``last_seen_at`` predates ``run_started_at - grace``. Returns the count.

        ``grace`` (2026-10-02, real bug found by review): a connector whose
        own per-event fault isolation means `fetch()` can legitimately
        return a partial set on a run with a transient failure (confirmed
        real for ``cupertino_events`` — a timeout/5xx/WAF hiccup on any one
        event) must not have that transient miss read identically to "the
        upstream actually removed this." Default is a zero timedelta —
        today's exact behavior, unchanged for every source that doesn't
        pass one.
        """
        ...

    async def search(
        self,
        embedding: Vector,
        *,
        categories: list[Category],
        domains: list[Domain] | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int = 8,
    ) -> list[Document]:
        """Nearest live, non-dismissed documents across ``categories`` (and date window).

        ``domains`` narrows to specific sources when given; ``None`` (the
        default) searches every domain. Needed because a single category
        can span multiple domains today (``EVENT`` comes from both the
        Faria scrape and Gmail) — a domain-locked search would silently
        miss real matches. See docs/reasoning-pipeline-design.md Decision A.

        Excludes ``dismissed_at IS NOT NULL`` rows, same as ``count()`` and
        ``list_documents()`` — a document the user marked "done" on the
        dashboard shouldn't resurface as a citation in an Ask-box answer.
        """
        ...

    async def count(
        self,
        *,
        categories: list[Category],
        domains: list[Domain] | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> int:
        """Exact count of live, non-dismissed documents matching the filter.

        Deliberately not derived from ``search()`` — retrieval's top-K is a
        similarity rank, not exhaustive, so synthesizing a count from it
        risks a confidently wrong number. This is a plain SQL ``count(*)``
        instead. See docs/reasoning-pipeline-design.md Decision G.
        """
        ...

    async def list_documents(
        self,
        *,
        category: Category,
        date_from: date,
        date_to: date,
        undated_since: date | None = None,
        limit: int = 50,
    ) -> list[Document]:
        """Live documents in a category and date window, chronological.

        Dated documents use ``event_date``. Dateless documents (``event_date
        IS NULL`` — most of the best-effort Gmail parse) have no deadline to
        filter on, so they're kept out of the window entirely: included when
        ``undated_since`` is ``None`` (the default — old behavior, always
        shown), otherwise only when ``published_at >= undated_since`` (or
        unset). This is what keeps a months-old, never-dated reminder from
        cluttering the dashboard forever — display-layer only, ``search()``
        is never filtered this way; the full corpus stays retrievable.
        """
        ...

    async def get_existing(
        self, source_id: UUID, external_ids: list[str]
    ) -> dict[str, Document]:
        """Live rows for this source matching any of ``external_ids``, keyed by it.

        Used for ingestion-time change-detection: skip re-enrichment when a
        fetched document's ``content`` matches what's already stored.
        ``get_existing(source_id, [])`` returns ``{}``.
        """
        ...

    async def next_event_date(self, *, category: Category, after: date) -> date | None:
        """Earliest live, non-dismissed `event_date` strictly after `after`.

        Feeds a "nothing today — here's the next one, dated" fallback (the
        dashboard's menu card); generic across categories, not menu-specific.
        Doesn't filter `is_relevant` — same "repository stays dumb, the
        service composes/filters for display" split as `list_documents`.
        """
        ...

    async def dismiss(self, document_id: UUID) -> bool:
        """Mark a document "done" — excluded from ``list_documents``/``search``/``count``.

        Deliberately a separate marker from ``deleted_at``: a future
        ``upsert``/reconcile must never undo a user's dismissal the way it
        legitimately clears/sets ``deleted_at``. Returns ``False`` if the id
        doesn't exist (not an error — the item may have already scrolled out
        of the window some other way).
        """
        ...
