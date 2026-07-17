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
#
# RUN DEPTHS (2026-07-14 redesign — full runs were too slow on every slot):
#   0600  light                 holdings/watchlist prices + exits only
#   0900  holdings_watchlist    four fast trading cycles (holdings + watchlist deep)
#   1000  holdings_watchlist
#   1200  holdings_watchlist
#   1400  holdings_watchlist
#   1600  full                  one full-universe deep dive per day
#   1730  evening_review        news-only research review
#   Sun 0000 weekly_market      all-sector market check-in (no trading)
# KEEP IN SYNC with expected_slots() + autonomy_config schedule.slot_depths.
export PATH="/Users/eastonryan/.local/bin:/Users/eastonryan/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"
cd /Users/eastonryan/east-equity-agent
mkdir -p logs

DOW=$(date +%u)    # 1=Mon ... 6=Sat 7=Sun
HHMM=$(date +%H%M)

if [ "$1" = "--scheduled" ]; then
  shift
  if [ "$DOW" -eq 7 ]; then
    # Sunday: weekly market check-in at midnight + late news
    case "$HHMM" in
      0000|2359) ;;
      *) exit 0 ;;
    esac
  elif [ "$DOW" -eq 6 ]; then
    # Saturday: news checks only
    case "$HHMM" in
      0000|2359) ;;
      *) exit 0 ;;
    esac
  elif [ "$DOW" -le 5 ]; then
    # Weekdays: SEVEN slots — 6am, 9am, 10am, 12pm, 2pm, 4pm, 5:30pm
    case "$HHMM" in
      0600|0900|1000|1200|1400|1600|1730) ;;
      *) exit 0 ;;
    esac
  else
    exit 0
  fi
fi

# Sunday midnight: weekly multi-sector market check-in (no trading).
if [ "$DOW" -eq 7 ] && [ "$HHMM" = "0000" ]; then
  .venv/bin/python -W ignore orchestrator.py --weekly-market "$@" >> logs/cron.log 2>&1
  exit $?
fi

if [ "$DOW" -ge 6 ]; then
  .venv/bin/python -W ignore orchestrator.py --news-only "$@" >> logs/cron.log 2>&1
  exit $?
fi

# Slot-aware depth: four holdings/watchlist cycles, one full deep dive, light pre-market.
case "$HHMM" in
  0600) EXTRA="--depth light" ;;
  0900|1000|1200|1400) EXTRA="--depth holdings_watchlist" ;;
  1600) EXTRA="--depth full" ;;
  1730) EXTRA="--news-only" ;;
  *)    EXTRA="--depth full" ;;
esac
.venv/bin/python -W ignore orchestrator.py $EXTRA "$@" >> logs/cron.log 2>&1

# Friday 5:30pm slot chains the weekly self-review after the evening review.
if [ "$DOW" = "5" ] && [ "$HHMM" = "1730" ]; then
  .venv/bin/python -W ignore orchestrator.py --self-review >> logs/cron.log 2>&1
fi

# Every weekday 5:30pm slot chains the daily study session after the evening
# review (and after Friday's self-review, so study can react to it): ONE
# researched curriculum topic written into the knowledge base + learning journal.
if [ "$DOW" -le 5 ] && [ "$HHMM" = "1730" ]; then
  .venv/bin/python -W ignore orchestrator.py --study >> logs/cron.log 2>&1
fi

# Sunday evening also runs universe curation after the late news tick (when scheduled).
if [ "$DOW" = "7" ] && [ "$HHMM" = "2359" ]; then
  .venv/bin/python -W ignore orchestrator.py --universe-review >> logs/cron.log 2>&1
fi
