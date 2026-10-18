"""Parses one weekly menu email into per-day `RawDocument`s.

Stub until a real sample email is captured (CLAUDE.md build step 3) — the
district's menu subscription must be set up first. Not guessed at.
"""

from __future__ import annotations

from acamar.domain.models import RawDocument
from acamar.ports.mail_client import RawEmail


def parse_menu_email(email: RawEmail) -> list[RawDocument]:
    """Parse one weekly menu email into one `RawDocument` per calendar day.

    ``external_id`` must be ``f"{email.message_id}::{event_date.isoformat()}"``
    (settled in docs/architecture.md §1.7): one email covers a whole week, so
    the bare Message-ID can't be the dedup key on its own.

    Raises:
        NotImplementedError: always, until this is implemented against a real
            sample email.
    """
    raise NotImplementedError(
        "parse_menu_email: no real sample menu email captured yet — "
        "see docs/architecture.md §1.7 and CLAUDE.md build step 3"
    )
