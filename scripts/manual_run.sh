#!/bin/bash
# Hand-fired run. THE ONLY supported way to run this agent locally.
#
# WHY THIS EXISTS (2026-07-20). The cloud routine is the scheduled trader; local runs
# are the operator deliberately driving. But `--manual` was an opt-in flag on a binary
# whose DEFAULT is "I am a scheduled run", so a local run typed from memory
# (`python orchestrator.py --depth holdings_watchlist`) was indistinguishable from a
# scheduled one. Two things then went wrong silently:
#
#   1. It journaled `manual: false`, so runlib.analytics.build_health() counted it as a
#      COMPLETED SCHEDULED SLOT. A hand-run could fill in a slot the cloud had actually
#      missed, and the heartbeat would report the outage as healthy. An alarm that a
#      manual run can silence is not an alarm.
#   2. It claimed and PUSHED the 30-minute cross-node lease, making the next scheduled
#      cloud run stand down. The operator poking at the system suppressed the main
#      trader.
#
# Both were observed on 2026-07-20. The fix for (2) is in runlib/preflight.py (manual
# runs yield but never claim). This script is the fix for (1): there is no way to fire a
# local run through it and forget the flag.
#
# Logs to its own file, NOT logs/cron.log — the scheduled trader's log stays a clean
# record of what the schedule actually did.
#
#   scripts/manual_run.sh                                  # holdings_watchlist (default)
#   scripts/manual_run.sh --depth full                     # full universe deep dive
#   scripts/manual_run.sh --gather-only                    # data refresh, no trading
#   scripts/manual_run.sh --note "checking the AMD setup"  # annotate the journal entry
#
# If the cloud trader currently holds the lease, this aborts with a message telling you
# to re-run once it finishes. That is intentional: scheduled wins, manual yields.
set -uo pipefail

export PATH="/Users/eastonryan/.local/bin:/Users/eastonryan/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs

# Default depth only when the caller named neither a depth nor a non-trading mode.
EXTRA=""
case " $* " in
  *" --depth "*|*" --gather-only "*|*" --news-only "*|*" --light "*|*" --weekly-market "*|\
*" --self-review "*|*" --study "*|*" --universe-review "*|*" --learning-mark "*|\
*" --freshness-audit "*) ;;
  *) EXTRA="--depth holdings_watchlist" ;;
esac

LOG="logs/manual_$(date +%Y%m%d-%H%M%S).log"
echo "manual run -> $LOG"

# -u so the log streams live instead of block-buffering behind tee.
.venv/bin/python -u -W ignore orchestrator.py --manual $EXTRA "$@" 2>&1 | tee "$LOG"
exit "${PIPESTATUS[0]}"
