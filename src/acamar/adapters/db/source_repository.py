"""`SourceRepository` backed by Postgres."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg_pool import AsyncConnectionPool

from acamar.domain.enums import SourceStatus
from acamar.domain.models import Source
from acamar.ports.repositories import SourceNotFoundError

_Pool = AsyncConnectionPool[psycopg.AsyncConnection[dict[str, Any]]]

_SELECT = """
    SELECT id, school_id, domain, category, config, last_run_at, last_status
    FROM sources WHERE id = %s
"""

_SELECT_ALL = """
    SELECT id, school_id, domain, category, config, last_run_at, last_status
    FROM sources ORDER BY domain, category
"""


class PostgresSourceRepository:
    def __init__(self, pool: _Pool) -> None:
        self._pool = pool

    async def get(self, source_id: UUID) -> Source:
        async with self._pool.connection() as conn:
            row = await (await conn.execute(_SELECT, (source_id,))).fetchone()
        if row is None:
            raise SourceNotFoundError(str(source_id))
        return Source.model_validate(row)

    async def mark_run(
        self, source_id: UUID, status: SourceStatus, *, at: datetime
    ) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE sources SET last_run_at = %s, last_status = %s WHERE id = %s",
                (at, status, source_id),
            )

    async def list_all(self) -> list[Source]:
        async with self._pool.connection() as conn:
            rows = await (await conn.execute(_SELECT_ALL)).fetchall()
        return [Source.model_validate(row) for row in rows]
