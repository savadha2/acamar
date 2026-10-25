"""connector_for — registry lookup."""

from __future__ import annotations

from uuid import uuid4

import pytest

from acamar.config import get_settings
from acamar.domain.enums import Category, Domain
from acamar.domain.models import Source
from acamar.ingestion.connector_factory import connector_for
from acamar.ports.source_connector import ConnectorError, SourceConnector
from acamar.sources.cupertino_events.connector import CupertinoEventsConnector
from acamar.sources.faria_events.connector import FariaEventsConnector
from acamar.sources.gmail_menu.connector import GmailMenuConnector
from acamar.sources.gmail_school.connector import GmailSchoolConnector


def _source(domain: Domain, category: Category, **config: str) -> Source:
    return Source(
        id=uuid4(),
        school_id=uuid4(),
        domain=domain,
        category=category,
        config=dict(config),
    )


def test_builds_the_faria_events_connector() -> None:
    connector = connector_for(
        _source(Domain.SCHOOL, Category.EVENT, calendar_url="https://x.test/cal")
    )
    assert isinstance(connector, FariaEventsConnector)
    assert isinstance(connector, SourceConnector)


def test_builds_the_cupertino_events_connector() -> None:
    connector = connector_for(
        _source(Domain.CITY, Category.EVENT, calendar_url="https://x.test/Events/Calendar")
    )
    assert isinstance(connector, CupertinoEventsConnector)
    assert isinstance(connector, SourceConnector)


def test_unregistered_pair_raises_connector_error() -> None:
    with pytest.raises(ConnectorError, match="no connector"):
        connector_for(_source(Domain.FINANCIAL, Category.REMINDER))


def test_builds_the_gmail_menu_connector() -> None:
    connector = connector_for(
        _source(Domain.GMAIL, Category.MENU, query="from:noreply@cusdk8nutrition.com")
    )
    assert isinstance(connector, GmailMenuConnector)
    assert isinstance(connector, SourceConnector)


def test_gmail_menu_without_a_query_raises_connector_error() -> None:
    with pytest.raises(ConnectorError, match="no Gmail search query"):
        connector_for(_source(Domain.GMAIL, Category.MENU))


def test_builds_the_gmail_school_connector() -> None:
    connector = connector_for(_source(Domain.GMAIL, Category.EVENT, query="label:school"))
    assert isinstance(connector, GmailSchoolConnector)
    assert isinstance(connector, SourceConnector)


def test_gmail_school_without_a_query_raises_connector_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An env var (not delenv) so this is deterministic even with a real .env set.
    monkeypatch.setenv("GMAIL_SCHOOL_SEARCH_QUERY", "")
    get_settings.cache_clear()
    with pytest.raises(ConnectorError, match="no Gmail search query"):
        connector_for(_source(Domain.GMAIL, Category.EVENT))
