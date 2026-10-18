"""Shared DB helpers for the operational scripts."""

from __future__ import annotations

from typing import Any

import psycopg

from acamar.config import get_settings


def connect() -> psycopg.Connection[tuple[Any, ...]]:
    """Open an autocommit connection to the configured database."""
    return psycopg.connect(get_settings().database_url, autocommit=True)


def scalar(cursor: psycopg.Cursor[Any]) -> Any:
    """First column of the single expected row; raise if the query returned none."""
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError("query returned no row")
    return row[0]
