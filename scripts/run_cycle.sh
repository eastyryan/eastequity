#!/bin/bash
# East Equity Agent scheduled cycle (cron).
# Weekdays: full trading cycles. Weekends: news-only reviews (markets closed).
# The Friday 19:30 slot also chains the weekly self-review after the trading cycle.
export PATH="/Users/eastonryan/.local/bin:/Users/eastonryan/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"
cd /Users/eastonryan/east-equity-agent
mkdir -p logs

DOW=$(date +%u)   # 1=Mon ... 6=Sat 7=Sun
HOUR=$(date +%H)

if [ "$DOW" -ge 6 ]; then
  .venv/bin/python -W ignore orchestrator.py --news-only "$@" >> logs/cron.log 2>&1
  exit $?
fi

.venv/bin/python -W ignore orchestrator.py "$@" >> logs/cron.log 2>&1

if [ "$DOW" = "5" ] && [ "$HOUR" = "19" ] && [ $# -eq 0 ]; then
  .venv/bin/python -W ignore orchestrator.py --self-review >> logs/cron.log 2>&1
fi
