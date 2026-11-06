#!/usr/bin/env bash
# Weekly production cycle for a Linux VPS: scrape -> R2 -> verify -> report.
#
# Mirrors cloudflare/weekly_scrape.ps1. Every step is idempotent, so a killed
# cycle simply resumes on the next run.
#
# Install (runs every Monday 16:00):
#   chmod +x cloudflare/weekly_scrape.sh
#   crontab -e
#   0 16 * * 1  cd /opt/new-scrapper && ./cloudflare/weekly_scrape.sh >> /var/log/gujrera.log 2>&1
#
# Manual run:
#   MAX_PROJECTS=300 ./cloudflare/weekly_scrape.sh
#   SKIP_SCRAPE=1 ./cloudflare/weekly_scrape.sh      # push + verify only
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

MAX_PROJECTS="${MAX_PROJECTS:-1000}"
SKIP_SCRAPE="${SKIP_SCRAPE:-0}"
PY="${PY:-python3}"

LOG_DIR="$REPO/_work/logs"
LOCK="$REPO/_work/scrape.lock"
HISTORY="$LOG_DIR/history.csv"
mkdir -p "$LOG_DIR" "$(dirname "$LOCK")"

STAMP="$(date +%Y%m%d-%H%M%S)"
LOG="$LOG_DIR/cycle-$STAMP.log"

log() { echo "$(date +%H:%M:%S)  $*" | tee -a "$LOG"; }

scrape_running() {
    pgrep -af 'run\.py' >/dev/null 2>&1
}

# ---- lock ------------------------------------------------------------------
# Also wait when a scrape is running without holding the lock (manual run).
if [ -f "$LOCK" ] || scrape_running; then
    log "another scrape is active (lock: $([ -f "$LOCK" ] && cat "$LOCK" || echo none)) - waiting up to 120 min"
    for _ in $(seq 1 120); do
        scrape_running || break
        sleep 60
    done
    if scrape_running; then
        log "still running - skipping this cycle"
        exit 0
    fi
    rm -f "$LOCK"
fi
echo "pid=$$ started=$(date -Is)" > "$LOCK"
trap 'rm -f "$LOCK"' EXIT

START_TS=$(date +%s)
log "=== cycle start ==="

# ---- targets ---------------------------------------------------------------
log "refreshing target list"
"$PY" build_targets.py >>"$LOG" 2>&1
BACKLOG=$("$PY" - <<'PY'
import json
from pathlib import Path
print(len(json.loads(Path("output/targets_missing.json").read_text(encoding="utf-8"))))
PY
)
log "projects still to scrape: $BACKLOG"

# ---- scrape ----------------------------------------------------------------
if [ "$SKIP_SCRAPE" != "1" ] && [ "$BACKLOG" -gt 0 ]; then
    log "scraping up to $MAX_PROJECTS project(s), direct to R2"
    "$PY" -u run.py --scrape --targets output/targets_missing.json \
        --limit "$MAX_PROJECTS" --direct-r2 >>"$LOG" 2>&1
    log "scrape exited $?"
else
    log "nothing new to scrape"
fi

# ---- sync + prune ----------------------------------------------------------
log "syncing to R2 and pruning local documents"
"$PY" -u cloudflare/r2_sync.py --workers 16 --prune >>"$LOG" 2>&1

# ---- verify ----------------------------------------------------------------
log "verifying every object against disk"
"$PY" -u cloudflare/r2_verify.py --all --workers 48 \
    --report cloudflare/verify_full.json >>"$LOG" 2>&1
VERIFY_OK=$?
if [ "$VERIFY_OK" -eq 0 ]; then log "verify: PASS"; else log "verify: PROBLEMS FOUND"; fi

# ---- broken register -------------------------------------------------------
log "regenerating BROKEN_DOCUMENTS"
"$PY" make_broken_report.py >>"$LOG" 2>&1

# ---- history ---------------------------------------------------------------
ELAPSED=$(( ($(date +%s) - START_TS) / 60 ))
[ -f "$HISTORY" ] || echo "started,backlog,cap,verify_passed,minutes" > "$HISTORY"
echo "$(date -Is),$BACKLOG,$MAX_PROJECTS,$([ "$VERIFY_OK" -eq 0 ] && echo true || echo false),$ELAPSED" >> "$HISTORY"
log "=== cycle finished in ${ELAPSED} min (log: $LOG) ==="

[ "$VERIFY_OK" -eq 0 ] || exit 1
