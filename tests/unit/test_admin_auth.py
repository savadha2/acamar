"""`require_admin` — the Pilot Cockpit's separate bearer-token gate."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from acamar.api.auth import require_admin
from acamar.config import get_settings


async def test_no_cockpit_token_configured_is_401_even_with_a_header() -> None:
    # Unset must fail loudly, not silently accept nothing / anything.
    with pytest.raises(HTTPException) as exc_info:
        await require_admin(authorization="Bearer whatever")
    assert exc_info.value.status_code == 401


async def test_valid_admin_token_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COCKPIT_TOKEN", "secret-admin-token")
    get_settings.cache_clear()

    await require_admin(authorization="Bearer secret-admin-token")  # does not raise


async def test_wrong_token_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COCKPIT_TOKEN", "secret-admin-token")
    get_settings.cache_clear()

    with pytest.raises(HTTPException) as exc_info:
        await require_admin(authorization="Bearer nope")
    assert exc_info.value.status_code == 401


async def test_missing_header_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COCKPIT_TOKEN", "secret-admin-token")
    get_settings.cache_clear()

    with pytest.raises(HTTPException) as exc_info:
        await require_admin(authorization=None)
    assert exc_info.value.status_code == 401


async def test_disable_auth_bypasses_the_check_entirely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISABLE_AUTH", "true")
    get_settings.cache_clear()

    await require_admin(authorization=None)  # does not raise
