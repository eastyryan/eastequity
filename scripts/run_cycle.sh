#!/bin/bash
# East Equity Agent scheduled cycle. Invoked by launchd every hour at :00, :30, :59
# with --scheduled; the time-slot gate below decides which ticks actually run.
# Direct invocation (no --scheduled) always runs, e.g. `run_cycle.sh --self-review`.
#
# DOUBLE-TRADE NOTE: this is the LOCAL trader. The orchestrator's RUN_LOCK
# (state/RUN_LOCK) is a LOCAL file lock only — it does NOT coordinate with the
# scheduled CLOUD trader, so both nodes can theoretically trade the same ledger
# in the same window. The kill switch is honored (orchestrator preflight aborts
# on state/KILL_SWITCH) and the relay keeps the ledger synced via git, but a real
# cross-node lease is still needed — see the integrator hand-off. Do not add a
# second orchestrator invocation to this script.
export PATH="/Users/eastonryan/.local/bin:/Users/eastonryan/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"
cd /Users/eastonryan/east-equity-agent
mkdir -p logs

DOW=$(date +%u)    # 1=Mon ... 6=Sat 7=Sun
HHMM=$(date +%H%M)

if [ "$1" = "--scheduled" ]; then
  shift
  if [ "$DOW" -le 5 ]; then
    # Weekdays: 6am, 9am, hourly 10am-4pm, 5:30pm, 7:30pm, midnight
    case "$HHMM" in
      0600|0900|1000|1100|1200|1300|1400|1500|1600|1730|0000) ;;
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

# Slot-aware depth: midnight is a news review; 5:30pm is a FULL-RESEARCH review
# (all names, deep research) with NO trading; market-day slots incl. 6am/9am
# pre-market are full trading cycles (pre-market data matters).
case "$HHMM" in
  0000|1730) EXTRA="--news-only" ;;
  *)         EXTRA="" ;;
esac
.venv/bin/python -W ignore orchestrator.py $EXTRA "$@" >> logs/cron.log 2>&1

# Friday 5:30pm slot chains the weekly self-review after the evening review.
if [ "$DOW" = "5" ] && [ "$HHMM" = "1730" ]; then
  .venv/bin/python -W ignore orchestrator.py --self-review >> logs/cron.log 2>&1
fi
