"""The contract every source agent (scraper, Gmail filter, ...) implements."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from acamar.domain.models import RawDocument


class ConnectorError(RuntimeError):
    """A source connector failed to fetch or parse its upstream data.

    The ingestion pipeline catches this to record ``sources.last_status = 'error'``
    rather than letting an unhandled exception mask a broken source. ``source``
    and ``__cause__`` are kept as structured attributes for logging.
    """

    def __init__(self, message: str, *, source: str) -> None:
        super().__init__(message)
        self.source = source


@runtime_checkable
class SourceConnector(Protocol):
    """Fetches an upstream source and returns normalised documents.

    A connector is responsible only for retrieval and parsing. It must not touch
    the database or the embedder, and re-running :meth:`fetch` must yield stable
    ``external_id`` values or the pipeline will insert duplicates.

    ``@runtime_checkable`` only gives shallow ``isinstance`` checks (attribute
    presence, not signatures) — real conformance is enforced by the shared
    connector contract test-suite, which every implementation must pass.
    """

    name: str
    """Short identifier used in logs and ``sources`` config (e.g. ``"faria_events"``)."""

    async def fetch(self) -> list[RawDocument]:
        """Return the current set of documents from the upstream source.

        Raises:
            ConnectorError: the source could not be fetched or parsed.
        """
        ...
