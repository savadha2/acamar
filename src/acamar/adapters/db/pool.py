"""Async connection pool, with pgvector and dict rows configured on every conn."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import psycopg
from pgvector.psycopg import register_vector_async
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from acamar.config import get_settings


async def _register_vector(conn: psycopg.AsyncConnection[Any]) -> None:
    await register_vector_async(conn)


@asynccontextmanager
async def connection_pool(
    conninfo: str | None = None,
) -> AsyncIterator[AsyncConnectionPool[psycopg.AsyncConnection[dict[str, Any]]]]:
    """Open a pool for the duration of the context, then close it."""
    pool: AsyncConnectionPool[psycopg.AsyncConnection[dict[str, Any]]] = AsyncConnectionPool(
        conninfo or get_settings().database_url,
        open=False,
        configure=_register_vector,
        kwargs={"row_factory": dict_row},
        min_size=1,
        max_size=4,
    )
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()
