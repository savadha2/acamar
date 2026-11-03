"""`require_subscriber` — the household password, subscriber tokens, and `disable_auth`."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException

from acamar.api import auth as auth_module
from acamar.api.auth import require_admin, require_subscriber
from acamar.config import get_settings
from acamar.domain.models import Subscriber
from acamar.security import hash_token
from tests.fakes import FakeSubscriberRepository


async def test_valid_token_resolves_the_subscriber() -> None:
    subscriber = Subscriber(id=uuid4(), label="me", created_at=datetime(2026, 1, 1, tzinfo=UTC))
    repo = FakeSubscriberRepository({hash_token("good-token"): subscriber})

    result = await require_subscriber(repo, authorization="Bearer good-token")

    assert result == subscriber


async def test_missing_header_is_401() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await require_subscriber(FakeSubscriberRepository(), authorization=None)
    assert exc_info.value.status_code == 401


async def test_unknown_token_is_401() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await require_subscriber(FakeSubscriberRepository(), authorization="Bearer nope")
    assert exc_info.value.status_code == 401


async def test_disable_auth_is_off_by_default() -> None:
    assert get_settings().disable_auth is False


async def test_disable_auth_bypasses_the_token_check_entirely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISABLE_AUTH", "true")
    get_settings.cache_clear()  # conftest's _isolate_settings clears it again after

    result = await require_subscriber(FakeSubscriberRepository(), authorization=None)

    assert result.label == "dev (auth disabled)"


# ---------------------------------------------------------------------------
# the shared household password (backlog item 50, 2026-10-25)
# ---------------------------------------------------------------------------


def _set_password(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("HOUSEHOLD_PASSWORD", value)
    get_settings.cache_clear()


async def test_household_password_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_password(monkeypatch, "correct horse battery staple")

    result = await require_subscriber(
        FakeSubscriberRepository(), authorization="Bearer correct horse battery staple"
    )

    assert result.label == "household (shared password)"


async def test_wrong_household_password_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_password(monkeypatch, "correct horse battery staple")

    with pytest.raises(HTTPException) as exc_info:
        await require_subscriber(FakeSubscriberRepository(), authorization="Bearer wrong")
    assert exc_info.value.status_code == 401


async def test_secret_stored_with_a_trailing_newline_still_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The cockpit-token incident (2026-10-24): a secret populated via a pipe
    # kept its newline, so no typed value could ever match.
    _set_password(monkeypatch, "correct horse battery staple\n")

    result = await require_subscriber(
        FakeSubscriberRepository(), authorization="Bearer correct horse battery staple"
    )

    assert result.label == "household (shared password)"


@pytest.mark.parametrize("unset", ["", "   "])
async def test_an_empty_household_password_never_matches(
    monkeypatch: pytest.MonkeyPatch, unset: str
) -> None:
    # Fail closed: an empty/whitespace setting must not make "Bearer " valid.
    _set_password(monkeypatch, unset)

    with pytest.raises(HTTPException) as exc_info:
        await require_subscriber(FakeSubscriberRepository(), authorization="Bearer ")
    assert exc_info.value.status_code == 401


async def test_a_non_ascii_guess_is_a_401_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    # secrets.compare_digest raises TypeError on non-ASCII str — must be bytes.
    _set_password(monkeypatch, "correct horse battery staple")

    with pytest.raises(HTTPException) as exc_info:
        await require_subscriber(FakeSubscriberRepository(), authorization="Bearer café")
    assert exc_info.value.status_code == 401


async def test_subscriber_tokens_still_work_alongside_the_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_password(monkeypatch, "correct horse battery staple")
    subscriber = Subscriber(id=uuid4(), label="me", created_at=datetime(2026, 1, 1, tzinfo=UTC))
    repo = FakeSubscriberRepository({hash_token("good-token"): subscriber})

    result = await require_subscriber(repo, authorization="Bearer good-token")

    assert result == subscriber


async def test_a_wrong_credential_is_answered_only_after_the_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def _record(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(auth_module, "FAILED_AUTH_DELAY_SECONDS", 1.0)
    monkeypatch.setattr("acamar.api.auth.asyncio.sleep", _record)

    with pytest.raises(HTTPException):
        await require_subscriber(FakeSubscriberRepository(), authorization="Bearer nope")

    assert sleeps == [1.0]


async def test_a_missing_header_is_rejected_without_the_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Nothing was guessed, so there's nothing to slow down.
    sleeps: list[float] = []

    async def _record(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("acamar.api.auth.asyncio.sleep", _record)

    with pytest.raises(HTTPException):
        await require_subscriber(FakeSubscriberRepository(), authorization=None)

    assert sleeps == []


async def test_the_household_password_never_grants_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_password(monkeypatch, "correct horse battery staple")
    monkeypatch.setenv("COCKPIT_TOKEN", "the-real-cockpit-token")
    get_settings.cache_clear()

    with pytest.raises(HTTPException) as exc_info:
        await require_admin(authorization="Bearer correct horse battery staple")
    assert exc_info.value.status_code == 401
