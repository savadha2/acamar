"""`DocumentRepository` backed by Postgres + pgvector."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
from pgvector import Vector as PgVector
from psycopg_pool import AsyncConnectionPool

from acamar.domain.enums import Category, Domain
from acamar.domain.models import Document, RawDocument, Vector

_Pool = AsyncConnectionPool[psycopg.AsyncConnection[dict[str, Any]]]

_COLUMNS = """
    id, source_id, domain, category, student_ref,
    title, content, headline, summary, is_relevant, organization,
    external_id, event_date, published_at, url
"""

# last_seen_at always bumps; deleted_at clears (a revived event); updated_at
# bumps only when the visible content changed.
_UPSERT = """
    INSERT INTO documents (
        source_id, domain, category, student_ref,
        title, content, headline, summary, is_relevant, organization,
        external_id, event_date, published_at, url, embedding
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (source_id, external_id) DO UPDATE SET
        domain       = EXCLUDED.domain,
        category     = EXCLUDED.category,
        student_ref  = EXCLUDED.student_ref,
        title        = EXCLUDED.title,
        content      = EXCLUDED.content,
        headline     = EXCLUDED.headline,
        summary      = EXCLUDED.summary,
        is_relevant  = EXCLUDED.is_relevant,
        organization = EXCLUDED.organization,
        event_date   = EXCLUDED.event_date,
        published_at = EXCLUDED.published_at,
        url          = EXCLUDED.url,
        embedding    = EXCLUDED.embedding,
        last_seen_at = now(),
        deleted_at   = NULL,
        updated_at   = CASE
            WHEN documents.title      IS DISTINCT FROM EXCLUDED.title
              OR documents.content    IS DISTINCT FROM EXCLUDED.content
              OR documents.event_date IS DISTINCT FROM EXCLUDED.event_date
            THEN now() ELSE documents.updated_at
        END
"""

_RECONCILE = """
    UPDATE documents SET deleted_at = now()
    WHERE source_id = %s
      AND deleted_at IS NULL
      AND event_date >= %s
      AND last_seen_at < %s
"""

_SEARCH = f"""
    SELECT {_COLUMNS}
    FROM documents
    WHERE deleted_at IS NULL
      AND dismissed_at IS NULL
      AND category = ANY(%s)
      AND (%s::text[] IS NULL OR domain = ANY(%s))
      AND (event_date IS NULL OR event_date BETWEEN %s AND %s)
    ORDER BY embedding <=> %s
    LIMIT %s
"""  # noqa: S608

_COUNT = """
    SELECT count(*) AS n
    FROM documents
    WHERE deleted_at IS NULL
      AND dismissed_at IS NULL
      AND category = ANY(%s)
      AND (%s::text[] IS NULL OR domain = ANY(%s))
      AND (event_date IS NULL OR event_date BETWEEN %s AND %s)
"""

_LIST = f"""
    SELECT {_COLUMNS}
    FROM documents
    WHERE deleted_at IS NULL
      AND dismissed_at IS NULL
      AND category = %s
      AND (
        (event_date IS NOT NULL AND event_date BETWEEN %s AND %s)
        OR (event_date IS NULL AND (published_at IS NULL OR published_at >= %s))
      )
    ORDER BY event_date NULLS LAST, title
    LIMIT %s
"""  # noqa: S608

_NEXT_EVENT_DATE = """
    SELECT MIN(event_date) AS next_date
    FROM documents
    WHERE deleted_at IS NULL
      AND dismissed_at IS NULL
      AND category = %s
      AND event_date > %s
"""

_DISMISS = """
    UPDATE documents SET dismissed_at = now()
    WHERE id = %s AND dismissed_at IS NULL
"""

_GET_EXISTING = f"""
    SELECT {_COLUMNS}
    FROM documents
    WHERE deleted_at IS NULL
      AND source_id = %s
      AND external_id = ANY(%s)
"""  # noqa: S608

_DATE_MIN = date(1, 1, 1)
_DATE_MAX = date(9999, 12, 31)


class PostgresDocumentRepository:
    def __init__(self, pool: _Pool) -> None:
        self._pool = pool

    async def upsert(
        self, source_id: UUID, documents: list[tuple[RawDocument, Vector]]
    ) -> int:
        if not documents:
            return 0
        params = [
            (
                source_id,
                doc.domain,
                doc.category,
                doc.student_ref,
                doc.title,
                doc.content,
                doc.headline,
                doc.summary,
                doc.is_relevant,
                doc.organization,
                doc.external_id,
                doc.event_date,
                doc.published_at,
                doc.url,
                PgVector(vector),
            )
            for doc, vector in documents
        ]
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.executemany(_UPSERT, params)
        return len(params)

    async def reconcile_deletions(
        self,
        source_id: UUID,
        *,
        run_started_at: datetime,
        on_or_after: date,
        grace: timedelta = timedelta(),
    ) -> int:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                _RECONCILE, (source_id, on_or_after, run_started_at - grace)
            )
        return cur.rowcount

    async def search(
        self,
        embedding: Vector,
        *,
        categories: list[Category],
        domains: list[Domain] | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int = 8,
    ) -> list[Document]:
        params = (
            categories,
            domains,
            domains,
            date_from or _DATE_MIN,
            date_to or _DATE_MAX,
            PgVector(embedding),
            limit,
        )
        async with self._pool.connection() as conn:
            rows = await (await conn.execute(_SEARCH, params)).fetchall()
        return [Document.model_validate(row) for row in rows]

    async def count(
        self,
        *,
        categories: list[Category],
        domains: list[Domain] | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> int:
        params = (
            categories,
            domains,
            domains,
            date_from or _DATE_MIN,
            date_to or _DATE_MAX,
        )
        async with self._pool.connection() as conn:
            row = await (await conn.execute(_COUNT, params)).fetchone()
        return int(row["n"]) if row is not None else 0

    async def list_documents(
        self,
        *,
        category: Category,
        date_from: date,
        date_to: date,
        undated_since: date | None = None,
        limit: int = 50,
    ) -> list[Document]:
        params = (
            category,
            date_from,
            date_to,
            undated_since or _DATE_MIN,
            limit,
        )
        async with self._pool.connection() as conn:
            rows = await (await conn.execute(_LIST, params)).fetchall()
        return [Document.model_validate(row) for row in rows]

    async def get_existing(
        self, source_id: UUID, external_ids: list[str]
    ) -> dict[str, Document]:
        if not external_ids:
            return {}
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(_GET_EXISTING, (source_id, external_ids))
            ).fetchall()
        return {row["external_id"]: Document.model_validate(row) for row in rows}

    async def dismiss(self, document_id: UUID) -> bool:
        async with self._pool.connection() as conn:
            cur = await conn.execute(_DISMISS, (document_id,))
        return cur.rowcount > 0

    async def next_event_date(self, *, category: Category, after: date) -> date | None:
        async with self._pool.connection() as conn:
            row = await (await conn.execute(_NEXT_EVENT_DATE, (category, after))).fetchone()
        return row["next_date"] if row is not None else None
