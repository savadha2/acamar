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


async def test_disable_auth_does_not_bypass_the_admin_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real incident, 2026-10-24 (docs/deployment-rcca-2026-10-24.md issue
    4): `require_admin` used to share `require_subscriber`'s `disable_auth`
    bypass, so turning off the household dashboard's token prompt silently
    also opened `/admin/shutdown`/pause/menu-upload to anyone — confirmed
    exploitable against the real deployed cloud API before this fix.
    `disable_auth` is specifically about `require_subscriber`; admin stays
    gated by `cockpit_token` regardless of it.
    """
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("COCKPIT_TOKEN", "secret-admin-token")
    get_settings.cache_clear()

    with pytest.raises(HTTPException) as exc_info:
        await require_admin(authorization=None)
    assert exc_info.value.status_code == 401

    await require_admin(authorization="Bearer secret-admin-token")  # a real token still works
