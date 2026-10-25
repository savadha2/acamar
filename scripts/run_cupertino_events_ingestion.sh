#!/bin/bash
# Cupertino Parks & Rec events — runs from this Mac, writing to BOTH the local
# Postgres and GCP's Cloud SQL (2026-10-25).
#
# Why the Mac and not a Cloud Run Job: cupertino.gov's Akamai WAF 403s Google
# Cloud egress outright (every URL, even robots.txt — see CLAUDE.md). The
# cloud `cupertino-events` job/trigger stay in place but PAUSED. Latency and
# a hard dependency on this Mac are accepted for this one source.
#
# Catch-up, not a fixed slot: launchd (com.acamar.ingest-cupertino-events.plist)
# fires this every 4 hours (03/07/11/15/19/23:00), at login (RunAtLoad), and
# right after wake if a firing was missed while asleep. Each firing is cheap: a
# target only actually runs if it hasn't SUCCEEDED since the most recent
# 07:00, so a sleeping, shut-down, or offline-at-07:00 Mac catches up at the
# next firing, and a failed run (e.g. Wi-Fi not back yet right at wake) is
# retried at the next 4-hourly check instead of waiting a whole day.
#
# The "last success" marker is a local file, deliberately not
# sources.last_run_at — a FAILED run updates last_run_at too, so reading it
# would wrongly skip the retry.
set -uo pipefail
cd "$(dirname "$0")/.."

# launchd's PATH is minimal (/usr/bin:/bin:/usr/sbin:/sbin).
export PATH="$HOME/.local/bin:$HOME/google-cloud-sdk/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"

PYTHON=.venv/bin/python
LOCAL_SOURCE=23cae8fb-4b36-4ab5-b39e-a0ffb82ef153   # city/event in local Postgres
CLOUD_SOURCE=43eea19a-35bc-4ec8-918f-c4d8fb6ae9a6   # city/event in Cloud SQL
CLOUD_SQL_INSTANCE=acamar-508716:us-central1:acamar-db
PROXY_PORT=5434
DAILY_SLOT="07:00"
STATE_DIR=logs

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# Epoch seconds of the most recent $DAILY_SLOT (today's if already past, else yesterday's).
last_slot_epoch() {
    local today slot
    today=$(date +%Y-%m-%d)
    # Seconds spelled out on purpose: BSD `date -j -f` fills any field the
    # format omits from the CURRENT time, so "%H:%M" alone gave 07:00:<now's
    # seconds> — making a run at 07:00:12 look stale to a check at hh:00:46.
    slot=$(date -j -f "%Y-%m-%d %H:%M:%S" "$today $DAILY_SLOT:00" +%s)
    if [ "$(date +%s)" -lt "$slot" ]; then
        slot=$((slot - 86400))
    fi
    echo "$slot"
}

is_due() {  # $1 = target name
    local marker="$STATE_DIR/cupertino-events.$1.last-success"
    [ ! -f "$marker" ] || [ "$(stat -f %m "$marker")" -lt "$(last_slot_epoch)" ]
}

mark_success() { touch "$STATE_DIR/cupertino-events.$1.last-success"; }

# Right after wake, Wi-Fi can take a while — wait up to ~2 min for a route,
# without spending a request on the site itself.
wait_for_network() {
    local i
    for i in $(seq 1 24); do
        if scutil -r www.cupertino.gov | grep -q '^Reachable'; then
            return 0
        fi
        sleep 5
    done
    return 1
}

run_local() {
    "$PYTHON" -m scripts.run_ingestion --source "$LOCAL_SOURCE"
}

run_cloud() {
    local proxy_log="$STATE_DIR/cloud-sql-proxy.cupertino.log" proxy_pid password status i
    cloud-sql-proxy --port "$PROXY_PORT" "$CLOUD_SQL_INSTANCE" >"$proxy_log" 2>&1 &
    proxy_pid=$!
    for i in $(seq 1 30); do
        grep -q "ready for new connections" "$proxy_log" && break
        sleep 1
    done
    if ! grep -q "ready for new connections" "$proxy_log"; then
        log "cloud: cloud-sql-proxy never became ready — see $proxy_log"
        kill "$proxy_pid" 2>/dev/null
        return 1
    fi

    # Fetched per run, URL-encoded, never written to disk.
    if ! password=$(gcloud secrets versions access latest --secret=db-password 2>/dev/null); then
        log "cloud: couldn't read the db-password secret (gcloud auth expired?)"
        kill "$proxy_pid" 2>/dev/null
        return 1
    fi
    password=$(printf '%s' "$password" | "$PYTHON" -c \
        'import sys, urllib.parse; print(urllib.parse.quote(sys.stdin.read(), safe=""))')

    DATABASE_URL="postgresql://acamar:${password}@127.0.0.1:${PROXY_PORT}/acamar" \
        "$PYTHON" -m scripts.run_ingestion --source "$CLOUD_SOURCE"
    status=$?
    kill "$proxy_pid" 2>/dev/null
    wait "$proxy_pid" 2>/dev/null
    return "$status"
}

overall=0
for target in local cloud; do
    if ! is_due "$target"; then
        continue  # already succeeded since the last 07:00 — the common, silent case
    fi
    if ! wait_for_network; then
        log "$target: no network after ~2 min — will retry at the next check"
        overall=1
        break
    fi
    log "$target: running cupertino_events ingestion"
    if "run_$target"; then
        mark_success "$target"
        log "$target: ok"
    else
        log "$target: FAILED — will retry at the next check"
        overall=1
    fi
done
exit "$overall"
