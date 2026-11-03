"""Bearer-token auth.

Every route except ``/health`` and ``/admin/*`` needs either the shared
household password (``settings.household_password``, 2026-10-25) or an
active ``subscribers`` token. ``/admin/*`` is gated by a separate credential
(``settings.cockpit_token``, see ``require_admin`` below); neither the
household password nor a subscriber token grants admin access.
"""

from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status

from acamar.api.dependencies import get_subscriber_repository
from acamar.config import get_settings
from acamar.domain.models import Subscriber
from acamar.ports.repositories import SubscriberRepository
from acamar.security import hash_token

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="a valid access token is required",
    headers={"WWW-Authenticate": "Bearer"},
)

_DEV_SUBSCRIBER = Subscriber(
    id=UUID(int=0), label="dev (auth disabled)", created_at=datetime(2000, 1, 1, tzinfo=UTC)
)
"""Returned when `settings.disable_auth` is set — see `config.py`."""

_HOUSEHOLD = Subscriber(
    id=UUID(int=1),
    label="household (shared password)",
    created_at=datetime(2000, 1, 1, tzinfo=UTC),
)
"""Returned for the shared household password — see `config.household_password`."""

FAILED_AUTH_DELAY_SECONDS = 1.0
"""Pause before every 401 for a presented-but-wrong credential: slows
guessing to roughly one try per second per connection. Deliberately no
lockout — a stranger hammering the public URL must not be able to lock the
household out. A long, hand-picked password is the real defense."""


def _is_household_password(credential: str) -> bool:
    expected = (get_settings().household_password or "").strip()
    # Empty/unset never matches — same fail-closed rule as cockpit_token.
    # Bytes, not str: compare_digest raises TypeError on non-ASCII str, which
    # would turn a guess containing "é" into a 500 instead of a 401.
    return bool(expected) and secrets.compare_digest(credential.encode(), expected.encode())


async def require_subscriber(
    subscribers: Annotated[SubscriberRepository, Depends(get_subscriber_repository)],
    authorization: Annotated[str | None, Header()] = None,
) -> Subscriber:
    """Resolve the caller from an ``Authorization: Bearer`` header, or 401.

    The bearer value may be the household password or a subscriber token;
    a wrong one is answered only after `FAILED_AUTH_DELAY_SECONDS`.

    Skips the check entirely when ``settings.disable_auth`` is set — a
    deliberate, temporary, dev-only escape hatch (see `config.py`), not a
    silent bypass: it's opt-in via an explicit setting, off by default.
    """
    if get_settings().disable_auth:
        return _DEV_SUBSCRIBER
    if authorization is None or not authorization.lower().startswith("bearer "):
        raise _UNAUTHORIZED
    credential = authorization[7:].strip()
    if _is_household_password(credential):
        return _HOUSEHOLD
    subscriber = await subscribers.active_by_token_hash(hash_token(credential))
    if subscriber is None:
        await asyncio.sleep(FAILED_AUTH_DELAY_SECONDS)
        raise _UNAUTHORIZED
    return subscriber


CurrentSubscriber = Annotated[Subscriber, Depends(require_subscriber)]


_UNAUTHORIZED_ADMIN = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="a valid admin token is required",
    headers={"WWW-Authenticate": "Bearer"},
)


async def require_admin(authorization: Annotated[str | None, Header()] = None) -> None:
    """Bearer-token gate for the Pilot Cockpit's `/admin/*` routes.

    Deliberately separate from `require_subscriber`/`subscribers` — upload,
    traffic visibility, and the pause/shutdown controls are a materially
    bigger blast radius than the read-only dashboard's "any active token
    sees everything" model, so they get their own credential
    (`settings.cockpit_token`) rather than a wider grant on the existing one.

    Returns nothing (there's no admin domain object, unlike
    `require_subscriber`/`Subscriber`) — routes depend on this purely for
    its side effect of raising 401.

    Deliberately does NOT honor `disable_auth` — fixed 2026-10-24 (real
    incident, docs/deployment-rcca-2026-10-24.md issue 4's investigation
    surfaced it live): `DISABLE_AUTH=true` is meant to drop the household
    dashboard's token prompt, not also open `/admin/shutdown`/`/admin/
    pause`/menu-upload to the whole internet. The two credentials sharing
    one bypass flag meant turning off one silently turned off both —
    confirmed exploitable against the real deployed cloud API before this
    fix (`/admin/status` returned real data with zero auth header set).
    """
    settings = get_settings()
    # `not settings.cockpit_token` (not `is None`): an empty string is just
    # as "unset" as None here. A misconfigured-empty Secret Manager value
    # mustn't make `secrets.compare_digest("", "")` the de facto admin
    # credential — found by review 2026-10-24, same incident as above.
    if not settings.cockpit_token:
        raise _UNAUTHORIZED_ADMIN
    if authorization is None or not authorization.lower().startswith("bearer "):
        raise _UNAUTHORIZED_ADMIN
    if not secrets.compare_digest(authorization[7:].strip(), settings.cockpit_token):
        raise _UNAUTHORIZED_ADMIN


CurrentAdmin = Annotated[None, Depends(require_admin)]
