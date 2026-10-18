"""Connector that pulls the district's auto-subscribed menu email out of Gmail."""

from __future__ import annotations

from acamar.adapters.gmail.client import GoogleGmailClient
from acamar.config import get_settings
from acamar.domain.models import RawDocument
from acamar.ports.mail_client import MailClient
from acamar.ports.source_connector import ConnectorError
from acamar.sources.gmail_menu.parser import parse_menu_email


class GmailMenuConnector:
    """Fetches the subscribed menu email and parses it into per-day documents.

    Not a scraper: both nutrition-vendor sites disallow automated access in
    their ``robots.txt`` (settled in CLAUDE.md). The district instead emails
    the weekly menu to a subscribed address; this connector searches that
    inbox and parses the message body, never touching the vendor sites.

    ``query`` must select exactly the subscription emails (e.g.
    ``"from:noreply@cusdk8nutrition.com"``). There is no default — an unset
    query must fail loudly rather than silently matching nothing.
    """

    name = "gmail_menu"

    def __init__(
        self,
        *,
        query: str | None = None,
        mail: MailClient | None = None,  # inject in tests
        max_results: int = 5,
    ) -> None:
        settings = get_settings()
        resolved_query = query or settings.gmail_menu_search_query
        if not resolved_query:
            raise ConnectorError(
                f"{self.name}: no Gmail search query configured "
                "(set GMAIL_MENU_SEARCH_QUERY or the source's config.query)",
                source=self.name,
            )
        self._query = resolved_query
        self._mail = mail or GoogleGmailClient()
        self._max_results = max_results

    async def fetch(self) -> list[RawDocument]:
        """Search the inbox and parse every matching email into documents.

        Raises:
            ConnectorError: the mailbox couldn't be searched, or a matched
                email failed to parse.
        """
        try:
            emails = await self._mail.search(self._query, max_results=self._max_results)
            documents: list[RawDocument] = []
            for email in emails:
                documents.extend(parse_menu_email(email))
            return documents
        except ConnectorError:
            raise
        except Exception as exc:
            # Deliberately broad: a MailClient failure or a parser failure
            # (malformed/redesigned email) must both surface as ConnectorError
            # so the pipeline records last_status='error', not a silent gap.
            raise ConnectorError(f"{self.name}: {exc}", source=self.name) from exc
