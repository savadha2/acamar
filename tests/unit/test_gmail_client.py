"""`_to_raw_email`/`_extract_bodies` (pure MIME mapping) and
`GoogleGmailClient._credentials_sync`'s refresh/fallback logic (OAuth
classes mocked — no real network/browser flow)."""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any

import pytest
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from acamar.adapters.gmail.client import GoogleGmailClient, _extract_bodies, _to_raw_email
from acamar.ports.mail_client import MailError


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def _headers(**overrides: str) -> list[dict[str, str]]:
    base = {
        "Message-ID": "<abc123@district.example>",
        "Subject": "This week's menu",
        "From": "noreply@cusdk8nutrition.com",
        "Date": "Mon, 14 Sep 2026 06:00:00 -0700",
    }
    base.update(overrides)
    return [{"name": k, "value": v} for k, v in base.items()]


def _message(payload: dict[str, Any], **header_overrides: str) -> dict[str, Any]:
    payload = {**payload, "headers": _headers(**header_overrides)}
    return {"id": "gmail-msg-1", "payload": payload}


# ---------------------------------------------------------------------------
# _to_raw_email
# ---------------------------------------------------------------------------


def test_plain_text_only() -> None:
    message = _message(
        {"mimeType": "text/plain", "body": {"data": _b64("Monday: pizza")}}
    )
    email = _to_raw_email(message)
    assert email.message_id == "<abc123@district.example>"
    assert email.subject == "This week's menu"
    assert email.sender == "noreply@cusdk8nutrition.com"
    assert email.text_body == "Monday: pizza"
    assert email.html_body is None


def test_html_only() -> None:
    message = _message(
        {"mimeType": "text/html", "body": {"data": _b64("<p>Monday: pizza</p>")}}
    )
    email = _to_raw_email(message)
    assert email.text_body is None
    assert email.html_body == "<p>Monday: pizza</p>"


def test_multipart_picks_up_both_bodies() -> None:
    message = _message(
        {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64("plain body")}},
                {"mimeType": "text/html", "body": {"data": _b64("<p>html body</p>")}},
            ],
        }
    )
    email = _to_raw_email(message)
    assert email.text_body == "plain body"
    assert email.html_body == "<p>html body</p>"


def test_nested_multipart_is_walked_recursively() -> None:
    message = _message(
        {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {"mimeType": "text/plain", "body": {"data": _b64("nested plain")}},
                    ],
                },
            ],
        }
    )
    email = _to_raw_email(message)
    assert email.text_body == "nested plain"


def test_missing_message_id_raises_mail_error() -> None:
    message = _message({"mimeType": "text/plain", "body": {"data": _b64("x")}})
    message["payload"]["headers"] = [
        h for h in message["payload"]["headers"] if h["name"] != "Message-ID"
    ]
    with pytest.raises(MailError, match="Message-ID"):
        _to_raw_email(message)


def test_unparseable_date_falls_back_instead_of_raising() -> None:
    message = _message(
        {"mimeType": "text/plain", "body": {"data": _b64("x")}}, Date="not a date"
    )
    email = _to_raw_email(message)
    assert email.received_at is not None


# ---------------------------------------------------------------------------
# _extract_bodies
# ---------------------------------------------------------------------------


def test_malformed_base64_is_skipped_not_raised() -> None:
    payload = {"mimeType": "text/plain", "body": {"data": "!!!not-base64!!!"}}
    text_body, html_body = _extract_bodies(payload)
    assert text_body is None
    assert html_body is None


def test_malformed_part_does_not_poison_a_sibling_part() -> None:
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": "!!!not-base64!!!"}},
            {"mimeType": "text/html", "body": {"data": _b64("<p>ok</p>")}},
        ],
    }
    text_body, html_body = _extract_bodies(payload)
    assert text_body is None
    assert html_body == "<p>ok</p>"


def test_no_body_at_all_returns_none_none() -> None:
    assert _extract_bodies({"mimeType": "text/plain"}) == (None, None)


# ---------------------------------------------------------------------------
# GoogleGmailClient._credentials_sync — refresh + fallback-to-consent logic
# (google-auth classes mocked; no real network or browser)
# ---------------------------------------------------------------------------


class _FakeCreds:
    def __init__(
        self,
        *,
        valid: bool,
        expired: bool = False,
        refresh_token: str | None = None,
        refresh_error: Exception | None = None,
    ) -> None:
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token
        self._refresh_error = refresh_error
        self.refreshed = False

    def refresh(self, request: object) -> None:
        self.refreshed = True
        if self._refresh_error is not None:
            raise self._refresh_error

    def to_json(self) -> str:
        return "{}"


