#!/bin/bash
# Local data relay: gather a fresh market-data bundle and commit it so cloud
# routine runs (whose sandbox blocks SEC/FRED/Yahoo) always have data.
# Runs via launchd at :40 every hour while the Mac is awake. Cheap: no Claude
# involved, just the Python data tools.
export PATH="/Users/eastonryan/.local/bin:/usr/local/bin:/usr/bin:/bin"
cd /Users/eastonryan/east-equity-agent
mkdir -p logs
{
  git pull --rebase origin main || exit 1
  OUT=$(.venv/bin/python -W ignore orchestrator.py --gather-only 2>&1 | grep -o 'CONTEXT_FILE=.*' | cut -d= -f2-)
  [ -s "$OUT" ] || exit 1
  cp "$OUT" data/cloud_context.json
  git add data/cloud_context.json
  git diff --cached --quiet && exit 0
  git commit -m "Refresh market data bundle for cloud runs [vercel skip]"
  for i in 1 2 3; do
    git push origin main && exit 0
    git pull --rebase origin main || exit 1
  done
  exit 1
} >> logs/relay.log 2>&1
