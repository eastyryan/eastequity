#!/bin/bash
# Box-run X poster: pull, post the day's trade memo (if any), commit the log.
#
# Runs from a Grok Bot routine at ~16:30 ET on weekdays (the box is the poster
# since 2026-10-08; .github/workflows/x-post.yml is dispatch-only because
# GitHub's cron fired after the 16:00-18:59 ET window every day from 09-24).
#
#   scripts/post_x_daily.sh                         # daily policy post
#   scripts/post_x_daily.sh --dry-run --force-window  # render only, no X call
#   scripts/post_x_daily.sh --force-window --draft state/x_catchup/20261002.txt
#
# All posting policy lives in tools/x_poster.py. The duplicate guard is
# journal/x_posts.jsonl: a draft already logged never posts again, so a second
# run (or a manual GitHub dispatch) after a post is a no-op. This script never
# prints secrets: keys are read by the Python process from the .env file.
# Exit 0 = posted or a clean skip (no same-day draft, outside window, dup).
set -uo pipefail
export TZ="America/Toronto"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
PY="${EE_PYTHON:-$ROOT/.venv/bin/python}"
[ -x "$PY" ] || { echo "post_x_daily: python not found at $PY (set EE_PYTHON)"; exit 1; }

# Git auth on the box goes through with-gh-token when it exists.
export PATH="/home/box/.local/bin:$PATH"
GIT=(git)
command -v with-gh-token >/dev/null 2>&1 && GIT=(with-gh-token git)

log() { echo "$(date '+%Y-%m-%d %H:%M:%S %Z') post_x_daily: $*"; }

# A fresh log is the duplicate guard: never post from a stale checkout.
if ! "${GIT[@]}" pull -q --rebase --autostash origin main; then
  log "git pull failed; not posting from a possibly stale journal/x_posts.jsonl"
  exit 1
fi

before=$(git hash-object journal/x_posts.jsonl 2>/dev/null || echo none)
"$PY" -W ignore -m tools.x_poster "$@"
rc=$?
after=$(git hash-object journal/x_posts.jsonl 2>/dev/null || echo none)

if [ "$before" = "$after" ]; then
  if [ $rc -eq 0 ]; then
    log "nothing posted (see the skip reason above); nothing to commit"
  else
    log "poster exited $rc without logging; see output above"
  fi
  exit $rc
fi

git add journal/x_posts.jsonl
git ls-files --error-unmatch state/equity_card.png >/dev/null 2>&1 && git add state/equity_card.png
# --only semantics: commit just these paths, never anyone else's staged work.
git commit -q -m "Record X post [vercel skip]" -- journal/x_posts.jsonl \
  $(git ls-files --error-unmatch state/equity_card.png 2>/dev/null) \
  || { log "commit failed"; exit 1; }
for i in 1 2 3; do
  if "${GIT[@]}" push -q origin HEAD:main; then
    log "post log pushed ($(git rev-parse --short HEAD))"
    exit $rc
  fi
  "${GIT[@]}" pull -q --rebase --autostash origin main || { log "rebase failed"; exit 1; }
done
log "push failed after retries; the post log is committed locally only"
exit 1
