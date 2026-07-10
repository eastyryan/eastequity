#!/bin/bash
# East Equity Agent scheduled cycle (cron, every 3h on weekdays).
# The Friday 18:00 slot also runs the weekly self-review after the trading cycle.
export PATH="/Users/eastonryan/.local/bin:/Users/eastonryan/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"
cd /Users/eastonryan/east-equity-agent
mkdir -p logs
.venv/bin/python -W ignore orchestrator.py "$@" >> logs/cron.log 2>&1
if [ "$(date +%u)" = "5" ] && [ "$(date +%H)" = "18" ] && [ $# -eq 0 ]; then
  .venv/bin/python -W ignore orchestrator.py --self-review >> logs/cron.log 2>&1
fi
