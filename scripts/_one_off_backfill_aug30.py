"""One-off: backfill gmail_school from Aug 30 through now, one time.

Goes through the real `IngestionService` (same guardrails, dedup,
enrichment, embedding as a normal run) — just with an explicit `after:`
bound instead of the usual `since=source.last_run_at` incremental one.
Safe to re-run: existing messages (external_id = Message-ID) just upsert in
place, and change-detection skips re-enriching anything unchanged.

    uv run python -m scripts._one_off_backfill_aug30
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from acamar.adapters.clock import SystemClock
from acamar.adapters.db.checks import assert_embedding_dim
from acamar.adapters.db.document_repository import PostgresDocumentRepository
from acamar.adapters.db.pool import connection_pool
from acamar.adapters.db.source_repository import PostgresSourceRepository
from acamar.adapters.embedding.ollama_embedder import OllamaEmbedder
from acamar.adapters.enrichment.ollama_enricher import OllamaEnricher
from acamar.config import get_settings
from acamar.domain.models import Source
from acamar.ingestion.service import IngestionService
from acamar.ports.source_connector import SourceConnector
from acamar.sources.gmail_school.connector import GmailSchoolConnector

_SOURCE_ID = "195af33e-563b-4a35-9b04-0872a46d826e"
_AFTER = "2026/08/30"
_MAX_RESULTS = 60  # Gmail returns newest-first; the Aug 30 range needs headroom
# beyond what a normal-sized fetch would keep, or the older half gets truncated.


def _connector_factory(source: Source) -> SourceConnector:
    base_query = source.config.get("query") or get_settings().gmail_school_search_query
    return GmailSchoolConnector(
        query=f"{base_query} after:{_AFTER}", max_results=_MAX_RESULTS
    )


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = get_settings()
    async with connection_pool() as pool:
        await assert_embedding_dim(pool, settings.embedding_dim)
        service = IngestionService(
            sources=PostgresSourceRepository(pool),
            documents=PostgresDocumentRepository(pool),
            embedder=OllamaEmbedder(),
            enricher=OllamaEnricher(),
            connector_factory=_connector_factory,
            clock=SystemClock(),
        )
        report = await service.run(UUID(_SOURCE_ID))
    print(report.model_dump_json(indent=2))


if __name__ == "__main__":
    asyncio.run(main())
