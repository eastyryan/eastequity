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
  BLOB=$(git hash-object -w state/live_prices.json) || exit 0
  SUBTREE=$(printf '100644 blob %s\tlive_prices.json\n' "$BLOB" | git mktree)
  TREE=$(printf '040000 tree %s\tstate\n' "$SUBTREE" | git mktree)
  COMMIT=$(git commit-tree "$TREE" -m "Live universe prices [skip ci][vercel skip]")
  git push -f origin "$COMMIT:refs/heads/live-data" && echo "pushed $COMMIT"
  # Event-driven trigger runs: if a watchlist would_buy_at level just CONFIRMED
  # on these fresh quotes (two consecutive ticks), spawn one focused trading
  # run instead of waiting for the next scheduled slot. All guards live in
  # tools/trigger_watch.py; the spawned run passes every normal gate.
  .venv/bin/python -W ignore -m tools.trigger_watch || true
} >> logs/live_prices_local.log 2>&1
exit 0
