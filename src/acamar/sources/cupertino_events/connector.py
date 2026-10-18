"""Connector that scrapes Cupertino's public Parks & Rec events pages."""

from __future__ import annotations

import logging

from acamar.adapters.clock import SystemClock
from acamar.adapters.polite_http import PoliteFetcher
from acamar.config import get_settings
from acamar.domain.models import RawDocument
from acamar.ports.clock import Clock
from acamar.ports.source_connector import ConnectorError
from acamar.sources.cupertino_events.parser import (
    IndexStructureError,
    parse_event_detail_html,
    parse_index_html,
)

logger = logging.getLogger("acamar.ingestion")


class CupertinoEventsConnector:
    """Scrapes the city's events index, then every linked event's own page.

    Unlike `FariaEventsConnector` (one URL, one robots check), this fetches
    an index page plus roughly two dozen event detail pages, each checked
    against `robots.txt` individually — confirmed necessary, not defensive
    paranoia: several of the real site's individual event paths are
    robots-disallowed even though the index page and most events aren't (see
    docs/cupertino-events-source-design.md §2). `PoliteFetcher` caches
    robots.txt per origin, so this costs one extra `can_fetch()` call per
    event, not one extra HTTP fetch.

    Per-event fault isolation is deliberate, not an oversight: a
    robots-disallowed event, a broken fetch, or an unparseable detail page
    for ONE event must never abort the other two dozen — the exact lesson
    already learned once in this codebase from a different connector
    (`manual_menu`: "one bad staged menu PDF permanently breaks ingestion for
    every meal/month"), applied here from the start instead of fixed after
    the fact. Only a failure fetching/parsing the index page itself raises
    `ConnectorError` — that really does mean zero candidate events, a
    genuine "something's broken" signal `faria_events`-style.

    No concurrency: events are fetched sequentially, each gated by
    `PoliteFetcher`'s own throttle — politeness over speed, same posture as
    every other scraper here. For ~27 candidate events at the default 5s
    floor, expect a multi-minute run, not a fast one.
    """

    name = "cupertino_events"

    def __init__(
        self,
        *,
        index_url: str | None = None,
        fetcher: PoliteFetcher | None = None,
        clock: Clock | None = None,
    ) -> None:
        settings = get_settings()
        self._index_url = index_url or settings.cupertino_events_index_url
        self._fetcher = fetcher or PoliteFetcher(
            source=self.name,
            user_agent=settings.scraper_user_agent,
            min_request_interval=settings.scraper_min_request_interval_seconds,
            max_attempts=settings.scraper_max_attempts,
        )
        self._clock = clock or SystemClock()

    async def fetch(self) -> list[RawDocument]:
        """Fetch the index page, then every linked event's own page.

        Raises:
            ConnectorError: the index page itself could not be fetched or
                parsed, or `robots.txt` disallows it. A single event's own
                failure never raises — see this class's docstring.
        """
        try:
            index_response = await self._fetcher.get(self._index_url)
        except ConnectorError:
            raise
        except Exception as exc:  # pragma: no cover - PoliteFetcher wraps its own
            raise ConnectorError(
                f"{self.name}: fetching the events index failed: {exc}", source=self.name
            ) from exc

        try:
            candidates = parse_index_html(index_response.text, base_url=self._index_url)
        except IndexStructureError as exc:
            raise ConnectorError(
                f"{self.name}: parsing the events index failed: {exc}", source=self.name
            ) from exc

        today = self._clock.today()
        documents: list[RawDocument] = []
        for title, url in candidates:
            try:
                detail_response = await self._fetcher.get(url)
            except ConnectorError as exc:
                # robots-disallowed OR a fetch failure for this one event —
                # either way, skip it, not the whole run.
                logger.info("cupertino_events: skipping %s: %s", url, exc)
                continue
            try:
                doc = parse_event_detail_html(
                    detail_response.text, url=url, title=title, today=today
                )
            except Exception:
                logger.warning(
                    "cupertino_events: skipping %s: failed to parse", url, exc_info=True
                )
                continue
            if doc is not None:
                documents.append(doc)
        return documents
