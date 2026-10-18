"""Run one source's ingestion — manual trigger and scheduler entry point.

    uv run python -m scripts.run_ingestion --source <uuid>

Exit code 0 on success, 1 if the run recorded an error.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from uuid import UUID

from acamar.adapters.clock import SystemClock
from acamar.adapters.db.checks import assert_embedding_dim
from acamar.adapters.db.document_repository import PostgresDocumentRepository
from acamar.adapters.db.pool import connection_pool
from acamar.adapters.db.source_repository import PostgresSourceRepository
from acamar.adapters.embedding.ollama_embedder import OllamaEmbedder
from acamar.adapters.enrichment.ollama_enricher import OllamaEnricher
from acamar.config import get_settings
from acamar.domain.enums import SourceStatus
from acamar.ingestion.connector_factory import connector_for
from acamar.ingestion.service import IngestionService


async def run(source_id: UUID) -> SourceStatus:
    settings = get_settings()
    async with connection_pool() as pool:
        await assert_embedding_dim(pool, settings.embedding_dim)
        service = IngestionService(
            sources=PostgresSourceRepository(pool),
            documents=PostgresDocumentRepository(pool),
            embedder=OllamaEmbedder(),
            enricher=OllamaEnricher(),
            connector_factory=connector_for,
            clock=SystemClock(),
        )
        report = await service.run(source_id)
    print(report.model_dump_json(indent=2))
    return report.status


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Run one source's ingestion.")
    parser.add_argument("--source", required=True, type=UUID, help="a sources.id")
    args = parser.parse_args(argv)
    status = asyncio.run(run(args.source))
    sys.exit(0 if status is SourceStatus.OK else 1)


if __name__ == "__main__":
    main()
