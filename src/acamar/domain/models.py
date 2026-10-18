"""Domain models produced and consumed by the ingestion pipeline."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from acamar.domain.enums import Category, Domain, Organization, SourceStatus

Vector = list[float]
"""An embedding — length must match ``documents.embedding`` (currently 768)."""


class RawDocument(BaseModel):
    """A single normalised item emitted by a source connector.

    Fields map 1:1 to the ingestable columns of the ``documents`` table. The
    connector does not know its ``source_id`` (the pipeline supplies it) and does
    not resolve ``student_id`` — it may only hint at one via ``student_ref``.

    The ``documents`` table has no JSON column, so any structured detail a
    connector wants to preserve (times, upstream ids) must be rendered into
    ``content`` as human-readable text — which is also what gets embedded.
    """

    # Frozen: once a connector emits a document, the pipeline only reads it.
    model_config = ConfigDict(frozen=True)

    external_id: str = Field(min_length=1)
    """Stable per-source identity used for dedup (``UNIQUE (source_id, external_id)``)."""

    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    domain: Domain
    category: Category

    event_date: date | None = None
    """The date the document is *about* (event/menu date), not when it was ingested."""

    published_at: datetime | None = None
    """When the source itself published or sent this, if known."""

    student_ref: str | None = None
    """Optional connector hint that this document concerns a specific student."""

    url: str | None = None
    """The source's own page for this document, if it has one (e.g.
    cupertino_events' event page) — lets the dashboard link out to it. NULL
    for sources with nothing stable to link to (Faria's calendar has no
    durable per-event URL, Gmail has no public one, manual_menu has no web
    page at all)."""

    headline: str | None = None
    """Short, assistant-style card title — never the raw ``title`` verbatim
    for Gmail content. Never set by a connector; the ingestion pipeline's
    enrichment step fills this in. See ``ports.enricher``.
    """

    summary: str | None = None
    """One/two-sentence LLM gloss of ``content``, for dashboard cards.

    Never set by a connector — the ingestion pipeline fills this in after
    ``fetch()`` returns, so connectors stay unaware of the enricher.
    """

    is_relevant: bool = True
    """Whether this belongs on the glance-view dashboard.

    Filters the dashboard only — retrieval/search must ignore this and stay
    over the full corpus (a "not dashboard-worthy" email can still answer a
    real question).
    """

    organization: Organization | None = None
    """Closed-taxonomy grouping for the dashboard (``domain.enums.Organization``)."""

    @property
    def embedding_text(self) -> str:
        """Text handed to the embedder.

        Title + content, without repeating the title when ``content`` already
        leads with it.
        """
        if self.title in self.content:
            return self.content
        return f"{self.title}\n\n{self.content}"


class Source(BaseModel):
    """A row of ``sources`` — one ingestion connector's config and run state."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    school_id: UUID
    domain: Domain
    category: Category
    config: dict[str, Any] = Field(default_factory=dict)
    last_run_at: datetime | None = None
    last_status: SourceStatus = SourceStatus.NEVER_RUN


class Document(BaseModel):
    """A persisted ``documents`` row, as returned by retrieval."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    source_id: UUID
    domain: Domain
    category: Category
    student_ref: str | None
    title: str
    content: str
    headline: str | None
    summary: str | None
    is_relevant: bool
    organization: Organization | None
    external_id: str
    event_date: date | None
    published_at: datetime | None
    url: str | None = None
    """See `RawDocument.url`'s own docstring. Optional with a default so
    every existing `Document(...)` construction (tests included) stays
    valid without naming it."""


class Subscriber(BaseModel):
    """A row of ``subscribers`` — one active/revoked access token."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    label: str
    created_at: datetime


class IngestionReport(BaseModel):
    """Outcome of one ``IngestionService.run`` — also the structured log line."""

    source_id: UUID
    source: str
    status: SourceStatus
    fetched: int = 0
    upserted: int = 0
    deleted: int = 0
    duration_ms: int = 0
    error: str | None = None
