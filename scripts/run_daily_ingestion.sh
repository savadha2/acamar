#!/bin/bash
# Runs every active source's ingestion sequentially — the daily launchd job's
# entry point (com.acamar.ingest-faria-events.plist). One process per source
# via scripts.run_ingestion, not a single Python script, so a broken source
# can't take the others down with it (each failure is caught + reported,
# `$status` still reflects whether *any* source failed for launchd's exit code).
set -uo pipefail
cd "$(dirname "$0")/.."

PYTHON=.venv/bin/python
SOURCES=(
    fe2de7f1-2b65-4a22-a4be-60aef0b4dd57  # school/event — Faria calendar scrape
    195af33e-563b-4a35-9b04-0872a46d826e  # gmail/event  — school emails (events + reminders)
)

status=0
for source in "${SOURCES[@]}"; do
    echo "=== ingesting $source ($(date)) ==="
    "$PYTHON" -m scripts.run_ingestion --source "$source" || status=1
done
exit "$status"
