"""Connector tests — HTTP is mocked; the real parser runs on the real captured pages.

PoliteFetcher's own robots/retry/throttle matrix is exhaustively covered in
test_polite_fetcher.py; what matters here is this connector's own paging:
follow the pager, keep page 1's events if a later page fails, raise only when
page 1 itself does.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from acamar.ports.source_connector import ConnectorError
from acamar.sources.cupertino_events.connector import CupertinoEventsConnector

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "cupertino_events"
CALENDAR_URL = "https://example.test/Parks-Recreation/Events/Parks-and-Recreation-Event-Calendar"
ROBOTS_URL = "https://example.test/robots.txt"
ROBOTS_ALLOW_ALL = "User-agent: *\nDisallow:\n"


def _page(n: int) -> str:
    return (_FIXTURES / f"calendar_page{n}.html").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr("acamar.adapters.polite_http.asyncio.sleep", _instant)


def _mock_site(
    *, page1: httpx.Response, page2: httpx.Response | None = None, robots: str = ROBOTS_ALLOW_ALL
) -> respx.Route:
    respx.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=robots))

    def _serve(request: httpx.Request) -> httpx.Response:
        if "pageindex=2" in str(request.url):
            return page2 if page2 is not None else httpx.Response(404)
        if "pageindex" in str(request.url):
            return httpx.Response(404)
        return page1

    return respx.get(url__startswith=CALENDAR_URL).mock(side_effect=_serve)


@pytest.fixture
def connector() -> CupertinoEventsConnector:
    return CupertinoEventsConnector(calendar_url=CALENDAR_URL)


def test_exposes_a_name() -> None:
    assert CupertinoEventsConnector(calendar_url=CALENDAR_URL).name == "cupertino_events"


@respx.mock
async def test_follows_the_pager_and_returns_every_event_on_both_pages(
    connector: CupertinoEventsConnector,
) -> None:
    route = _mock_site(
        page1=httpx.Response(200, html=_page(1)), page2=httpx.Response(200, html=_page(2))
    )

    documents = await connector.fetch()

    assert len(documents) == 8
    assert "signing-santa/signing-santa-event" in {d.external_id for d in documents}
    assert route.call_count == 2  # the listing only — no per-event page fetches


@respx.mock
async def test_a_later_page_failing_keeps_page_ones_events(
    connector: CupertinoEventsConnector,
) -> None:
    _mock_site(page1=httpx.Response(200, html=_page(1)), page2=httpx.Response(500))

    documents = await connector.fetch()

    assert len(documents) == 6  # page 1's; the 48h reconcile grace covers page 2's


@respx.mock
async def test_page_one_failing_raises(connector: CupertinoEventsConnector) -> None:
    _mock_site(page1=httpx.Response(500))

    with pytest.raises(ConnectorError):
        await connector.fetch()


@respx.mock
async def test_page_one_with_no_listing_raises(connector: CupertinoEventsConnector) -> None:
    _mock_site(page1=httpx.Response(200, html="<html><body>Redesigned</body></html>"))

    with pytest.raises(ConnectorError, match="parsing listing page 1"):
        await connector.fetch()


@respx.mock
async def test_robots_disallowing_the_calendar_raises_without_fetching_it(
    connector: CupertinoEventsConnector,
) -> None:
    route = _mock_site(
        page1=httpx.Response(200, html=_page(1)),
        robots="User-agent: *\nDisallow: /Parks-Recreation/\n",
    )

    with pytest.raises(ConnectorError, match="disallows"):
        await connector.fetch()

    assert not route.called


@respx.mock
async def test_an_event_listed_on_two_pages_is_returned_once(
    connector: CupertinoEventsConnector,
) -> None:
    # The listing can shift between requests; external_id must stay unique.
    _mock_site(
        page1=httpx.Response(200, html=_page(1)),
        page2=httpx.Response(200, html=_page(1).replace("Page 1 of 2", "Page 2 of 2")),
    )

    documents = await connector.fetch()

    assert len(documents) == 6


@respx.mock
async def test_the_pager_is_capped(connector: CupertinoEventsConnector) -> None:
    route = _mock_site(
        page1=httpx.Response(200, html=_page(1).replace("Page 1 of 2", "Page 1 of 500"))
    )

    await connector.fetch()

    assert route.call_count == 10  # page 1 + 9 more (all 404 → skipped), never 500
