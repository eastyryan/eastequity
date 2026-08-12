#!/bin/bash
# Scheduled Grok trader for GitHub Actions. Runs with the Mac off.
#
# launchd / this Mac are NOT the clock. This script is invoked by
# .github/workflows/grok-cycle.yml on a UTC cron that covers both EST and EDT;
# we self-gate on America/New_York so a DST-duplicate fire is a 5-second no-op.
#
#   scripts/cloud_slot.sh            # infer role from ET clock
#   scripts/cloud_slot.sh watchdog
#   scripts/cloud_slot.sh brain
set -uo pipefail

export TZ="America/New_York"
export EE_SCHEDULED_TRADER=1

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1
mkdir -p logs state

PY="${PYTHON:-python3}"
DOW=$(date +%u)    # 1=Mon ... 7=Sun
HHMM=$(date +%H%M)
ROLE="${1:-auto}"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S %Z') [$ROLE] $*"; }

if [ -f state/KILL_SWITCH ]; then
  log "KILL_SWITCH present — standing down"
  exit 0
fi

# Minutes since midnight, decimal, no octal surprises.
mins_now=$((10#${HHMM:0:2} * 60 + 10#${HHMM:2:2}))

in_window() {
  # $1 = HHMM slot. True if now is in [slot, slot+WINDOW) ET.
  local slot="$1"
  local window="${2:-20}"
  local sm=$((10#${slot:0:2} * 60 + 10#${slot:2:2}))
  local delta=$((mins_now - sm))
  [ "$delta" -ge 0 ] && [ "$delta" -lt "$window" ]
}

if [ "$ROLE" = "auto" ]; then
  ROLE=""
  if [ "$DOW" -eq 6 ] || [ "$DOW" -eq 7 ]; then
    if in_window 0000 25; then
      ROLE="midnight"
    elif in_window 2359 20 || { [ "$HHMM" = "0000" ] && [ "$DOW" -eq 7 ]; }; then
      ROLE="weekend"
    fi
  elif [ "$DOW" -le 5 ]; then
    if in_window 0600 20; then ROLE="brain"
    elif in_window 0845 20; then ROLE="brain"
    elif in_window 1030 20; then ROLE="brain"
    elif in_window 1200 20; then ROLE="brain"
    elif in_window 1400 20; then ROLE="brain"
    elif in_window 1530 20; then ROLE="brain"
    elif in_window 1730 20; then ROLE="evening"
    elif in_window 1900 20; then ROLE="study"
    elif in_window 0000 25; then ROLE="midnight"
    elif [ "${HHMM:2:2}" = "15" ] && [ "$HHMM" \> "0714" ] && [ "$HHMM" \< "1916" ]; then
      ROLE="watchdog"
    fi
  fi
  if [ -z "$ROLE" ]; then
    log "ET $HHMM DOW=$DOW — not inside a slot window, standing down"
    exit 0
  fi
fi

if [ "$ROLE" = "watchdog" ]; then
  if [ "$DOW" -ge 6 ] || [ "$HHMM" \< "0715" ] || [ "$HHMM" \> "1915" ]; then
    log "watchdog outside weekday 07:15–19:15 ET — standing down"
    exit 0
  fi
fi

log "running role=$ROLE"

run_py() {
  log "\$ $PY $*"
  "$PY" -u -W ignore "$@"
}

case "$ROLE" in
  brain)
    run_py scripts/mark_run_start.py || true
    run_py orchestrator.py --auto-depth
    ;;
  evening)
    run_py scripts/mark_run_start.py || true
    run_py orchestrator.py --auto-depth
    if [ "$DOW" = "5" ]; then
      log "Friday — chaining weekly self-review"
      run_py orchestrator.py --self-review || true
    fi
    ;;
  study)
    if [ "$DOW" -ge 6 ]; then
      log "study: weekend, standing down"
      exit 0
    fi
    run_py orchestrator.py --study
    ;;
  midnight)
    run_py scripts/mark_run_start.py || true
    if [ "$DOW" -eq 7 ]; then
      run_py orchestrator.py --weekly-market
    else
      run_py orchestrator.py --news-only
    fi
    ;;
  weekend)
    if [ "$DOW" -le 5 ]; then
      log "weekend news: weekday, standing down"
      exit 0
    fi
    run_py scripts/mark_run_start.py || true
    run_py orchestrator.py --news-only
    if [ "$DOW" -eq 7 ]; then
      log "Sunday — chaining universe review"
      run_py orchestrator.py --universe-review || true
    fi
    ;;
  watchdog)
    # Always pull first: breadcrumbs live on origin/main.
    git fetch origin --quiet || true
    git reset --hard origin/main --quiet || true
    DETECT=$("$PY" scripts/find_missed_slot.py)
    log "detector: $DETECT"
    SLOT=$("$PY" -c "import json,sys; print(json.loads(sys.argv[1]).get('slot') or '')" "$DETECT")
    if [ -n "$SLOT" ]; then
      DEPTH=$("$PY" -c "import json,sys; print(json.loads(sys.argv[1]).get('depth') or 'holdings_watchlist')" "$DETECT")
      export EE_RECOVERY_SLOT="$SLOT"
      log "MODE=recover slot=$SLOT depth=$DEPTH"
      run_py scripts/mark_run_start.py --slot "$SLOT" --stage recovery || true
      run_py orchestrator.py --depth "$DEPTH"
    elif [ -f state/trigger_pending.json ]; then
      TICKERS=$("$PY" - <<'PY'
import json, sys
from datetime import datetime, timezone
from pathlib import Path
p = Path("state/trigger_pending.json")
try:
    d = json.loads(p.read_text())
except Exception:
    sys.exit(0)
confirmed = d.get("confirmed") or []
ts = d.get("ts") or ""
if not confirmed:
    sys.exit(0)
try:
    when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    age = (datetime.now(timezone.utc) - when).total_seconds()
except Exception:
    sys.exit(0)
if age > 90 * 60:
    sys.exit(0)
print(",".join(str(t) for t in confirmed))
PY
)
      if [ -n "${TICKERS:-}" ]; then
        if [ "$HHMM" \< "0935" ] || [ "$HHMM" \> "1545" ]; then
          log "trigger $TICKERS outside 09:35–15:45 — leaving pending"
          exit 0
        fi
        log "MODE=trigger tickers=$TICKERS"
        run_py orchestrator.py --depth holdings_watchlist --trigger-run "$TICKERS"
        rm -f state/trigger_pending.json
      else
        log "watchdog: no recoverable missed slot, no pending trigger"
      fi
    else
      log "watchdog: no recoverable missed slot, no pending trigger"
    fi
    ;;
  *)
    log "unknown role '$ROLE'"
    exit 1
    ;;
esac

log "finished role=$ROLE"
