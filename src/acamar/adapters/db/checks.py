"""Cheap startup assertions about the live database."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg_pool import AsyncConnectionPool

_Pool = AsyncConnectionPool[psycopg.AsyncConnection[dict[str, Any]]]

_DIM_QUERY = """
    SELECT atttypmod AS dim
    FROM pg_attribute
    WHERE attrelid = 'documents'::regclass AND attname = 'embedding'
"""


class SchemaMismatchError(RuntimeError):
    """The live schema disagrees with the app's configuration."""


async def assert_embedding_dim(pool: _Pool, expected: int) -> None:
    """Fail fast if ``documents.embedding`` isn't ``vector(expected)``.

    For pgvector, ``atttypmod`` is the declared dimension as-is.
    """
    async with pool.connection() as conn:
        row = await (await conn.execute(_DIM_QUERY)).fetchone()
    actual = None if row is None else row["dim"]
    if actual != expected:
        raise SchemaMismatchError(
            f"documents.embedding is vector({actual}) but EMBEDDING_DIM={expected} "
            f"— re-apply db/schema.sql or fix the config"
        )
