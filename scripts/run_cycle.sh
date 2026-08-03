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
# RUN DEPTHS come from autonomy_config.json -> schedule.slot_depths, resolved at run
# time by orchestrator's --auto-depth. THEY ARE NO LONGER DUPLICATED HERE.
#
# WHY (2026-08-03): this script used to carry its own `case "$HHMM"` depth map, and the
# comment above it told you to "KEEP IN SYNC" by hand. It drifted the same day the
# instruction was written — 10:30 was promoted from holdings_watchlist to `full` in
# autonomy_config.json AND runlib/depths.py, and this file still said
# `0845|1030|1200|1400) --depth holdings_watchlist`. So the local trader would have run
# a mini-scan at the one slot that was changed specifically to put a full universe scan
# inside the session. --auto-depth reads the live config, which also picks up the
# earnings deep-dive escalation (schedule.earnings_deep_dive) that a hardcoded --depth
# silently bypassed.
#
# The SLOT GATE below stays hardcoded on purpose: it answers "should this launchd tick
# do anything at all", which is a property of the launchd plist, not of the config.
# KEEP IT IN SYNC with runlib.analytics.expected_slots() — tests/test_schedule_sources_agree.py
# fails if it drifts.
#   0600 / 0845 / 1030 / 1200 / 1400 / 1600 / 1730 weekdays
#   Sun 0000 weekly_market (all-sector market check-in, no trading) + 2359 news
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
    # Weekdays: SEVEN slots — 6am, 8:45am, 10:30am, 12pm, 2pm, 4pm, 5:30pm
    case "$HHMM" in
      0600|0845|1030|1200|1400|1600|1730) ;;
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

# Slot-aware depth, resolved from schedule.slot_depths by the orchestrator itself.
.venv/bin/python -W ignore orchestrator.py --auto-depth "$@" >> logs/cron.log 2>&1

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
