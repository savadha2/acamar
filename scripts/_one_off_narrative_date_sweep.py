"""One-off: apply the new narrative-date extraction retroactively to
EXISTING summaries (no LLM call — free, since the summary text is already
stored). Catches cases where a prior enrichment run's summary correctly
stated a date but the structured event_date field was left null (and, for
rows the cache-reuse bug wiped, the summary is still the original correct
one even though event_date got dropped).

    uv run python -m scripts._one_off_narrative_date_sweep
"""

from __future__ import annotations

import asyncio

import psycopg

from acamar.domain.enums import Category
from acamar.ingestion.service import _extract_narrative_date

_SOURCE_ID = "195af33e-563b-4a35-9b04-0872a46d826e"

_SELECT = """
    SELECT id, title, summary, published_at
    FROM documents
    WHERE source_id = %s AND deleted_at IS NULL AND dismissed_at IS NULL
      AND domain = 'gmail' AND event_date IS NULL AND is_relevant = true
      AND summary IS NOT NULL
"""

_UPDATE = "UPDATE documents SET event_date = %s, category = %s, updated_at = now() WHERE id = %s"


async def main() -> None:
    async with await psycopg.AsyncConnection.connect(
        "postgresql://localhost:5432/acamar", row_factory=psycopg.rows.dict_row
    ) as conn:
        rows = await (await conn.execute(_SELECT, (_SOURCE_ID,))).fetchall()
        print(f"{len(rows)} undated documents with an existing summary to check")

        found = 0
        for row in rows:
            event_date = _extract_narrative_date(
                row["summary"], reference=row["published_at"].date()
            )
            if event_date is None:
                continue
            await conn.execute(_UPDATE, (event_date, Category.EVENT, row["id"]))
            found += 1
            print(f"BACKFILLED {row['title']!r} -> {event_date}")
        await conn.commit()
        print(f"{found} of {len(rows)} backfilled for free (no LLM call)")


if __name__ == "__main__":
    asyncio.run(main())
