"""Connector tests — HTTP is mocked; the real parser runs on real captured HTML.

The exhaustive robots.txt/retry/throttle matrix used to live entirely here; as
of 2026-10-01 that logic was extracted into `adapters.polite_http.PoliteFetcher`
(shared with `cupertino_events`) and its own exhaustive tests moved to
`test_polite_fetcher.py`. What's left here confirms this connector is actually
*wired up* to that shared policy (robots still blocks a fetch, retries still
happen, a failure still surfaces as `ConnectorError`) plus everything genuinely
Faria-specific (HTML parsing, the 7-document happy path, site-redesign
detection).
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx

from acamar.ports.source_connector import ConnectorError
from acamar.sources.faria_events.connector import FariaEventsConnector

CALENDAR_URL = "https://example.test/about-us/school-calendar"
ROBOTS_URL = "https://example.test/robots.txt"

ROBOTS_ALLOW_ALL = "User-agent: *\nDisallow:\n"
ROBOTS_DISALLOW_CALENDAR = "User-agent: *\nDisallow: /about-us/\n"


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make retry/throttle backoff instant (PoliteFetcher's, not this module's)."""

    async def _instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr("acamar.adapters.polite_http.asyncio.sleep", _instant)


@pytest.fixture
def connector() -> FariaEventsConnector:
    return FariaEventsConnector(calendar_url=CALENDAR_URL, max_attempts=3)


def _mock_robots(body: str = ROBOTS_ALLOW_ALL, status: int = 200) -> None:
    respx.get(ROBOTS_URL).mock(return_value=httpx.Response(status, text=body))


def _mock_calendar(html: str, status: int = 200) -> respx.Route:
    return respx.get(CALENDAR_URL).mock(return_value=httpx.Response(status, html=html))


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_exposes_a_name() -> None:
    assert FariaEventsConnector(calendar_url=CALENDAR_URL).name == "faria_events"


@respx.mock
async def test_fetch_returns_parsed_events(
    connector: FariaEventsConnector, faria_calendar_html: str
) -> None:
    _mock_robots()
    _mock_calendar(faria_calendar_html)

    documents = await connector.fetch()

    assert len(documents) == 7
    assert {d.domain.value for d in documents} == {"school"}


@respx.mock
async def test_sends_the_configured_user_agent(faria_calendar_html: str) -> None:
    _mock_robots()
    route = _mock_calendar(faria_calendar_html)

    await FariaEventsConnector(
        calendar_url=CALENDAR_URL, user_agent="ACAMAR-test/9"
    ).fetch()

    assert route.calls.last.request.headers["User-Agent"] == "ACAMAR-test/9"


# ---------------------------------------------------------------------------
# PoliteFetcher wiring — confirms the policy actually applies through this
# connector; the exhaustive edge cases for the policy itself live in
# test_polite_fetcher.py.
# ---------------------------------------------------------------------------


@respx.mock
async def test_robots_disallow_raises_before_fetching_calendar(
    connector: FariaEventsConnector, faria_calendar_html: str
) -> None:
    _mock_robots(ROBOTS_DISALLOW_CALENDAR)
    calendar = _mock_calendar(faria_calendar_html)

    with pytest.raises(ConnectorError, match="disallows"):
        await connector.fetch()

    assert not calendar.called


@respx.mock
async def test_retries_transient_5xx_then_succeeds(
    connector: FariaEventsConnector, faria_calendar_html: str
) -> None:
    _mock_robots()
    respx.get(CALENDAR_URL).mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(200, html=faria_calendar_html),
        ]
    )

    assert len(await connector.fetch()) == 7


@respx.mock
async def test_exhausted_retries_become_connector_error(
    connector: FariaEventsConnector,
) -> None:
    _mock_robots()
    respx.get(CALENDAR_URL).mock(return_value=httpx.Response(503))

    with pytest.raises(ConnectorError, match="after 3 attempts"):
        await connector.fetch()


# ---------------------------------------------------------------------------
# Faria-specific: HTML parsing
# ---------------------------------------------------------------------------


@respx.mock
async def test_site_redesign_becomes_connector_error(
    connector: FariaEventsConnector,
) -> None:
    _mock_robots()
    _mock_calendar("<html><body>You are being redirected</body></html>")

    with pytest.raises(ConnectorError, match="parsing calendar HTML failed"):
        await connector.fetch()


# ---------------------------------------------------------------------------
# client lifecycle
# ---------------------------------------------------------------------------


@pytest.fixture
def injected_client() -> Iterator[httpx.AsyncClient]:
    client = httpx.AsyncClient()
    yield client


@respx.mock
async def test_injected_client_is_left_open(
    injected_client: httpx.AsyncClient, faria_calendar_html: str
) -> None:
    _mock_robots()
    _mock_calendar(faria_calendar_html)

    await FariaEventsConnector(
        calendar_url=CALENDAR_URL, client=injected_client
    ).fetch()

    assert not injected_client.is_closed
    await injected_client.aclose()
