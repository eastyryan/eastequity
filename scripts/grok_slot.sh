#!/bin/bash
# Fire one East Equity slot as a local Grok CLI agent (OAuth session, not the API).
#
# OPTIONAL local outer-agent. The scheduled trader is GitHub Actions
# (scripts/cloud_slot.sh + .github/workflows/grok-cycle.yml). Do not load
# launchd for this — a sleeping Mac is the point, and two clocks double-trade.
#
#   scripts/grok_slot.sh              # infer role from ET clock
#   scripts/grok_slot.sh brain        # force the market-cycle prompt
#   scripts/grok_slot.sh watchdog     # missed-slot + trigger pass
#
# Slot gate stays hardcoded on purpose (same reason as run_cycle.sh): it answers
# "should this launchd tick do anything", which is a property of the plist.
# KEEP IN SYNC with runlib.analytics.expected_slots() /
# autonomy_config.json schedule.slot_depths / tests/test_schedule_sources_agree.py.
set -uo pipefail

export PATH="/Users/eastonryan/.grok/bin:/Users/eastonryan/.local/bin:/Users/eastonryan/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"
export TZ="America/New_York"

ROOT="/Users/eastonryan/east-equity-agent"
cd "$ROOT" || exit 1
mkdir -p logs state

GROK="${GROK_BIN:-/Users/eastonryan/.grok/bin/grok}"
PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  PY="$ROOT/.venv311/bin/python"
fi

DOW=$(date +%u)    # 1=Mon ... 6=Sat 7=Sun
HHMM=$(date +%H%M)
ROLE="${1:-auto}"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S %Z') [$ROLE] $*" | tee -a logs/grok_cron.log; }

if [ -f state/KILL_SWITCH ]; then
  log "KILL_SWITCH present — standing down"
  exit 0
fi

if [ ! -x "$GROK" ]; then
  log "FATAL: grok CLI not found at $GROK"
  exit 1
fi

# Infer role from the ET clock when launchd passes nothing (or "auto").
if [ "$ROLE" = "auto" ]; then
  if [ "$DOW" -eq 6 ] || [ "$DOW" -eq 7 ]; then
    case "$HHMM" in
      0000) ROLE="midnight" ;;
      2359|2358|0001) ROLE="weekend" ;;
      *) log "weekend $HHMM — not a weekend slot, standing down"; exit 0 ;;
    esac
  elif [ "$DOW" -le 5 ]; then
    case "$HHMM" in
      0600|0559|0601) ROLE="brain" ;;
      0845|0844|0846) ROLE="brain" ;;
      1030|1029|1031) ROLE="brain" ;;
      1200|1159|1201) ROLE="brain" ;;
      1400|1359|1401) ROLE="brain" ;;
      1530|1529|1531) ROLE="brain" ;;
      1730|1729|1731) ROLE="evening" ;;
      1900|1859|1901) ROLE="study" ;;
      0000|2359)      ROLE="midnight" ;;
      *)
        # Watchdog ticks land at :15; anything else is a stray launchd fire.
        if [ "${HHMM:2:2}" = "15" ] && [ "$HHMM" \> "0714" ] && [ "$HHMM" \< "1916" ]; then
          ROLE="watchdog"
        else
          log "weekday $HHMM — not a scheduled slot, standing down"
          exit 0
        fi
        ;;
    esac
  else
    log "unknown DOW=$DOW, standing down"
    exit 0
  fi
fi

# Watchdog is weekday 07:15–19:15 ET even when launchd passes the role explicitly.
if [ "$ROLE" = "watchdog" ]; then
  if [ "$DOW" -ge 6 ] || [ "$HHMM" \< "0715" ] || [ "$HHMM" \> "1915" ]; then
    log "watchdog outside weekday 07:15–19:15 ET (DOW=$DOW HHMM=$HHMM) — standing down"
    exit 0
  fi
fi

case "$ROLE" in
  brain)    PROMPT="scripts/prompts/scheduled_brain.md"; EFFORT="high" ;;
  evening)  PROMPT="scripts/prompts/evening_review.md";  EFFORT="high" ;;
  study)    PROMPT="scripts/prompts/daily_study.md";     EFFORT="high" ;;
  midnight) PROMPT="scripts/prompts/midnight.md";        EFFORT="high" ;;
  weekend)  PROMPT="scripts/prompts/weekend_news.md";    EFFORT="high" ;;
  watchdog) PROMPT="scripts/prompts/watchdog.md";        EFFORT="medium" ;;
  *) log "unknown role '$ROLE'"; exit 1 ;;
esac

if [ ! -f "$PROMPT" ]; then
  log "FATAL: prompt missing: $PROMPT"
  exit 1
fi

# One Grok slot at a time. mkdir is atomic; macOS has no /usr/bin/flock.
# A full run is 18–20 min (p90 ~25). A lockdir older than 45 min is stale
# leftover from a killed launchd job and is stolen so a dead lock cannot
# eat the rest of the day.
LOCKDIR="state/GROK_SLOT.lockdir"
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  if [ -d "$LOCKDIR" ]; then
    AGE=$(( $(date +%s) - $(stat -f %m "$LOCKDIR" 2>/dev/null || echo 0) ))
    if [ "$AGE" -gt 2700 ]; then
      log "stale GROK_SLOT.lockdir (${AGE}s) — stealing"
      rmdir "$LOCKDIR" 2>/dev/null || rm -rf "$LOCKDIR"
      if ! mkdir "$LOCKDIR" 2>/dev/null; then
        log "could not steal lock — standing down"
        exit 0
      fi
    else
      log "another grok slot holds GROK_SLOT.lockdir (${AGE}s old) — standing down"
      exit 0
    fi
  else
    log "lock mkdir failed — standing down"
    exit 0
  fi
fi
echo $$ >"$LOCKDIR/pid"
trap 'rm -rf "$LOCKDIR"' EXIT

# Fresh main before the brain reasons. Fail-open: a git error must not brick the slot.
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git fetch origin --quiet || log "git fetch failed (continuing)"
  if git merge-base --is-ancestor HEAD origin/main 2>/dev/null; then
    if ! git merge-base --is-ancestor origin/main HEAD 2>/dev/null; then
      git pull --ff-only origin main >>logs/grok_cron.log 2>&1 || log "ff-only pull failed (continuing)"
    fi
  fi
fi

LOG="logs/grok_${ROLE}_$(date +%Y%m%d-%H%M%S).log"
log "starting $ROLE → $LOG (grok 4.6, effort $EFFORT)"

# Session auth via ~/.grok/auth.json — no XAI_API_KEY. --yolo so launchd is
# unattended. --no-auto-update so a CLI bump cannot stall a slot.
set +e
"$GROK" --cwd "$ROOT" --yolo --no-auto-update \
  -m grok-4.6 --effort "$EFFORT" \
  --prompt-file "$PROMPT" \
  >>"$LOG" 2>&1
RC=$?
set -e

log "finished $ROLE exit=$RC (log $LOG)"
exit "$RC"
