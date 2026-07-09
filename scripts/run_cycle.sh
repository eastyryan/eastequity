#!/bin/bash
# East Equity Agent scheduled trading cycle (invoked by cron every 3 hours on weekdays).
export PATH="/Users/eastonryan/.local/bin:/Users/eastonryan/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"
cd /Users/eastonryan/east-equity-agent
mkdir -p logs
exec .venv/bin/python -W ignore orchestrator.py >> logs/cron.log 2>&1
