#!/bin/bash
# East Equity Agent scheduled cycle. Invoked by launchd every hour at :00, :30, :59
# with --scheduled; the time-slot gate below decides which ticks actually run.
# Direct invocation (no --scheduled) always runs, e.g. `run_cycle.sh --self-review`.
#
# DOUBLE-TRADE NOTE: this is the LOCAL trader. The orchestrator's RUN_LOCK
# (state/RUN_LOCK) is a LOCAL file lock; cross-node coordination with the
# scheduled CLOUD trader is handled by the git-arbitrated lease in preflight
# (acquire_cross_node_lease / state/RUN_LEASE.json — scheduled runs stand down
# when another node holds an unexpired lease; fail-open on git errors). The
# kill switch is honored (preflight aborts on state/KILL_SWITCH). Do not add a
# second orchestrator invocation to this script.
export PATH="/Users/eastonryan/.local/bin:/Users/eastonryan/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"
cd /Users/eastonryan/east-equity-agent
mkdir -p logs

DOW=$(date +%u)    # 1=Mon ... 6=Sat 7=Sun
HHMM=$(date +%H%M)

if [ "$1" = "--scheduled" ]; then
  shift
  if [ "$DOW" -le 5 ]; then
    # Weekdays: SEVEN slots (user policy 2026-07-13) - 6am, 9am, 10am, 12pm,
    # 2pm, 4pm, 5:30pm. Overnight/weekend news is covered by the cloud routines.
    # KEEP IN SYNC with expected_slots() in orchestrator.py (runs heartbeat).
    case "$HHMM" in
      0600|0900|1000|1200|1400|1600|1730) ;;
      *) exit 0 ;;
    esac
  else
    # Weekends: midnight + 11:59pm news checks only
    case "$HHMM" in
      0000|2359) ;;
      *) exit 0 ;;
    esac
  fi
fi

if [ "$DOW" -ge 6 ]; then
  .venv/bin/python -W ignore orchestrator.py --news-only "$@" >> logs/cron.log 2>&1
  exit $?
fi

# Slot-aware depth: 5:30pm is a FULL-RESEARCH review (all names, deep research)
# with NO trading; market-day slots incl. 6am/9am pre-market are full cycles
# (pre-market data matters; the market-hours gate makes them research/exit-only).
case "$HHMM" in
  1730) EXTRA="--news-only" ;;
  *)    EXTRA="" ;;
esac
.venv/bin/python -W ignore orchestrator.py $EXTRA "$@" >> logs/cron.log 2>&1

# Friday 5:30pm slot chains the weekly self-review after the evening review.
if [ "$DOW" = "5" ] && [ "$HHMM" = "1730" ]; then
  .venv/bin/python -W ignore orchestrator.py --self-review >> logs/cron.log 2>&1
fi
