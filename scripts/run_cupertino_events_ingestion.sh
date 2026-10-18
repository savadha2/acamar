#!/bin/bash
# Runs the cupertino_events source once — a separate launchd job
# (com.acamar.ingest-cupertino-events.plist) from the main 6x/day one
# (run_daily_ingestion.sh), deliberately: city events change far less often
# than the school calendar/email sources, and a single run here is already a
# multi-minute, ~30-request sequential scrape (politeness throttle, not a
# bug — see sources/cupertino_events/connector.py's own docstring). Running
# it 6x/day would mostly repeat an unchanged crawl for no benefit.
set -uo pipefail
cd "$(dirname "$0")/.."

PYTHON=.venv/bin/python
SOURCE=23cae8fb-4b36-4ab5-b39e-a0ffb82ef153  # city/event — cupertino_events

"$PYTHON" -m scripts.run_ingestion --source "$SOURCE"
