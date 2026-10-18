"""Shared contract every :class:`SourceConnector` implementation must satisfy.

A new connector adds a subclass of :class:`SourceConnectorContract` that wires up
the three fixtures; the test methods are inherited.
"""

from __future__ import annotations

import abc
from datetime import UTC, datetime

import httpx
import pytest
import respx

from acamar.adapters.polite_http import PoliteFetcher
from acamar.domain.enums import Category, Domain
from acamar.domain.models import RawDocument
from acamar.ports.source_connector import ConnectorError, SourceConnector
from acamar.sources.cupertino_events.connector import CupertinoEventsConnector
from acamar.sources.faria_events.connector import FariaEventsConnector
from tests.fakes import FrozenClock

pytestmark = pytest.mark.contract


class SourceConnectorContract(abc.ABC):
    """Inherit and implement the three fixtures below."""

    @pytest.fixture
    @abc.abstractmethod
    def working_connector(self) -> SourceConnector:
        """A connector whose ``fetch()`` will return one or more documents."""

    @pytest.fixture
    @abc.abstractmethod
    def empty_connector(self) -> SourceConnector:
        """A connector whose ``fetch()`` will return ``[]`` without error."""

    @pytest.fixture
    @abc.abstractmethod
    def failing_connector(self) -> SourceConnector:
        """A connector whose upstream is broken."""

    def test_has_a_non_empty_name(self, working_connector: SourceConnector) -> None:
        assert isinstance(working_connector.name, str)
        assert working_connector.name

    async def test_fetch_returns_a_list_of_raw_documents(
        self, working_connector: SourceConnector
    ) -> None:
        result = await working_connector.fetch()
        assert isinstance(result, list)
        assert result and all(isinstance(d, RawDocument) for d in result)

    async def test_documents_are_well_formed(
        self, working_connector: SourceConnector
    ) -> None:
        for doc in await working_connector.fetch():
            assert doc.external_id and doc.title and doc.content
            assert isinstance(doc.domain, Domain)
            assert isinstance(doc.category, Category)

    async def test_external_ids_are_stable_across_runs(
        self, working_connector: SourceConnector
    ) -> None:
        first = {d.external_id for d in await working_connector.fetch()}
        second = {d.external_id for d in await working_connector.fetch()}
        assert first == second

    async def test_empty_upstream_yields_empty_list(
        self, empty_connector: SourceConnector
    ) -> None:
        assert await empty_connector.fetch() == []

    async def test_broken_upstream_raises_connector_error(
        self, failing_connector: SourceConnector
    ) -> None:
        with pytest.raises(ConnectorError):
            await failing_connector.fetch()


_CAL = "https://contract.test/about-us/school-calendar"
_ROBOTS = "https://contract.test/robots.txt"
_ALLOW_ALL = "User-agent: *\nDisallow:\n"
_EMPTY_PAGE = '<html><body><div class="fsCalendarWeekdays"></div></body></html>'


class TestFariaEventsConnectorContract(SourceConnectorContract):
    @staticmethod
    def _connector(
        mock: respx.MockRouter, calendar: httpx.Response, **kw: object
    ) -> SourceConnector:
        mock.get(_ROBOTS).mock(return_value=httpx.Response(200, text=_ALLOW_ALL))
        mock.get(_CAL).mock(return_value=calendar)
        return FariaEventsConnector(calendar_url=_CAL, **kw)  # type: ignore[arg-type]

    @pytest.fixture
    def working_connector(
        self, respx_mock: respx.MockRouter, faria_calendar_html: str
    ) -> SourceConnector:
        return self._connector(
            respx_mock, httpx.Response(200, html=faria_calendar_html)
        )

    @pytest.fixture
    def empty_connector(self, respx_mock: respx.MockRouter) -> SourceConnector:
        return self._connector(respx_mock, httpx.Response(200, html=_EMPTY_PAGE))

    @pytest.fixture
    def failing_connector(self, respx_mock: respx.MockRouter) -> SourceConnector:
        return self._connector(respx_mock, httpx.Response(500), max_attempts=1)


_INDEX = "https://contract.test/Parks-Recreation/Events"
_INDEX_ROBOTS = "https://contract.test/robots.txt"
_ONE_LINK = '<a href="/Parks-Recreation/Events/Some-Event">Some Event</a>'
_DATED_EVENT_PAGE = "<html><body><p><strong>Monday, January 5, 2026</strong></p></body></html>"
_UNDATED_EVENT_PAGE = "<html><body><p>Nothing scheduled yet.</p></body></html>"
_TODAY = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))


class TestCupertinoEventsConnectorContract(SourceConnectorContract):
    @staticmethod
    def _connector(
        mock: respx.MockRouter,
        index: httpx.Response,
        event: httpx.Response | None = None,
        *,
        max_attempts: int = 3,
    ) -> SourceConnector:
        mock.get(_INDEX_ROBOTS).mock(return_value=httpx.Response(200, text=_ALLOW_ALL))
        mock.get(_INDEX).mock(return_value=index)
        if event is not None:
            mock.get(f"{_INDEX}/Some-Event").mock(return_value=event)
        fetcher = PoliteFetcher(
            source="cupertino_events",
            user_agent="ACAMAR-contract-test/1",
            min_request_interval=0.0,
            max_attempts=max_attempts,
        )
        return CupertinoEventsConnector(index_url=_INDEX, fetcher=fetcher, clock=_TODAY)

    @pytest.fixture
    def working_connector(self, respx_mock: respx.MockRouter) -> SourceConnector:
        return self._connector(
            respx_mock,
            httpx.Response(200, html=_ONE_LINK),
            httpx.Response(200, html=_DATED_EVENT_PAGE),
        )

    @pytest.fixture
    def empty_connector(self, respx_mock: respx.MockRouter) -> SourceConnector:
        # A real index with one linked event, but that event has nothing
        # scheduled yet — the confirmed-real "will return in <year>" shape —
        # so fetch() legitimately returns [] without raising.
        return self._connector(
            respx_mock,
            httpx.Response(200, html=_ONE_LINK),
            httpx.Response(200, html=_UNDATED_EVENT_PAGE),
        )

    @pytest.fixture
    def failing_connector(self, respx_mock: respx.MockRouter) -> SourceConnector:
        # The index page itself failing is the one thing that raises for
        # this connector — a single event's own failure never does (see
        # CupertinoEventsConnector's own docstring). max_attempts=1 so this
        # fails on the first try, no real retry-backoff sleep needed.
        return self._connector(respx_mock, httpx.Response(500), max_attempts=1)
