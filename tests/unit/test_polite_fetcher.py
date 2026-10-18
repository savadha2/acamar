"""PoliteFetcher — robots.txt + throttle + retry, independent of any connector.

Extracted 2026-10-01 from test_faria_events_connector.py alongside the
PoliteFetcher extraction itself (see docs/cupertino-events-source-design.md
§5.1) — these cases used to be exercised only through FariaEventsConnector;
now they're exercised directly against the shared implementation, so a
second connector doesn't need to re-duplicate this whole matrix.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx

from acamar.adapters.polite_http import PoliteFetcher
from acamar.ports.source_connector import ConnectorError

URL = "https://example.test/some/page"
ROBOTS_URL = "https://example.test/robots.txt"
OTHER_URL = "https://other.test/some/page"
OTHER_ROBOTS_URL = "https://other.test/robots.txt"

ROBOTS_ALLOW_ALL = "User-agent: *\nDisallow:\n"
ROBOTS_DISALLOW_PAGE = "User-agent: *\nDisallow: /some/\n"
ROBOTS_WITH_DELAY = "User-agent: *\nCrawl-delay: 20\nDisallow:\n"


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make retry/throttle backoff instant."""

    async def _instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr("acamar.adapters.polite_http.asyncio.sleep", _instant)


@pytest.fixture
def fetcher() -> PoliteFetcher:
    return PoliteFetcher(
        source="test", user_agent="ACAMAR-test/1", min_request_interval=0.0, max_attempts=3
    )


def _mock_robots(
    url: str = ROBOTS_URL, body: str = ROBOTS_ALLOW_ALL, status: int = 200
) -> respx.Route:
    return respx.get(url).mock(return_value=httpx.Response(status, text=body))


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------


@respx.mock
async def test_robots_disallow_raises_before_fetching(fetcher: PoliteFetcher) -> None:
    _mock_robots(body=ROBOTS_DISALLOW_PAGE)
    page = respx.get(URL).mock(return_value=httpx.Response(200, text="ok"))

    with pytest.raises(ConnectorError, match="disallows"):
        await fetcher.get(URL)

    assert not page.called


@respx.mock
async def test_missing_robots_txt_is_treated_as_allowed(fetcher: PoliteFetcher) -> None:
    _mock_robots(status=404)
    respx.get(URL).mock(return_value=httpx.Response(200, text="ok"))

    response = await fetcher.get(URL)

    assert response.text == "ok"


@respx.mock
async def test_unreachable_robots_txt_fails_open(fetcher: PoliteFetcher) -> None:
    respx.get(ROBOTS_URL).mock(side_effect=httpx.ConnectError("dns"))
    respx.get(URL).mock(return_value=httpx.Response(200, text="ok"))

    response = await fetcher.get(URL)

    assert response.text == "ok"


@respx.mock
async def test_robots_crawl_delay_raises_the_throttle_floor(fetcher: PoliteFetcher) -> None:
    _mock_robots(body=ROBOTS_WITH_DELAY)
    respx.get(URL).mock(return_value=httpx.Response(200, text="ok"))

    await fetcher.get(URL)

    assert fetcher._min_request_interval == 20.0


@respx.mock
async def test_robots_txt_is_fetched_once_across_multiple_requests(
    fetcher: PoliteFetcher,
) -> None:
    robots = _mock_robots()
    respx.get(URL).mock(return_value=httpx.Response(200, text="one"))
    respx.get("https://example.test/other/page").mock(
        return_value=httpx.Response(200, text="two")
    )

    await fetcher.get(URL)
    await fetcher.get("https://example.test/other/page")

    assert robots.call_count == 1


@respx.mock
async def test_two_origins_do_not_crosstalk(fetcher: PoliteFetcher) -> None:
    # example.test disallows /some/; other.test allows everything. A single
    # PoliteFetcher serving both origins in one run (what cupertino_events
    # would do if the index and an event ever lived on different hosts) must
    # evaluate each against its own robots.txt, not the first one it saw.
    _mock_robots(ROBOTS_URL, body=ROBOTS_DISALLOW_PAGE)
    _mock_robots(OTHER_ROBOTS_URL, body=ROBOTS_ALLOW_ALL)
    respx.get(OTHER_URL).mock(return_value=httpx.Response(200, text="ok"))

    with pytest.raises(ConnectorError, match="disallows"):
        await fetcher.get(URL)

    response = await fetcher.get(OTHER_URL)
    assert response.text == "ok"


