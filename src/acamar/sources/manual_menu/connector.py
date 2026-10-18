"""Connector that parses every staged breakfast/lunch menu PDF it's been given.

Not a scraper and not schedule-driven: `POST /admin/menu-upload` (the Pilot
Cockpit) is the trigger, this connector only reads the filesystem hand-off
that endpoint leaves behind. Every staged file is re-parsed on every run, not
just the newest — a monthly upload is a partial view of what's still valid
(last month's menu is still a legitimate, queryable document), the same
reason `IngestionService` skips reconciliation for this source (its
`sources` row needs `config={"reconcile": false}`).
"""

from __future__ import annotations

import re
from pathlib import Path

from acamar.config import get_settings
from acamar.domain.models import RawDocument
from acamar.ports.source_connector import ConnectorError
from acamar.sources.manual_menu.parser import Meal, parse_menu_pdf

_MEALS: tuple[Meal, ...] = ("breakfast", "lunch")
_STAGED_FILENAME = re.compile(r"^(?P<year>\d{4})-(?P<month>\d{2})\.pdf$")


class ManualMenuConnector:
    """Parses every ``{year}-{month}.pdf`` staged under ``upload_dir/<meal>/``.

    The upload endpoint writes exactly one file per (meal, year, month) — a
    re-upload for the same month overwrites it, so a corrected PDF replaces
    the wrong one instead of accumulating duplicates. Every argument defaults
    to :func:`acamar.config.get_settings`, same pattern as every other
    connector.
    """

    name = "manual_menu"

    def __init__(self, *, upload_dir: str | None = None) -> None:
        self._upload_dir = Path(upload_dir or get_settings().cockpit_upload_dir)

    async def fetch(self) -> list[RawDocument]:
        """Parse every staged PDF, for every meal, into per-day documents.

        Raises:
            ConnectorError: a staged file fails to parse.
        """
        documents: list[RawDocument] = []
        try:
            for meal in _MEALS:
                for path in sorted((self._upload_dir / meal).glob("*.pdf")):
                    match = _STAGED_FILENAME.match(path.name)
                    if match is None:
                        continue  # not one of ours — ignore, don't fail the whole run
                    documents.extend(
                        parse_menu_pdf(
                            path.read_bytes(),
                            meal=meal,
                            year=int(match.group("year")),
                            month=int(match.group("month")),
                        )
                    )
        except Exception as exc:
            # Deliberately broad: a filesystem hiccup or a parser failure
            # (unexpected PDF layout) must both surface as ConnectorError so
            # the pipeline records last_status='error', not a silent gap.
            raise ConnectorError(f"{self.name}: {exc}", source=self.name) from exc
        return documents
