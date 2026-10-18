"""GmailMenuConnector — orchestration only; the mail client and parser are faked."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from acamar.config import get_settings
from acamar.ports.mail_client import MailError, RawEmail
from acamar.ports.source_connector import ConnectorError
from acamar.sources.gmail_menu.connector import GmailMenuConnector
from tests.fakes import FakeMailClient


def _email(message_id: str = "<msg-1@district.example>") -> RawEmail:
    return RawEmail(
        message_id=message_id,
        subject="This week's menu",
        sender="noreply@cusdk8nutrition.com",
        received_at=datetime(2026, 9, 14, tzinfo=UTC),
        text_body="Monday: pizza",
    )


def test_no_query_anywhere_raises_connector_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # An env var (not delenv) so this is deterministic even if a real .env sets one.
    monkeypatch.setenv("GMAIL_MENU_SEARCH_QUERY", "")
    get_settings.cache_clear()
    with pytest.raises(ConnectorError, match="no Gmail search query"):
        GmailMenuConnector(mail=FakeMailClient())


def test_explicit_query_overrides_settings_default() -> None:
    mail = FakeMailClient([_email()])
    connector = GmailMenuConnector(query="from:district.example", mail=mail)
    assert connector._query == "from:district.example"


def test_settings_query_used_when_none_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GMAIL_MENU_SEARCH_QUERY", "from:configured@district.example")
    get_settings.cache_clear()
    connector = GmailMenuConnector(mail=FakeMailClient())
    assert connector._query == "from:configured@district.example"


async def test_fetch_searches_then_parses_each_matched_email() -> None:
    mail = FakeMailClient([_email()])
    connector = GmailMenuConnector(query="from:district.example", mail=mail, max_results=3)

    # The parser is a deliberate stub (docs/architecture.md §1.7) — the wiring
    # up to it is what's under test, so its NotImplementedError must surface
    # as a ConnectorError like any other parse failure.
    with pytest.raises(ConnectorError, match="no real sample menu email"):
        await connector.fetch()

    assert mail.queries == [("from:district.example", 3)]


async def test_mail_client_failure_becomes_connector_error() -> None:
    mail = FakeMailClient(error=MailError("oauth token expired"))
    connector = GmailMenuConnector(query="from:district.example", mail=mail)

    with pytest.raises(ConnectorError, match="oauth token expired"):
        await connector.fetch()
