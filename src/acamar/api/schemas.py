"""Response models unique to the API layer."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel

from acamar.domain.enums import Category, Domain, SourceStatus


class SourceStatusResponse(BaseModel):
    """Ingestion health for one source (`GET /sources/{id}/status`)."""

    domain: Domain
    category: Category
    last_status: SourceStatus
    last_run_at: datetime | None


class SourceHealthResponse(BaseModel):
    """One `sources` row's health — feeds the Pilot Cockpit's sources panel.

    Like `SourceStatusResponse` but carries `id`, since this backs a *list*
    of sources rather than a single already-known one.
    """

    id: UUID
    domain: Domain
    category: Category
    last_status: SourceStatus
    last_run_at: datetime | None


class TrafficRecordResponse(BaseModel):
    """One captured HTTP request — see `api.middleware.RequestRecord`."""

    at: datetime
    method: str
    path: str
    status_code: int
    duration_ms: int


class QueryTopicResponse(BaseModel):
    """One captured `/query` call's routed topic — see `api.middleware.QueryRecord`."""

    at: datetime
    question: str
    intent: str
    categories: list[str]
    grounded: bool


class AdminStatusResponse(BaseModel):
    """`GET /admin/status` — sources health + the in-memory traffic/topic snapshot."""

    paused: bool
    sources: list[SourceHealthResponse]
    recent_requests: list[TrafficRecordResponse]
    recent_queries: list[QueryTopicResponse]


class PauseStateResponse(BaseModel):
    """`POST /admin/pause` / `POST /admin/resume` result."""

    paused: bool


class MenuCoverageResponse(BaseModel):
    """`GET /admin/menu-coverage` result.

    Which days of a (meal, year, month) are already ingested for a source —
    feeds the upload panel's "currently on file" chip and per-day coverage
    strip.
    """

    days_in_month: int
    covered_days: list[int]
    """1-indexed days of the month with a live (not soft-deleted) document —
    a dismissed-from-the-dashboard day still counts as "already uploaded",
    same semantics as `DocumentRepository.get_existing`.
    """


class QueryRequest(BaseModel):
    """Body of `POST /query`."""

    question: str


class QuerySource(BaseModel):
    """One document `POST /query`'s answer was grounded in — for citation."""

    id: UUID
    title: str
    event_date: date | None


class QueryResponse(BaseModel):
    """Response of `POST /query`."""

    answer: str
    sources: list[QuerySource]
    grounded: bool
    """``False`` only for a best-effort `DOCUMENT` answer after exhausting
    the CRAG retry cap — weather/count/decline answers are always ``True``.
    """
