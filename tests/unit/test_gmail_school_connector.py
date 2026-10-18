"""GmailSchoolConnector — orchestration only; the mail client is faked."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from acamar.config import get_settings
from acamar.domain.enums import Category
from acamar.ports.mail_client import MailError, RawEmail
from acamar.ports.source_connector import ConnectorError
from acamar.sources.gmail_school.connector import GmailSchoolConnector
from tests.fakes import FakeMailClient


def _email(subject: str = "Sweat Challenge") -> RawEmail:
    return RawEmail(
        message_id="<abc123@mail.gmail.com>",
        subject=subject,
        sender="donotreply@parentsquare.com",
        received_at=datetime(2026, 9, 9, tzinfo=UTC),
        text_body="Join the fundraiser!",
    )


def test_no_query_anywhere_raises_connector_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GMAIL_SCHOOL_SEARCH_QUERY", "")
    get_settings.cache_clear()
    with pytest.raises(ConnectorError, match="no Gmail search query"):
        GmailSchoolConnector(mail=FakeMailClient())


def test_explicit_query_overrides_settings_default() -> None:
    connector = GmailSchoolConnector(query="label:school", mail=FakeMailClient())
    assert connector._query == "label:school"


def test_settings_query_used_when_none_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GMAIL_SCHOOL_SEARCH_QUERY", "label:configured")
    get_settings.cache_clear()
    connector = GmailSchoolConnector(mail=FakeMailClient())
    assert connector._query == "label:configured"


async def test_fetch_searches_then_parses_every_matched_email() -> None:
    mail = FakeMailClient(
        [
            _email("Community Board Office Hour - September 21, 2026 6:00-7:00 pm"),
            _email("Sweat Challenge"),
        ]
    )
    connector = GmailSchoolConnector(query="label:school", mail=mail, max_results=10)

    docs = await connector.fetch()

    assert len(docs) == 2
    assert mail.queries == [("label:school", 10)]
    categories = {doc.category for doc in docs}
    assert categories == {Category.EVENT, Category.REMINDER}


async def test_no_since_searches_the_bare_query() -> None:
    mail = FakeMailClient([])
    connector = GmailSchoolConnector(query="label:school", mail=mail)
    await connector.fetch()
    assert mail.queries == [("label:school", 25)]


async def test_since_appends_an_after_clause_with_a_one_day_buffer() -> None:
    mail = FakeMailClient([])
    connector = GmailSchoolConnector(
        query="label:school", mail=mail, since=datetime(2026, 9, 15, 6, 0, tzinfo=UTC)
    )
    await connector.fetch()
    assert mail.queries == [("label:school after:2026/09/14", 25)]


async def test_no_matches_returns_empty_list_not_an_error() -> None:
    connector = GmailSchoolConnector(query="label:school", mail=FakeMailClient([]))
    assert await connector.fetch() == []


async def test_mail_client_failure_becomes_connector_error() -> None:
    mail = FakeMailClient(error=MailError("oauth token expired"))
    connector = GmailSchoolConnector(query="label:school", mail=mail)

    with pytest.raises(ConnectorError, match="oauth token expired"):
        await connector.fetch()
