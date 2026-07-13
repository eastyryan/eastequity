#!/bin/bash
# Local data relay: gather a fresh market-data bundle and commit it so cloud
# routine runs (whose sandbox blocks SEC/FRED/Yahoo) always have data. Also
# posts any deserving X drafts (API keys live only in the local .env).
# Runs via launchd at :50 every hour while the Mac is awake.
export PATH="/Users/eastonryan/.local/bin:/usr/local/bin:/usr/bin:/bin"
cd /Users/eastonryan/east-equity-agent
mkdir -p logs
{
  # --autostash: the working tree routinely holds regenerated artifacts between
  # ticks (chart PNGs deleted/added by the last gather). A plain rebase refuses
  # with "unstaged changes" and the WHOLE relay dies - the bundle then goes stale
  # every hour until a human cleans the tree. Autostash makes that impossible.
  git pull --rebase --autostash origin main || exit 1

  # X posting first: trade drafts may exist even when market data is unchanged.
  .venv/bin/python -W ignore tools/x_poster.py >> logs/xposter.log 2>&1 || true

  OUT=$(.venv/bin/python -W ignore orchestrator.py --gather-only 2>&1 | grep -o 'CONTEXT_FILE=.*' | cut -d= -f2-)
  [ -s "$OUT" ] && cp "$OUT" data/cloud_context.json

  git add data/cloud_context.json data/charts journal/x_posts.jsonl 2>/dev/null
  # Keep the authoritative ledger + kill switch synced from this node. Staged
  # separately and guarded so a missing file never aborts the data commit
  # (git add is atomic — one bad pathspec drops the whole add). `git add -A`
  # also propagates a CLEARED kill switch (staged deletion) to the cloud trader.
  # The `git pull --rebase` at the top means we add on top of the latest remote
  # ledger, so this syncs rather than clobbers the cloud trader's commits.
  git add -A state/portfolio.json 2>/dev/null || true
  git add -A state/KILL_SWITCH 2>/dev/null || true
  git diff --cached --quiet && exit 0
  git commit -m "Refresh market data bundle for cloud runs [vercel skip]"
  for i in 1 2 3; do
    git push origin main && exit 0
    git pull --rebase --autostash origin main || exit 1
  done
  exit 1
} >> logs/relay.log 2>&1
