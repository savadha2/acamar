"""Maps a `Source` to the connector that ingests it.

Adding a source type is one entry here + one connector class — no change to
`IngestionService`.
"""

from __future__ import annotations

from collections.abc import Callable

from acamar.domain.enums import Category, Domain
from acamar.domain.models import Source
from acamar.ports.source_connector import ConnectorError, SourceConnector
from acamar.sources.cupertino_events.connector import CupertinoEventsConnector
from acamar.sources.faria_events.connector import FariaEventsConnector
from acamar.sources.gmail_menu.connector import GmailMenuConnector
from acamar.sources.gmail_school.connector import GmailSchoolConnector
from acamar.sources.manual_menu.connector import ManualMenuConnector

_Builder = Callable[[Source], SourceConnector]

_REGISTRY: dict[tuple[Domain, Category], _Builder] = {
    (Domain.SCHOOL, Category.EVENT): lambda source: FariaEventsConnector(
        calendar_url=source.config.get("calendar_url"),
    ),
    # This source's row needs config={"reconcile": false} — a monthly upload
    # is a partial view of what's still valid, same reason Gmail sources
    # always skip reconciliation (see IngestionService._should_reconcile).
    (Domain.SCHOOL, Category.MENU): lambda source: ManualMenuConnector(
        upload_dir=source.config.get("upload_dir"),
    ),
    (Domain.GMAIL, Category.MENU): lambda source: GmailMenuConnector(
        query=source.config.get("query"),
    ),
    (Domain.GMAIL, Category.EVENT): lambda source: GmailSchoolConnector(
        query=source.config.get("query"),
        since=source.last_run_at,
    ),
    (Domain.CITY, Category.EVENT): lambda source: CupertinoEventsConnector(
        index_url=source.config.get("index_url"),
    ),
}


def connector_for(source: Source) -> SourceConnector:
    """Raises :class:`ConnectorError` if no connector is registered."""
    builder = _REGISTRY.get((source.domain, source.category))
    if builder is None:
        raise ConnectorError(
            f"no connector for {source.domain}/{source.category}",
            source=str(source.id),
        )
    return builder(source)
