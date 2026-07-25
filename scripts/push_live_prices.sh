#!/bin/bash
# Local live-price feeder — companion to .github/workflows/live-prices.yml.
#
# GitHub coalesces the Action's */5 cron under load (observed ~hourly on
# 2026-07-15), which starves the stop-enforcement overlay: a snapshot older
# than the freshness window is deliberately NOT applied, so the "live" feed
# was almost never live. This launchd job (com.eastequity.liveprices, every
# 5 min) refreshes from the Mac during market hours and publishes the same
# single-file orphan commit to the 'live-data' branch — NEVER main, so it can
# never supersede a trade run. It uses git plumbing (hash-object/mktree/
# commit-tree) instead of checkout --orphan so the working branch and index
# are never disturbed; a force-push race with the Action is benign (both
# sides publish an equally-fresh full snapshot).
export PATH="/Users/eastonryan/.local/bin:/usr/local/bin:/usr/bin:/bin"
cd /Users/eastonryan/east-equity-agent || exit 0
mkdir -p logs

DOW=$(TZ=America/New_York date +%u)
HHMM=$((10#$(TZ=America/New_York date +%H%M)))
# Weekdays 9:00am-4:30pm ET (quotes matter while stops can fire).
if [ "$DOW" -gt 5 ] || [ "$HHMM" -lt 900 ] || [ "$HHMM" -gt 1630 ]; then
  exit 0
fi

{
  echo "=== live-price tick $(date -u +%FT%TZ) ==="
  .venv/bin/python -W ignore -c "
from tools.live_prices import refresh_from_book
r = refresh_from_book()
print('refreshed', r.get('n'), 'quotes as of', r.get('as_of'))" || exit 0
  [ -s state/live_prices.json ] || { echo "no snapshot produced"; exit 0; }
  # BETWEEN-CYCLE STOP ENFORCEMENT. Stops are numbers in state/portfolio.json, not
  # resting broker orders, so they were only honored when a full cycle ran — leaving
  # ~2h intraday windows (10:30->12:00, 12:00->14:00, 14:00->16:00). This tick
  # already had the fresh quotes; it just never looked at the stops (trigger_watch
  # is a WATCHLIST tool and explicitly skips held tickers). Runs BEFORE the push:
  # honoring a stop is more urgent than publishing a snapshot.
  #
  # LOCAL ONLY, deliberately. The cloud live-prices workflow is an ephemeral runner
  # that publishes a single-file orphan commit and never commits state/portfolio.json,
  # so a fill executed there would reach Alpaca while the ledger record died with the
  # sandbox — the orphan-position failure mode. Here the ledger is on disk and the
  # next local run reads it. Cloud stop-watching needs the same out-of-band fill
  # ingestion path that broker-resting stops need.
  # Still runs locally when the laptop happens to be awake, but is no longer
  # the ONLY place stop enforcement happens: .github/workflows/stop-watch.yml
  # runs the same module every 10 minutes in the cloud and commits the ledger.
  # Both nodes are safe to run concurrently — _apply_fill refuses to book the
  # same broker order id twice, which is the one key identical across checkouts.
  .venv/bin/python -W ignore -m scripts.stop_watch || true
  BLOB=$(git hash-object -w state/live_prices.json) || exit 0
  SUBTREE=$(printf '100644 blob %s\tlive_prices.json\n' "$BLOB" | git mktree)
  # Repo-root vercel.json in the orphan tree so Vercel never builds this
  # app-less branch (it reads git.deploymentEnabled from the repo root; without
  # it every push triggers a preview build that fails on the missing "dashboard"
  # Root Directory). "state" sorts before "vercel.json", so tree order is valid.
  VERCEL_BLOB=$(printf '{"$schema":"https://openapi.vercel.sh/vercel.json","git":{"deploymentEnabled":false}}\n' | git hash-object -w --stdin)
  TREE=$(printf '040000 tree %s\tstate\n100644 blob %s\tvercel.json\n' "$SUBTREE" "$VERCEL_BLOB" | git mktree)
  COMMIT=$(git commit-tree "$TREE" -m "Live universe prices [skip ci][vercel skip]")
  git push -f origin "$COMMIT:refs/heads/live-data" && echo "pushed $COMMIT"
  # Event-driven trigger runs: if a watchlist would_buy_at level just CONFIRMED
  # on these fresh quotes (two consecutive ticks), spawn one focused trading
  # run instead of waiting for the next scheduled slot. All guards live in
  # tools/trigger_watch.py; the spawned run passes every normal gate.
  .venv/bin/python -W ignore -m tools.trigger_watch || true
} >> logs/live_prices_local.log 2>&1
exit 0
