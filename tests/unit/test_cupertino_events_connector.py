"""Connector tests — HTTP is mocked; the real parser runs on real-grounded fixtures.

PoliteFetcher's own robots/retry/throttle matrix is exhaustively covered in
test_polite_fetcher.py; what matters here is behavior specific to this
connector: a two-phase (index + N detail pages) fetch, per-event robots
checks, and per-event fault isolation — none of which faria_events needed.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import respx

from acamar.domain.models import RawDocument
from acamar.ports.source_connector import ConnectorError
from acamar.sources.cupertino_events import parser as cupertino_parser
from acamar.sources.cupertino_events.connector import CupertinoEventsConnector
from tests.fakes import FrozenClock

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "cupertino_events"
INDEX_URL = "https://example.test/Parks-Recreation/Events"
ROBOTS_URL = "https://example.test/robots.txt"
ROBOTS_ALLOW_ALL = "User-agent: *\nDisallow:\n"
TODAY = FrozenClock(datetime(2026, 10, 2, 12, 0, tzinfo=UTC))


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _small_index(*slugs: str) -> str:
    links = "\n".join(
        f'<a href="/Parks-Recreation/Events/{slug}">{slug.replace("-", " ").title()}</a>'
        for slug in slugs
    )
    return f"<html><body>{links}</body></html>"


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr("acamar.adapters.polite_http.asyncio.sleep", _instant)


def _mock_robots(body: str = ROBOTS_ALLOW_ALL, status: int = 200) -> None:
    respx.get(ROBOTS_URL).mock(return_value=httpx.Response(status, text=body))


@pytest.fixture
def connector() -> CupertinoEventsConnector:
    return CupertinoEventsConnector(index_url=INDEX_URL, clock=TODAY)


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_exposes_a_name() -> None:
    assert CupertinoEventsConnector(index_url=INDEX_URL).name == "cupertino_events"


@respx.mock
async def test_fetch_returns_one_document_per_dated_event(
    connector: CupertinoEventsConnector,
) -> None:
    _mock_robots()
    respx.get(INDEX_URL).mock(
        return_value=httpx.Response(200, html=_small_index("Tree-Lighting"))
    )
    respx.get(f"{INDEX_URL}/Tree-Lighting").mock(
        return_value=httpx.Response(200, html=_fixture("tree_lighting.html"))
    )

    documents = await connector.fetch()

    assert len(documents) == 1
    assert documents[0].domain.value == "city"
    assert documents[0].external_id == "tree-lighting"


@respx.mock
async def test_an_event_with_no_resolvable_date_is_excluded_not_errored(
    connector: CupertinoEventsConnector,
) -> None:
    _mock_robots()
    respx.get(INDEX_URL).mock(
        return_value=httpx.Response(
            200, html=_small_index("Tree-Lighting", "Movies-in-the-Park")
        )
    )
    respx.get(f"{INDEX_URL}/Tree-Lighting").mock(
        return_value=httpx.Response(200, html=_fixture("tree_lighting.html"))
    )
    respx.get(f"{INDEX_URL}/Movies-in-the-Park").mock(
        return_value=httpx.Response(200, html=_fixture("movies_in_the_park.html"))
    )

    documents = await connector.fetch()

    assert len(documents) == 1
    assert documents[0].external_id == "tree-lighting"


# ---------------------------------------------------------------------------
# per-URL robots.txt — the behavior faria_events never needed
# ---------------------------------------------------------------------------


@respx.mock
async def test_a_robots_disallowed_event_is_skipped_others_still_ingested(
    connector: CupertinoEventsConnector,
) -> None:
    # Real shape, confirmed from the live site's own robots.txt: the index
    # page and most events are allowed, but a specific event path is not —
    # disallowed via a trailing `*` wildcard, which is why this project
    # needed its own robots.txt matcher (adapters.robots) instead of
    # urllib.robotparser, which silently never matches `*` at all (confirmed
    # while building this — see adapters/robots.py's own module docstring).
    robots = "User-agent: *\nDisallow: /Parks-Recreation/Events/Monster-Mash*\n"
    _mock_robots(robots)
    respx.get(INDEX_URL).mock(
        return_value=httpx.Response(200, html=_small_index("Tree-Lighting", "Monster-Mash"))
    )
    respx.get(f"{INDEX_URL}/Tree-Lighting").mock(
        return_value=httpx.Response(200, html=_fixture("tree_lighting.html"))
    )
    monster_mash = respx.get(f"{INDEX_URL}/Monster-Mash").mock(
        return_value=httpx.Response(200, html=_fixture("tree_lighting.html"))
    )

    documents = await connector.fetch()

    assert len(documents) == 1
    assert documents[0].external_id == "tree-lighting"
    assert not monster_mash.called


@respx.mock
async def test_robots_disallowing_the_index_itself_raises(
    connector: CupertinoEventsConnector,
) -> None:
    _mock_robots("User-agent: *\nDisallow: /Parks-Recreation/\n")
    index = respx.get(INDEX_URL).mock(
        return_value=httpx.Response(200, html=_small_index("Tree-Lighting"))
    )

    with pytest.raises(ConnectorError, match="disallows"):
        await connector.fetch()

    assert not index.called


# ---------------------------------------------------------------------------
# per-event fault isolation — one bad event must not break the whole run
# ---------------------------------------------------------------------------


@respx.mock
async def test_one_events_fetch_failure_does_not_abort_the_run(
    connector: CupertinoEventsConnector,
) -> None:
    _mock_robots()
    respx.get(INDEX_URL).mock(
        return_value=httpx.Response(200, html=_small_index("Tree-Lighting", "Broken-Event"))
    )
    respx.get(f"{INDEX_URL}/Tree-Lighting").mock(
        return_value=httpx.Response(200, html=_fixture("tree_lighting.html"))
    )
    respx.get(f"{INDEX_URL}/Broken-Event").mock(return_value=httpx.Response(500))

    documents = await connector.fetch()

    assert len(documents) == 1
    assert documents[0].external_id == "tree-lighting"


@respx.mock
async def test_one_events_unparseable_page_does_not_abort_the_run(
    connector: CupertinoEventsConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    # BeautifulSoup is extremely tolerant of malformed markup in practice, so
    # rather than hope some byte sequence happens to make real parsing raise,
    # this directly simulates a parse-time exception for one specific event
    # to honestly test the connector's own per-event `except Exception`
    # isolation, independent of what bs4 does or doesn't choke on.
    _mock_robots()
    respx.get(INDEX_URL).mock(
        return_value=httpx.Response(200, html=_small_index("Tree-Lighting", "Weird-Event"))
    )
    respx.get(f"{INDEX_URL}/Tree-Lighting").mock(
        return_value=httpx.Response(200, html=_fixture("tree_lighting.html"))
    )
    respx.get(f"{INDEX_URL}/Weird-Event").mock(return_value=httpx.Response(200, html="<html/>"))

    real_parse = cupertino_parser.parse_event_detail_html

    def _flaky_parse(html: str, *, url: str, title: str, today: date) -> RawDocument | None:
        if "Weird-Event" in url:
            raise ValueError("simulated real-world parse failure")
        return real_parse(html, url=url, title=title, today=today)

    monkeypatch.setattr(
        "acamar.sources.cupertino_events.connector.parse_event_detail_html", _flaky_parse
    )

    documents = await connector.fetch()

    assert len(documents) == 1
    assert documents[0].external_id == "tree-lighting"


# ---------------------------------------------------------------------------
# index page failures
# ---------------------------------------------------------------------------


@respx.mock
async def test_index_page_with_no_links_raises(connector: CupertinoEventsConnector) -> None:
    _mock_robots()
    respx.get(INDEX_URL).mock(
        return_value=httpx.Response(200, html="<html><body>nothing here</body></html>")
    )

    with pytest.raises(ConnectorError, match="parsing the events index failed"):
        await connector.fetch()


@respx.mock
async def test_index_page_fetch_failure_raises(connector: CupertinoEventsConnector) -> None:
    _mock_robots()
    respx.get(INDEX_URL).mock(return_value=httpx.Response(503))

    with pytest.raises(ConnectorError):
        await connector.fetch()
