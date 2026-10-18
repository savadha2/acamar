"""The email-search port. The core never calls the Gmail API directly."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class MailError(RuntimeError):
    """A mail client failed to authenticate, search, or fetch a message."""


class RawEmail(BaseModel):
    """One fetched message, transport detail already stripped away.

    ``message_id`` is the RFC-822 ``Message-ID`` header — stable across
    re-fetches, and feeds a Gmail-sourced connector's ``external_id``.
    """

    model_config = ConfigDict(frozen=True)

    message_id: str = Field(min_length=1)
    subject: str
    sender: str
    received_at: datetime
    text_body: str | None = None
    html_body: str | None = None


class MailClient(Protocol):
    """Searches a mailbox and returns fully-hydrated messages."""

    async def search(self, query: str, *, max_results: int = 20) -> list[RawEmail]:
        """Return matching messages, newest first.

        Hides the transport's list-then-get shape (e.g. Gmail API v1) — callers
        never see a bare message id.

        Raises:
            MailError: the mailbox could not be searched or a message could
                not be fetched.
        """
        ...
