"""Seed the database with the Faria school and its events source.

Idempotent — safe to re-run. Prints the ids so they can be passed to
``scripts.run_ingestion``.

    uv run python -m scripts.seed
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Json

from acamar.domain.enums import Category, Domain
from scripts._db import connect, scalar

SCHOOL_NAME = "Faria Elementary School"
SOURCE_CONFIG = {
    "schedule": "weekly:sat@06:00",
    "calendar_url": "https://faria.cusdk8.org/about-us/school-calendar",
}
MANUAL_MENU_CONFIG = {
    # A monthly upload is a partial view of what's still valid (last month's
    # menu is still a legitimate document) — reconciling would wrongly
    # soft-delete every month but the one just uploaded. See
    # IngestionService._should_reconcile.
    "reconcile": False,
}
CUPERTINO_EVENTS_CONFIG: dict[str, Any] = {
    # `reconcile` defaults True (correct here — this connector re-lists
    # every currently-linked event each run, same full-reassertion shape
    # as Faria's own month scrape). `reconcile_grace_hours` is NOT the same
    # thing: real bug found by review (2026-10-02) — this connector's own
    # per-event fault isolation means a transient failure on any ONE event
    # (timeout, a flaky retry, a robots.txt/WAF hiccup) makes fetch()
    # legitimately return a partial set that run, and with the once-daily
    # cron, reconciling on "not seen *this* run" alone wrongly soft-deletes
    # a real, still-upcoming event for up to a day. 48h (~2 missed runs)
    # tolerates one bad run for free while a genuinely removed event still
    # disappears within two days — see IngestionService._reconcile_grace.
    "reconcile_grace_hours": 48,
}
# `calendar_url` is only set in config to override the settings default, not
# needed normally.


def seed() -> None:
    with connect() as conn:
        school_id = _upsert_school(conn, SCHOOL_NAME)
        events_source_id = _upsert_events_source(conn, school_id)
        menu_source_id = _upsert_manual_menu_source(conn, school_id)
        cupertino_events_source_id = _upsert_cupertino_events_source(conn, school_id)

    print(f"school  {school_id}  {SCHOOL_NAME}")
    print(f"source  {events_source_id}  faria_events       ({Domain.SCHOOL}/{Category.EVENT})")
    print(f"source  {menu_source_id}  manual_menu        ({Domain.SCHOOL}/{Category.MENU})")
    print(
        f"source  {cupertino_events_source_id}  cupertino_events   "
        f"({Domain.CITY}/{Category.EVENT})"
    )
    print(f"\nnext:  uv run python -m scripts.run_ingestion --source {events_source_id}")
    print(f"set in .env:  MANUAL_MENU_SOURCE_ID={menu_source_id}")


def _upsert_school(conn: psycopg.Connection[Any], name: str) -> Any:
    existing = conn.execute("SELECT id FROM schools WHERE name = %s", (name,)).fetchone()
    if existing is not None:
        return existing[0]
    return scalar(
        conn.execute("INSERT INTO schools (name) VALUES (%s) RETURNING id", (name,))
    )


def _upsert_events_source(conn: psycopg.Connection[Any], school_id: Any) -> Any:
    existing = conn.execute(
        "SELECT id FROM sources WHERE school_id = %s AND domain = %s AND category = %s",
        (school_id, Domain.SCHOOL, Category.EVENT),
    ).fetchone()
    if existing is not None:
        conn.execute(
            "UPDATE sources SET config = %s WHERE id = %s",
            (Json(SOURCE_CONFIG), existing[0]),
        )
        return existing[0]
    return scalar(
        conn.execute(
            "INSERT INTO sources (school_id, domain, category, config) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (school_id, Domain.SCHOOL, Category.EVENT, Json(SOURCE_CONFIG)),
        )
    )


def _upsert_manual_menu_source(conn: psycopg.Connection[Any], school_id: Any) -> Any:
    existing = conn.execute(
        "SELECT id FROM sources WHERE school_id = %s AND domain = %s AND category = %s",
        (school_id, Domain.SCHOOL, Category.MENU),
    ).fetchone()
    if existing is not None:
        conn.execute(
            "UPDATE sources SET config = %s WHERE id = %s",
            (Json(MANUAL_MENU_CONFIG), existing[0]),
        )
        return existing[0]
    return scalar(
        conn.execute(
            "INSERT INTO sources (school_id, domain, category, config) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (school_id, Domain.SCHOOL, Category.MENU, Json(MANUAL_MENU_CONFIG)),
        )
    )


def _upsert_cupertino_events_source(conn: psycopg.Connection[Any], school_id: Any) -> Any:
    existing = conn.execute(
        "SELECT id FROM sources WHERE school_id = %s AND domain = %s AND category = %s",
        (school_id, Domain.CITY, Category.EVENT),
    ).fetchone()
    if existing is not None:
        conn.execute(
            "UPDATE sources SET config = %s WHERE id = %s",
            (Json(CUPERTINO_EVENTS_CONFIG), existing[0]),
        )
        return existing[0]
    return scalar(
        conn.execute(
            "INSERT INTO sources (school_id, domain, category, config) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (school_id, Domain.CITY, Category.EVENT, Json(CUPERTINO_EVENTS_CONFIG)),
        )
    )


if __name__ == "__main__":
    seed()
