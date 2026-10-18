"""ASGI middleware: the Pilot Cockpit's in-memory traffic snapshot + pause gate.

`CockpitState` is process-wide, in-memory, deliberately not Postgres or
Redis — the traffic snapshot is a "what's happening right now" pilot view,
not a durable log, and `paused` only needs to survive as long as the process
does; a restart clearing both is correct behaviour here, not a bug.
"""

from __future__ import annotations

import time
from collections import deque
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

_PAUSE_EXEMPT_PREFIXES = ("/health", "/admin")
"""Routes that must keep working while paused — the cockpit itself (so
`/admin/resume` is always reachable) and the health check (so an external
monitor can still see the process is up, just paused)."""


class RequestRecord(BaseModel):
    """One captured HTTP request, for the Pilot Cockpit's traffic snapshot."""

    model_config = ConfigDict(frozen=True)

    at: datetime
    method: str
    path: str
    status_code: int
    duration_ms: int


class QueryRecord(BaseModel):
    """One `/query` call's routed topic, for the "most-queried topics" chart.

    A `RequestRecord` for the same call already captures method/path/status/
    duration (it's HTTP-generic); this captures what the *Router* decided —
    data the generic ASGI middleware can never see, since it lives inside
    the route handler's own business logic. Pushed by
    `api.routers.query.query`, not the middleware.
    """

    model_config = ConfigDict(frozen=True)

    at: datetime
    question: str
    """Truncated (see `_QUESTION_PREVIEW_LEN`) — this is an admin-visible
    operational log, not a place to grow an unbounded transcript."""
    intent: str
    categories: list[str]
    """Only meaningful when `intent == "document"`."""
    grounded: bool


class CockpitState:
    """Process-wide mutable state the `/admin/*` routes read and flip."""

    def __init__(self, *, max_recent: int) -> None:
        self.paused = False
        self._recent: deque[RequestRecord] = deque(maxlen=max_recent)
        self._recent_queries: deque[QueryRecord] = deque(maxlen=max_recent)

    def record(self, entry: RequestRecord) -> None:
        """Append one captured request, evicting the oldest past `max_recent`."""
        self._recent.append(entry)

    def recent(self) -> list[RequestRecord]:
        """Captured requests, most recent first."""
        return list(reversed(self._recent))

    def record_query(self, entry: QueryRecord) -> None:
        """Append one routed `/query` call, evicting the oldest past `max_recent`."""
        self._recent_queries.append(entry)

    def recent_queries(self) -> list[QueryRecord]:
        """Captured query topics, most recent first."""
        return list(reversed(self._recent_queries))


class TrafficMiddleware(BaseHTTPMiddleware):
    """Records every request into `CockpitState` and enforces its pause gate."""

    def __init__(self, app: ASGIApp, *, state: CockpitState) -> None:
        super().__init__(app)
        self._state = state

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Serve (or 503 while paused) and record the request either way."""
        started = time.monotonic()
        if self._state.paused and not request.url.path.startswith(_PAUSE_EXEMPT_PREFIXES):
            response: Response = JSONResponse(
                {"detail": "the API is paused from the Pilot Cockpit"},
                status_code=503,
            )
        else:
            response = await call_next(request)
        self._state.record(
            RequestRecord(
                at=datetime.now(UTC),
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
        )
        return response
