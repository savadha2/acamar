"""`MailClient` backed by the Gmail API v1 (OAuth local-browser consent flow)."""

from __future__ import annotations

import asyncio
import base64
import binascii
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from acamar.config import get_settings
from acamar.ports.mail_client import MailError, RawEmail

_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GoogleGmailClient:
    """Reads a Gmail inbox via the Gmail API v1.

    OAuth runs once per machine: the first call with no cached token opens a
    local-browser consent flow against ``credentials_path`` (the OAuth client
    secret downloaded from Google Cloud Console) and caches a refresh token at
    ``token_path``. Later runs load and silently refresh that token — no
    repeated logins. Both paths are gitignored; never in source.

    The Gmail client library is synchronous, so every call is wrapped in
    ``asyncio.to_thread`` to keep this port's ``search`` async, consistent
    with the rest of the codebase (architecture.md decision #5).
    """

    def __init__(
        self,
        *,
        credentials_path: str | None = None,
        token_path: str | None = None,
    ) -> None:
        settings = get_settings()
        self._credentials_path = Path(credentials_path or settings.gmail_credentials_path)
        self._token_path = Path(token_path or settings.gmail_token_path)
        self._service: Any | None = None

    async def search(self, query: str, *, max_results: int = 20) -> list[RawEmail]:
        """List matching messages, then fetch and map each in full.

        Raises:
            MailError: OAuth, transport, or the API itself failed.
        """
        try:
            return await asyncio.to_thread(self._search_sync, query, max_results)
        except MailError:
            raise
        except Exception as exc:
            raise MailError(f"gmail search {query!r} failed: {exc}") from exc

    def _search_sync(self, query: str, max_results: int) -> list[RawEmail]:
        if self._service is None:
            self._service = build("gmail", "v1", credentials=self._credentials_sync())
        service = self._service
        listing = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=max_results)
            .execute()
        )
        message_ids = [m["id"] for m in listing.get("messages", [])]
        emails: list[RawEmail] = []
        for message_id in message_ids:
            message = (
                service.users()
                .messages()
                .get(userId="me", id=message_id, format="full")
                .execute()
            )
            emails.append(_to_raw_email(message))
        return emails

    def _credentials_sync(self) -> Credentials:
        creds: Credentials | None = None
        if self._token_path.exists():
            creds = Credentials.from_authorized_user_file(str(self._token_path), _SCOPES)

        if creds is not None and creds.valid:
            return creds

        if creds is not None and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:
                # Real bug found 2026-09-24: Google can revoke the refresh
                # token itself (not just the short-lived access token) —
                # "invalid_grant: Token has been expired or revoked". This
                # used to propagate straight out as a MailError with no
                # recovery short of manually deleting the cached token
                # file. Fall through to a fresh interactive consent
                # instead, same as the "no cached token at all" case below
                # — matches what this method's own docstring already
                # promises ("silently refreshing it if expired").
                creds = None
            else:
                self._save_token(creds)
                return creds

        if not self._credentials_path.exists():
            raise MailError(
                f"no Gmail OAuth client secret at {self._credentials_path} "
                "(download it from Google Cloud Console — see docs/architecture.md §1.7)"
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(self._credentials_path), _SCOPES)
        creds = flow.run_local_server(port=0)
        self._save_token(creds)
        return creds

    def _save_token(self, creds: Credentials) -> None:
        self._token_path.parent.mkdir(parents=True, exist_ok=True)
        self._token_path.write_text(creds.to_json())


def _to_raw_email(message: dict[str, Any]) -> RawEmail:
    """Pure mapping from a Gmail API message dict to `RawEmail`.

    Testable against synthetic message dicts — no network/OAuth needed.

    Raises:
        MailError: the message has no ``Message-ID`` header.
    """
    payload = message.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    message_id = headers.get("message-id")
    if not message_id:
        raise MailError(f"gmail message {message.get('id')} has no Message-ID header")
    text_body, html_body = _extract_bodies(payload)
    return RawEmail(
        message_id=message_id,
        subject=headers.get("subject", ""),
        sender=headers.get("from", ""),
        received_at=_parse_date(headers.get("date")),
        text_body=text_body,
        html_body=html_body,
    )


def _parse_date(raw: str | None) -> datetime:
    if raw is None:
        return datetime.now(UTC)
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return datetime.now(UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _extract_bodies(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    """Recursively walk MIME ``parts`` for the first ``text/plain`` and ``text/html`` body.

    Malformed base64 in a part is skipped (that body stays ``None``) rather
    than failing the whole message — a bad part shouldn't lose a menu email.
    """
    text_body: str | None = None
    html_body: str | None = None

    mime_type = payload.get("mimeType", "")
    data = payload.get("body", {}).get("data")
    if data:
        decoded = _decode_body(data)
        if mime_type == "text/plain":
            text_body = decoded
        elif mime_type == "text/html":
            html_body = decoded

    for part in payload.get("parts", []):
        part_text, part_html = _extract_bodies(part)
        if text_body is None:
            text_body = part_text
        if html_body is None:
            html_body = part_html

    return text_body, html_body


def _decode_body(data: str) -> str | None:
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError):
        return None
