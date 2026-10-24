"""Apply pending SQL migrations under db/migrations/, in filename order.

Minimum-viable migration tooling (docs/gcp-aws-deployment-proposal.md §8) —
numbered SQL files plus this runner, not Alembic. Applied migrations are
recorded one-row-per-file in a `schema_migrations` table (created on first
run), so a migration never re-runs against a database that already has it.
Idempotent — safe to re-run; each pending file applies in its own
transaction, so a failure partway through a file rolls that file back
without touching ones already recorded.

Add a schema change as a new file (``0002_*.sql``, ``0003_*.sql``, ...) —
never edit a migration that may already be applied somewhere (see
0001_initial.sql's own header).

    uv run python -m scripts.migrate
"""

from __future__ import annotations

from pathlib import Path

import psycopg

from acamar.config import get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"

_CREATE_TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def main() -> None:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        print(f"no migration files found under {MIGRATIONS_DIR}")
        return

    # Not autocommit (unlike scripts._db.connect()) — each migration applies
    # and records itself in one transaction, deliberately.
    conn = psycopg.connect(get_settings().database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(_CREATE_TRACKING_TABLE)
        conn.commit()

        with conn.cursor() as cur:
            cur.execute("SELECT filename FROM schema_migrations")
            already_applied = {row[0] for row in cur.fetchall()}

        pending = [f for f in files if f.name not in already_applied]
        if not pending:
            print(f"up to date — {len(files)} migration(s) already applied, 0 pending")
            return

        for path in pending:
            print(f"applying {path.name} ...", end=" ", flush=True)
            try:
                with conn.cursor() as cur:
                    cur.execute(path.read_text())
                    cur.execute(
                        "INSERT INTO schema_migrations (filename) VALUES (%s)",
                        (path.name,),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                print("FAILED")
                raise
            print("done")

        skipped = len(files) - len(pending)
        print(f"applied {len(pending)} migration(s), {skipped} already up to date")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
