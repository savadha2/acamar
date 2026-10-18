"""`require_subscriber` — bearer-token auth, plus the `disable_auth` escape hatch."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException

from acamar.api.auth import require_subscriber
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
