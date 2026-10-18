"""Connector that pulls school events/reminders out of a filtered Gmail search."""

from __future__ import annotations

from datetime import datetime, timedelta

from acamar.adapters.gmail.client import GoogleGmailClient
from acamar.config import get_settings
from acamar.domain.models import RawDocument
from acamar.ports.mail_client import MailClient
from acamar.ports.source_connector import ConnectorError
from acamar.sources.gmail_school.parser import parse_school_email

_DEFAULT_MAX_RESULTS = 25
_SINCE_BUFFER_DAYS = 1
"""Overlap so `after:` (day granularity) can't skip a same-day message that
arrived earlier than the previous run's time-of-day."""


class GmailSchoolConnector:
    """Fetches school/ParentSquare mail and parses each message into a document.

    Gmail is not menu-only (CLAUDE.md § "Current scope"): this is a second,
    independent Gmail-sourced connector, registered under its own
    ``(Domain.GMAIL, Category.EVENT)`` factory entry. ``query`` typically
    selects a curated label (e.g. ``"label:our-sweet-family-..."``) rather than
    a single sender, since school mail comes from many addresses via
    ParentSquare. There is no default — an unset query must fail loudly.

    ``since`` (typically ``source.last_run_at``) makes this incremental: the
    Gmail API has no batch-fetch, so a flat search re-does one HTTP round
    trip per message every run regardless of whether anything changed —
    the 2026-09-15 runtime blowup that prompted this. First run (``since``
    unset) still does the full, unbounded search. Incremental fetch means
    "not returned this run" no longer implies "removed upstream", so
    ``IngestionService`` skips reconciliation for Gmail-domain sources
    entirely — see its docstring.
    """

    name = "gmail_school"

    def __init__(
        self,
        *,
        query: str | None = None,
        since: datetime | None = None,
        mail: MailClient | None = None,  # inject in tests
        max_results: int = _DEFAULT_MAX_RESULTS,
    ) -> None:
        settings = get_settings()
        resolved_query = query or settings.gmail_school_search_query
        if not resolved_query:
            raise ConnectorError(
                f"{self.name}: no Gmail search query configured "
                "(set GMAIL_SCHOOL_SEARCH_QUERY or the source's config.query)",
                source=self.name,
            )
        self._query = resolved_query
        self._since = since
        self._mail = mail or GoogleGmailClient()
        self._max_results = max_results

    async def fetch(self) -> list[RawDocument]:
        """Search the inbox and parse every matching email into documents.

        Raises:
            ConnectorError: the mailbox couldn't be searched.
        """
        query = self._query
        if self._since is not None:
            cutoff = (self._since - timedelta(days=_SINCE_BUFFER_DAYS)).date()
            query = f"{query} after:{cutoff:%Y/%m/%d}"
        try:
            emails = await self._mail.search(query, max_results=self._max_results)
        except Exception as exc:
            # Deliberately broad: any MailClient failure (OAuth, transport, API)
            # must surface as ConnectorError so the pipeline records
            # last_status='error', not a silent gap. The parser itself never
            # raises (see parse_school_email), so nothing else reaches here.
            raise ConnectorError(f"{self.name}: {exc}", source=self.name) from exc

        documents: list[RawDocument] = []
        for email in emails:
            documents.extend(parse_school_email(email))
        return documents
