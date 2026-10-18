"""Shared robots.txt-aware, throttled, retrying HTTP GET for scraper connectors.

Extracted 2026-10-01 from ``FariaEventsConnector``, which had this logic
embedded directly since it was the only scraper connector. ``cupertino_events``
needs the exact same policy (check robots.txt, throttle between requests,
retry transient failures) applied **per URL** across many paths on one host
(one index page + roughly two dozen event pages, all on ``cupertino.gov``) —
not once against a single fixed URL, which is all ``faria_events`` ever
needed and is why this was never factored out before. One ``PoliteFetcher``
instance is meant to be reused across every request a connector makes in one
``fetch()`` call, so its robots.txt cache and throttle clock actually do
something.

Uses ``adapters.robots`` (our own minimal parser), not ``urllib.robotparser``
— the stdlib one doesn't implement the ``*``/``$`` wildcard extension
cupertino.gov's real robots.txt actually relies on for every one of its
per-event disallow rules; see that module's own docstring for the confirmed
(not assumed) failure this would otherwise have shipped with silently.
"""

from __future__ import annotations

import asyncio
import time

import httpx

from acamar.adapters.robots import RobotsRules, parse_robots_txt
from acamar.ports.source_connector import ConnectorError

_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=5.0)
_RETRYABLE_STATUS = frozenset({502, 503, 504})
_BACKOFF_BASE_SECONDS = 1.0


class PoliteFetcher:
    """Fetches URLs honoring robots.txt, a minimum request interval, and retries.

    Robots.txt is fetched and parsed once per **origin** the first time a URL
    on it is requested, then cached for every later ``get()`` call against
    that same origin — so a connector can call ``get()`` many times across
    many paths without re-fetching robots.txt each time. ``source`` is used
    only to label a raised :class:`ConnectorError`'s ``source`` attribute and
    message text; pass the owning connector's own ``name``.
    """

    def __init__(
        self,
        *,
        source: str,
        user_agent: str,
        min_request_interval: float,
        max_attempts: int,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._source = source
        self._user_agent = user_agent
        self._min_request_interval = min_request_interval
        self._max_attempts = max_attempts
        self._client = client
        self._last_request_monotonic: float | None = None
        self._robots_cache: dict[str, RobotsRules | None] = {}

    async def get(self, url: str) -> httpx.Response:
        """GET ``url``, honoring robots.txt and this fetcher's throttle/retry policy.

        Raises:
            ConnectorError: robots.txt disallows ``url``, or the request
                failed after ``max_attempts`` tries.
        """
        # Own a short-lived client when none was injected; don't close a shared one.
        if self._client is not None:
            return await self._get_with(self._client, url)
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            return await self._get_with(client, url)

    async def _get_with(self, client: httpx.AsyncClient, url: str) -> httpx.Response:
        await self._assert_robots_allow(client, url)
        return await self._get_with_retry(client, url)

    async def _assert_robots_allow(self, client: httpx.AsyncClient, url: str) -> None:
        """Honour ``robots.txt`` at runtime, not just by prior manual review."""
        robots_url = str(httpx.URL(url).join("/robots.txt"))
        if robots_url not in self._robots_cache:
            self._robots_cache[robots_url] = await self._fetch_robots(client, robots_url)
        rules = self._robots_cache[robots_url]
        if rules is None:
            return  # unreachable or missing robots.txt -> fail open, standard crawler stance
        if not rules.can_fetch(httpx.URL(url).path):
            raise ConnectorError(
                f"{self._source}: robots.txt disallows {url}", source=self._source
            )
        if rules.crawl_delay is not None:
            self._min_request_interval = max(self._min_request_interval, rules.crawl_delay)

    async def _fetch_robots(
        self, client: httpx.AsyncClient, robots_url: str
    ) -> RobotsRules | None:
        try:
            response = await client.get(
                robots_url, headers=self._headers(), follow_redirects=True
            )
        except httpx.HTTPError:
            return None  # robots unreachable -> fail open
        if response.status_code >= 400:
            return None  # no robots.txt -> nothing disallowed
        return parse_robots_txt(response.text, user_agent=self._user_agent)

    async def _get_with_retry(self, client: httpx.AsyncClient, url: str) -> httpx.Response:
        for attempt in range(1, self._max_attempts + 1):
            await self._throttle()
            try:
                # follow_redirects=False (2026-10-02, real bug found by
                # review): `_assert_robots_allow` only ever validates `url`
                # itself — auto-following a redirect would silently hand
                # whatever's at the *landing* URL (confirmed real: some
                # cupertino.gov event links 302 to entirely different
                # third-party origins) straight into a connector's content,
                # never robots-checked against that real origin, and for
                # cupertino_events that content flows on into an LLM
                # prompt. A redirect is treated as this URL being
                # unfetchable for our purposes, same as any other failure —
                # see the explicit ConnectorError below.
                response = await client.get(
                    url, headers=self._headers(), follow_redirects=False
                )
                self._last_request_monotonic = time.monotonic()
                if response.is_redirect:
                    raise ConnectorError(
                        f"{self._source}: {url} redirects to "
                        f"{response.headers.get('location', '?')} — not following "
                        "a redirect to an origin we haven't robots-checked",
                        source=self._source,
                    )
                if response.status_code not in _RETRYABLE_STATUS:
                    response.raise_for_status()
                    return response
                reason: object = f"HTTP {response.status_code}"
            except httpx.TransportError as exc:
                self._last_request_monotonic = time.monotonic()
                reason = exc
            except httpx.HTTPStatusError as exc:
                raise ConnectorError(
                    f"{self._source}: fetching {url} failed: {exc}", source=self._source
                ) from exc

            if attempt == self._max_attempts:
                raise ConnectorError(
                    f"{self._source}: fetching {url} failed after {attempt} attempts ({reason})",
                    source=self._source,
                )
            await asyncio.sleep(_BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))

        raise AssertionError("unreachable")  # pragma: no cover

    async def _throttle(self) -> None:
        # monotonic() so a system clock change can't let a burst skip the delay.
        if self._last_request_monotonic is None:
            return
        elapsed = time.monotonic() - self._last_request_monotonic
        remaining = self._min_request_interval - elapsed
        if remaining > 0:
            await asyncio.sleep(remaining)

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": self._user_agent}
