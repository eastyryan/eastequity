#!/bin/bash
# Local data relay: gather a fresh market-data bundle and commit it so cloud
# routine runs (whose sandbox blocks SEC/FRED/Yahoo) always have data. Also
# posts any deserving X drafts (API keys live only in the local .env).
# Runs via launchd at :50 every hour while the Mac is awake.
export PATH="/Users/eastonryan/.local/bin:/usr/local/bin:/usr/bin:/bin"
cd /Users/eastonryan/east-equity-agent
mkdir -p logs
{
  echo "=== relay tick $(date '+%Y-%m-%d %H:%M:%S') ==="

  # Discard local mark-to-market noise on the ledger before syncing: gather runs
  # persist price marks into state/portfolio.json, but the CLOUD trade path owns
  # the ledger — carrying hourly local marks into a rebase invites both-modified
  # conflicts with real trade commits. Corporate-action credits survive this:
  # the actions_processed_through marker reverts with the file, so the next
  # gather re-credits idempotently and the conditional add below commits it.
  git checkout -- state/portfolio.json 2>/dev/null || true

  # Sync with origin WITHOUT ever leaving the repo wedged mid-rebase (the
  # 2026-07-14 incident: a binary chart conflict left rebase-merge in place and
  # every later tick died for hours with "Pulling is not possible"). On
  # conflict: abort, and if everything local-only is a disposable relay data
  # commit, drop it and take origin — the Action's bundle is as fresh or
  # fresher. Anything else local is NOT ours to discard: stand down loudly.
  sync_with_origin() {
    git pull --rebase --autostash origin main && return 0
    git rebase --abort 2>/dev/null
    git fetch origin main || return 1
    if git log origin/main..HEAD --format=%s | grep -qv "Refresh market data bundle"; then
      echo "RELAY: non-relay local commits conflict with origin — manual resolution needed; standing down"
      return 1
    fi
    echo "RELAY: dropping superseded local data commit(s), taking origin"
    git reset --hard origin/main
  }
  sync_with_origin || exit 1

  # X posting first: trade drafts may exist even when market data is unchanged.
  .venv/bin/python -W ignore -m tools.x_poster >> logs/xposter.log 2>&1 || true

  GATHER_LOG=$(mktemp)
  .venv/bin/python -W ignore orchestrator.py --gather-only 2>&1 | tee "$GATHER_LOG"
  OUT=$(grep -o 'CONTEXT_FILE=.*' "$GATHER_LOG" | cut -d= -f2-)
  [ -s "$OUT" ] && cp "$OUT" data/cloud_context.json

  git add data/cloud_context.json data/charts dashboard/data/position_charts.json journal/x_posts.jsonl 2>/dev/null
  # Ledger commit ONLY when corporate actions (dividends/splits) changed it this
  # tick — routine ledger writes belong to the cloud trade path, and hourly mark
  # commits were the other half of the recurring rebase-conflict problem.
  if grep -q 'CORPORATE_ACTIONS_CHANGED=1' "$GATHER_LOG"; then
    git add -A state/portfolio.json 2>/dev/null || true
  fi
  # `git add -A` also propagates a CLEARED kill switch (staged deletion).
  git add -A state/KILL_SWITCH 2>/dev/null || true
  rm -f "$GATHER_LOG"
  git diff --cached --quiet && exit 0
  git commit -m "Refresh market data bundle for cloud runs [vercel skip]"
  for i in 1 2 3; do
    git push origin main && exit 0
    sync_with_origin || exit 1
  done
  echo "RELAY: push failed after retries"
  exit 1
} >> logs/relay.log 2>&1
