#!/bin/bash
# Local data relay: gather a fresh market-data bundle and commit it so cloud
# routine runs (whose sandbox blocks SEC/FRED/Yahoo) always have data. Also
# posts any deserving X drafts (API keys live only in the local .env).
# Runs via launchd at :50 every hour while the Mac is awake.
export PATH="/Users/eastonryan/.local/bin:/usr/local/bin:/usr/bin:/bin"
cd /Users/eastonryan/east-equity-agent
mkdir -p logs
{
  git pull --rebase origin main || exit 1

  # X posting first: trade drafts may exist even when market data is unchanged.
  .venv/bin/python -W ignore tools/x_poster.py >> logs/xposter.log 2>&1 || true

  OUT=$(.venv/bin/python -W ignore orchestrator.py --gather-only 2>&1 | grep -o 'CONTEXT_FILE=.*' | cut -d= -f2-)
  [ -s "$OUT" ] && cp "$OUT" data/cloud_context.json

  git add data/cloud_context.json data/charts journal/x_posts.jsonl 2>/dev/null
  git diff --cached --quiet && exit 0
  git commit -m "Refresh market data bundle for cloud runs [vercel skip]"
  for i in 1 2 3; do
    git push origin main && exit 0
    git pull --rebase origin main || exit 1
  done
  exit 1
} >> logs/relay.log 2>&1
