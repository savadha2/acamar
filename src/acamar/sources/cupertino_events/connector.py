"""Connector that scrapes Cupertino's Parks & Rec Event Calendar listing."""

from __future__ import annotations

import logging

from acamar.adapters.polite_http import PoliteFetcher
from acamar.config import get_settings
from acamar.domain.models import RawDocument
from acamar.ports.source_connector import ConnectorError
from acamar.sources.cupertino_events.parser import (
    CalendarPage,
    CalendarStructureError,
    page_url,
    parse_calendar_page_html,
)

logger = logging.getLogger("acamar.ingestion")

_MAX_PAGES = 10
"""Safety cap on the pager's own "of N" — the real listing had 2 pages."""


class CupertinoEventsConnector:
    """Scrapes the city's Event Calendar listing, page by page.

    Rebuilt 2026-10-25 (see `parser`'s module docstring): every field the
    dashboard needs is on the listing itself, so this never fetches an
    individual event's page — ~2 requests per run instead of ~25.

    Page 1 failing (fetch, robots, or no listing at all) raises
    `ConnectorError`: that really is zero events, a "something's broken"
    signal. A later page failing is logged and skipped, keeping what was
    already parsed — a partial fetch the source's `reconcile_grace_hours`
    (48h) already tolerates, so a transient page-2 failure can't soft-delete
    that page's events. Every URL still goes through `PoliteFetcher`'s
    per-URL `robots.txt` check and throttle.
    """

    name = "cupertino_events"

    def __init__(
        self,
        *,
        calendar_url: str | None = None,
        fetcher: PoliteFetcher | None = None,
    ) -> None:
        settings = get_settings()
        self._calendar_url = calendar_url or settings.cupertino_events_calendar_url
        self._fetcher = fetcher or PoliteFetcher(
            source=self.name,
            user_agent=settings.scraper_user_agent,
            min_request_interval=settings.scraper_min_request_interval_seconds,
            max_attempts=settings.scraper_max_attempts,
        )

    async def fetch(self) -> list[RawDocument]:
        """Fetch every listing page and return one document per listed event.

        Raises:
            ConnectorError: page 1 could not be fetched or parsed, or
                `robots.txt` disallows it.
        """
        first = await self._fetch_page(1)
        documents = list(first.documents)
        for page in range(2, min(first.total_pages, _MAX_PAGES) + 1):
            try:
                documents.extend((await self._fetch_page(page)).documents)
            except ConnectorError as exc:
                logger.warning("cupertino_events: skipping listing page %d: %s", page, exc)

        # The same event can appear on two pages if the listing shifts
        # between requests — keep the first, so external_id stays unique.
        unique: dict[str, RawDocument] = {}
        for doc in documents:
            unique.setdefault(doc.external_id, doc)
        return list(unique.values())

    async def _fetch_page(self, page: int) -> CalendarPage:
        url = page_url(self._calendar_url, page)
        response = await self._fetcher.get(url)  # raises ConnectorError itself
        try:
            return parse_calendar_page_html(response.text, base_url=self._calendar_url)
        except CalendarStructureError as exc:
            raise ConnectorError(
                f"{self.name}: parsing listing page {page} failed: {exc}", source=self.name
            ) from exc