def test_valid_cached_credentials_are_used_as_is(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    token_path = tmp_path / "token.json"
    token_path.write_text("{}")
    creds = _FakeCreds(valid=True)
    monkeypatch.setattr(
        Credentials, "from_authorized_user_file", lambda *a, **k: creds
    )
    gmail_client = GoogleGmailClient(token_path=str(token_path))

    result = gmail_client._credentials_sync()

    assert result is creds  # type: ignore[comparison-overlap]
    assert creds.refreshed is False


def test_expired_credentials_are_refreshed_and_saved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    token_path = tmp_path / "token.json"
    token_path.write_text("{}")
    creds = _FakeCreds(valid=False, expired=True, refresh_token="rt")
    monkeypatch.setattr(
        Credentials, "from_authorized_user_file", lambda *a, **k: creds
    )
    gmail_client = GoogleGmailClient(token_path=str(token_path))

    result = gmail_client._credentials_sync()

    assert result is creds  # type: ignore[comparison-overlap]
    assert creds.refreshed is True
    assert token_path.read_text() == "{}"  # _save_token wrote creds.to_json()


def test_a_revoked_refresh_token_falls_back_to_interactive_consent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    # Real bug (2026-09-24): Google can revoke the refresh token itself
    # ("invalid_grant: Token has been expired or revoked"), not just expire
    # the access token — `creds.refresh()` raising here used to propagate
    # straight out as a MailError, with no recovery short of manually
    # deleting the cached token file. Must fall through to a fresh
    # interactive consent instead, same as "no cached token at all" below.
    token_path = tmp_path / "token.json"
    token_path.write_text("{}")
    creds_path = tmp_path / "creds.json"
    creds_path.write_text("{}")
    refresh_error = RefreshError("invalid_grant")  # type: ignore[no-untyped-call]
    dead_creds = _FakeCreds(
        valid=False, expired=True, refresh_token="rt", refresh_error=refresh_error
    )
    fresh_creds = _FakeCreds(valid=True)
    monkeypatch.setattr(
        Credentials, "from_authorized_user_file", lambda *a, **k: dead_creds
    )
    fake_flow = SimpleNamespace(run_local_server=lambda **k: fresh_creds)
    monkeypatch.setattr(
        InstalledAppFlow, "from_client_secrets_file", lambda *a, **k: fake_flow
    )
    gmail_client = GoogleGmailClient(credentials_path=str(creds_path), token_path=str(token_path))

    result = gmail_client._credentials_sync()

    assert result is fresh_creds  # type: ignore[comparison-overlap]
    assert dead_creds.refreshed is True


def test_no_cached_token_goes_straight_to_interactive_consent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    creds_path = tmp_path / "creds.json"
    creds_path.write_text("{}")
    fresh_creds = _FakeCreds(valid=True)
    fake_flow = SimpleNamespace(run_local_server=lambda **k: fresh_creds)
    monkeypatch.setattr(
        InstalledAppFlow, "from_client_secrets_file", lambda *a, **k: fake_flow
    )
    gmail_client = GoogleGmailClient(
        credentials_path=str(creds_path), token_path=str(tmp_path / "missing.json")
    )

    result = gmail_client._credentials_sync()

    assert result is fresh_creds  # type: ignore[comparison-overlap]


def test_missing_client_secret_raises_a_clear_mail_error(tmp_path: Any) -> None:
    gmail_client = GoogleGmailClient(
        credentials_path=str(tmp_path / "missing-creds.json"),
        token_path=str(tmp_path / "missing-token.json"),
    )
    with pytest.raises(MailError, match="no Gmail OAuth client secret"):
        gmail_client._credentials_sync()


def _flow_that_must_not_run(*a: Any, **k: Any) -> Any:
    raise AssertionError("interactive consent must never start in a headless run")


def test_headless_revoked_token_raises_instead_of_opening_consent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    # Backlog item 48: a launchd run hung 16 days in run_local_server. On the
    # GCP job (interactive=False) a dead token must fail the run, not hang it.
    token_path = tmp_path / "token.json"
    token_path.write_text("{}")
    refresh_error = RefreshError("invalid_grant")  # type: ignore[no-untyped-call]
    dead_creds = _FakeCreds(
        valid=False, expired=True, refresh_token="rt", refresh_error=refresh_error
    )
    monkeypatch.setattr(
        Credentials, "from_authorized_user_file", lambda *a, **k: dead_creds
    )
    monkeypatch.setattr(InstalledAppFlow, "from_client_secrets_file", _flow_that_must_not_run)
    gmail_client = GoogleGmailClient(token_path=str(token_path), interactive=False)

    with pytest.raises(MailError, match="gmail_auth"):
        gmail_client._credentials_sync()


def test_headless_missing_token_raises_instead_of_opening_consent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setattr(InstalledAppFlow, "from_client_secrets_file", _flow_that_must_not_run)
    gmail_client = GoogleGmailClient(
        token_path=str(tmp_path / "missing.json"), interactive=False
    )

    with pytest.raises(MailError, match="interactive consent is off"):
        gmail_client._credentials_sync()


def test_interactive_auth_defaults_on_from_settings() -> None:
    assert GoogleGmailClient()._interactive is True


def test_refresh_still_succeeds_when_the_token_file_is_read_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    # On GCP the token is a read-only Secret Manager mount, and the stored
    # access token is always expired by the time a job starts — so every run
    # refreshes, and a failed write-back must not abort the run.
    token_path = tmp_path / "token.json"
    token_path.write_text('{"original": true}')
    token_path.chmod(0o444)
    creds = _FakeCreds(valid=False, expired=True, refresh_token="rt")
    monkeypatch.setattr(
        Credentials, "from_authorized_user_file", lambda *a, **k: creds
    )
    gmail_client = GoogleGmailClient(token_path=str(token_path), interactive=False)

    result = gmail_client._credentials_sync()

    assert result is creds  # type: ignore[comparison-overlap]
    assert token_path.read_text() == '{"original": true}'
