#!/bin/bash
# East Equity Agent scheduled cycle. Invoked by launchd every hour at :00, :30, :59
# with --scheduled; the time-slot gate below decides which ticks actually run.
# Direct invocation (no --scheduled) always runs, e.g. `run_cycle.sh --self-review`.
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
      0600|0900|1000|1100|1200|1300|1400|1500|1600|1730|1930|0000) ;;
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

# Slot-aware depth: midnight is a news review; all market-day slots including
# 6am/9am pre-market are FULL research cycles (pre-market data matters).
case "$HHMM" in
  0000) EXTRA="--news-only" ;;
  *)    EXTRA="" ;;
esac
.venv/bin/python -W ignore orchestrator.py $EXTRA "$@" >> logs/cron.log 2>&1

# Friday 7:30pm slot chains the weekly self-review after the trading cycle.
if [ "$DOW" = "5" ] && [ "$HHMM" = "1930" ]; then
  .venv/bin/python -W ignore orchestrator.py --self-review >> logs/cron.log 2>&1
fi
