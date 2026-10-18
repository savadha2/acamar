"""Connector that fetches the Faria calendar page and parses its events."""

from __future__ import annotations

import httpx

from acamar.adapters.polite_http import PoliteFetcher
from acamar.config import get_settings
from acamar.domain.models import RawDocument
from acamar.ports.source_connector import ConnectorError
from acamar.sources.faria_events.parser import parse_calendar_html


class FariaEventsConnector:
    """Scrapes the current month of events from Faria's public calendar.

    Only the server-rendered page is fetched (the JSON calendar endpoints are
    disallowed by ``robots.txt``, which is re-checked at runtime via
    :class:`~acamar.adapters.polite_http.PoliteFetcher`, which also throttles
    requests and retries transient failures with exponential backoff).

    Every argument defaults to :func:`acamar.config.get_settings`; pass explicit
    values in tests or from the connector factory.
    """

    name = "faria_events"

    def __init__(
        self,
        *,
        calendar_url: str | None = None,
        client: httpx.AsyncClient | None = None,  # inject in tests / to share a pool
        user_agent: str | None = None,
        min_request_interval: float | None = None,
        max_attempts: int | None = None,
        fetcher: PoliteFetcher | None = None,  # inject a fully-built fetcher in tests
    ) -> None:
        settings = get_settings()
        self._calendar_url = calendar_url or settings.faria_calendar_url
        self._fetcher = fetcher or PoliteFetcher(
            source=self.name,
            user_agent=user_agent or settings.scraper_user_agent,
            min_request_interval=(
                settings.scraper_min_request_interval_seconds
                if min_request_interval is None
                else min_request_interval
            ),
            max_attempts=(
                settings.scraper_max_attempts if max_attempts is None else max_attempts
            ),
            client=client,
        )

    async def fetch(self) -> list[RawDocument]:
        """Fetch and parse the calendar page.

        Raises:
            ConnectorError: the page could not be retrieved or parsed, or
                ``robots.txt`` disallows it.
        """
        response = await self._fetcher.get(self._calendar_url)
        try:
            return parse_calendar_html(response.text)
        except Exception as exc:
            # Deliberately broad: any parser failure (site redesign, truncated
            # HTML, missing calendar container) must surface as ConnectorError so
            # the pipeline records last_status='error', not a silent stale board.
            raise ConnectorError(
                f"{self.name}: parsing calendar HTML failed: {exc}", source=self.name
            ) from exc
