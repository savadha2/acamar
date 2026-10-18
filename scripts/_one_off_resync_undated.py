"""One-off: retroactively apply the general event_date LLM fallback to
already-ingested, still-undated gmail_school documents.

The normal ingestion pipeline's change-detection would wrongly *reuse* their
existing enrichment (content unchanged) and never actually call the model
with the new prompt — this bypasses that cache deliberately, once.

    uv run python -m scripts._one_off_resync_undated
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, timedelta

from acamar.adapters.enrichment.ollama_enricher import OllamaEnricher
from acamar.domain.enums import Category, Domain
from acamar.domain.models import RawDocument
from acamar.ports.enricher import EnrichmentError

_SOURCE_ID = "195af33e-563b-4a35-9b04-0872a46d826e"

# Matches DashboardService's default window_days — a document published
# before this is already excluded from the dashboard's undated_since filter
# regardless of what event_date we might find, so there's nothing to gain
# from spending an LLM call on it. Mark it not-relevant and move on.
_STALENESS_WINDOW_DAYS = 7

_SELECT = """
    SELECT id, title, content, published_at
    FROM documents
    WHERE source_id = %s AND deleted_at IS NULL AND dismissed_at IS NULL
      AND domain = 'gmail' AND event_date IS NULL AND is_relevant = true
    ORDER BY published_at
"""

_UPDATE = """
    UPDATE documents
    SET event_date = %s, category = %s, headline = %s, summary = %s,
        is_relevant = %s, organization = %s, updated_at = now()
    WHERE id = %s
"""

_MARK_STALE = "UPDATE documents SET is_relevant = false, updated_at = now() WHERE id = %s"


async def main() -> None:
    import psycopg

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    enricher = OllamaEnricher()
    cutoff = date.today() - timedelta(days=_STALENESS_WINDOW_DAYS)

    async with await psycopg.AsyncConnection.connect(
        "postgresql://localhost:5432/acamar", row_factory=psycopg.rows.dict_row
    ) as conn:
        all_rows = await (await conn.execute(_SELECT, (_SOURCE_ID,))).fetchall()
        stale = [r for r in all_rows if r["published_at"].date() < cutoff]
        rows = [r for r in all_rows if r["published_at"].date() >= cutoff]

        for row in stale:
            await conn.execute(_MARK_STALE, (row["id"],))
            print(f"STALE (skipped, no LLM call): {row['title']!r}", flush=True)
        await conn.commit()
        print(
            f"{len(stale)} marked stale for free; {len(rows)} left to actually re-check",
            flush=True,
        )

        for i, row in enumerate(rows, start=1):
            doc = RawDocument(
                external_id="n/a",
                title=row["title"],
                content=row["content"],
                domain=Domain.GMAIL,
                category=Category.REMINDER,
                published_at=row["published_at"],
            )
            print(
                f"[{i}/{len(rows)}] starting {row['title']!r} "
                f"({len(row['content'])} chars)...",
                flush=True,
            )
            started = time.perf_counter()
            try:
                enrichment = await enricher.enrich(doc)
            except EnrichmentError as exc:
                elapsed = time.perf_counter() - started
                print(f"[{i}/{len(rows)}] FAILED after {elapsed:.1f}s: {exc}", flush=True)
                continue
            elapsed = time.perf_counter() - started

            found_date = enrichment.event_date is not None
            category = Category.EVENT if found_date else Category.REMINDER
            await conn.execute(
                _UPDATE,
                (
                    enrichment.event_date,
                    category,
                    enrichment.headline,
                    enrichment.summary,
                    enrichment.is_relevant,
                    enrichment.organization,
                    row["id"],
                ),
            )
            await conn.commit()
            marker = f"DATE FOUND: {enrichment.event_date}" if found_date else "no date"
            print(
                f"[{i}/{len(rows)}] {row['title']!r} -> {marker}  ({elapsed:.1f}s)",
                flush=True,
            )


if __name__ == "__main__":
    asyncio.run(main())
