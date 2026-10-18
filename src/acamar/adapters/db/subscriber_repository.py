"""`SubscriberRepository` backed by Postgres."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg_pool import AsyncConnectionPool

from acamar.domain.models import Subscriber

_Pool = AsyncConnectionPool[psycopg.AsyncConnection[dict[str, Any]]]

_ACTIVE_BY_HASH = """
    SELECT id, label, created_at
    FROM subscribers
    WHERE token_hash = %s AND revoked_at IS NULL
"""


class PostgresSubscriberRepository:
    def __init__(self, pool: _Pool) -> None:
        self._pool = pool

    async def active_by_token_hash(self, token_hash: str) -> Subscriber | None:
        async with self._pool.connection() as conn:
            row = await (await conn.execute(_ACTIVE_BY_HASH, (token_hash,))).fetchone()
        return None if row is None else Subscriber.model_validate(row)