# ---------------------------------------------------------------------------
# failures & retries
# ---------------------------------------------------------------------------


@respx.mock
async def test_exhausted_retries_become_connector_error(fetcher: PoliteFetcher) -> None:
    _mock_robots()
    respx.get(URL).mock(return_value=httpx.Response(503))

    with pytest.raises(ConnectorError, match="after 3 attempts"):
        await fetcher.get(URL)


@respx.mock
async def test_retries_transient_5xx_then_succeeds(fetcher: PoliteFetcher) -> None:
    _mock_robots()
    respx.get(URL).mock(
        side_effect=[httpx.Response(503), httpx.Response(503), httpx.Response(200, text="ok")]
    )

    response = await fetcher.get(URL)

    assert response.text == "ok"


@respx.mock
async def test_redirect_is_not_followed_and_becomes_connector_error(
    fetcher: PoliteFetcher,
) -> None:
    # Real bug found by review (2026-10-02): auto-following a redirect would
    # hand a connector content from an origin robots.txt was never checked
    # against (confirmed real: some cupertino_events links 302 to entirely
    # different third-party sites). A redirect must be treated as this URL
    # being unfetchable, not silently followed.
    _mock_robots()
    other_origin = respx.get("https://elsewhere.test/landed").mock(
        return_value=httpx.Response(200, text="should never be reached")
    )
    respx.get(URL).mock(
        return_value=httpx.Response(302, headers={"location": "https://elsewhere.test/landed"})
    )

    with pytest.raises(ConnectorError, match="redirects to"):
        await fetcher.get(URL)

    assert not other_origin.called


@respx.mock
async def test_repeated_network_errors_become_connector_error(fetcher: PoliteFetcher) -> None:
    _mock_robots()
    respx.get(URL).mock(side_effect=httpx.ConnectError("boom"))

    with pytest.raises(ConnectorError):
        await fetcher.get(URL)


@respx.mock
async def test_read_timeout_becomes_connector_error(fetcher: PoliteFetcher) -> None:
    _mock_robots()
    respx.get(URL).mock(side_effect=httpx.ReadTimeout("slow"))

    with pytest.raises(ConnectorError):
        await fetcher.get(URL)


@respx.mock
async def test_404_becomes_connector_error(fetcher: PoliteFetcher) -> None:
    _mock_robots()
    respx.get(URL).mock(return_value=httpx.Response(404))

    with pytest.raises(ConnectorError, match="fetching"):
        await fetcher.get(URL)


# ---------------------------------------------------------------------------
# client lifecycle & throttle
# ---------------------------------------------------------------------------


@pytest.fixture
def injected_client() -> Iterator[httpx.AsyncClient]:
    client = httpx.AsyncClient()
    yield client


@respx.mock
async def test_injected_client_is_left_open(injected_client: httpx.AsyncClient) -> None:
    _mock_robots()
    respx.get(URL).mock(return_value=httpx.Response(200, text="ok"))

    fetcher = PoliteFetcher(
        source="test",
        user_agent="ACAMAR-test/1",
        min_request_interval=0.0,
        max_attempts=3,
        client=injected_client,
    )
    await fetcher.get(URL)

    assert not injected_client.is_closed
    await injected_client.aclose()


@respx.mock
async def test_throttle_delays_the_second_request(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_robots()
    respx.get(URL).mock(return_value=httpx.Response(200, text="ok"))
    slept: list[float] = []

    async def _record(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("acamar.adapters.polite_http.asyncio.sleep", _record)

    async with httpx.AsyncClient() as client:
        fetcher = PoliteFetcher(
            source="test",
            user_agent="ACAMAR-test/1",
            min_request_interval=5.0,
            max_attempts=3,
            client=client,
        )
        await fetcher.get(URL)
        await fetcher.get(URL)

    assert any(s > 0 for s in slept)  # the 2nd fetch had to wait out the interval
