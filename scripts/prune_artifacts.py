#!/usr/bin/env python3
"""Prune expired run artifacts from state/ — dry-run by default.

WHY THIS EXISTS (2026-08-05 audit): every run writes an X draft, every study
session writes a bundle + raw response, and every brain wake writes two context
packs. Nothing ever deleted any of them. Measured at audit time: 172 committed
state/x_draft_*.txt, 24 committed study artifacts, and 234 UNTRACKED
state/context_*.json (~1-2MB each) accumulating locally with no pruning path
anywhere in the repo. The committed ones also ride along in every workflow
checkout forever.

WHAT IS SAFE TO DELETE, WITH EVIDENCE (all verified against tools/x_poster.py
and journal/x_posts.jsonl before this was written):

  * X drafts, >30 days old. The poster can only ever post a draft that
    (a) matches glob "x_draft_*_trade.txt" (x_poster.py:232) AND
    (b) carries TODAY's date in its filename (x_poster.py:233, `today_local in
    d.name` — run ids start YYYYMMDD) AND (c) is not already in the post log
    (journal/x_posts.jsonl, x_poster.py:35/97-100). So a 30-day-old draft is
    categorically unpostable. Two tiers of caution on top of that:
      - `*_trade.txt` drafts are deleted ONLY if they also appear in the post
        log (posted / superseded / dry_run — presence in the log is exactly
        what x_poster._already_posted() treats as consumed). An old trade
        draft the log has never seen is KEPT forever: it should be impossible
        to post, but "unposted draft never selected" is the rule, not an
        inference.
      - plain drafts (no `_trade` suffix) have no log entry to demand — the
        poster never posts them directly. Their one downstream use is
        promote_draft_for_fill (x_poster.py:147), which reads the plain draft
        when a fill lands for that run id; if the file is gone it falls back
        to composing the memo from the fill + journal/proposals
        (x_poster.py:156), which the docstring calls the BETTER source — and
        the promoted file would carry the old run date, making it unpostable
        by rule (b) regardless.

  * Study artifacts (state/study_*.json bundles, state/study_response_*.txt
    raw responses), >30 days old. Written once per session by
    runlib/reviews.py (bundle at :236-237, response at :291/:384) as the
    session's input and its audit trail. Grep of runlib/tools/scripts/
    dashboard finds NO later reader — the durable product is the lesson,
    which lives in the knowledge base, not in these files.

  * Local context packs (state/context_*.json, state/context_full_*.json),
    >14 days old. Gitignored (".gitignore: state/context_*.json" — the
    pattern covers both), so these only ever exist on the machine that ran
    the brain; the committed relay copy is data/cloud_context.json, which
    this script NEVER touches. Every reader wants the newest one:
    runlib/capabilities.py:88-100 prefilters to the 8 newest by mtime,
    scripts/stop_watch.py:165-167 takes the single newest, and a run reads
    its own pack by exact run id only while it is running.

AGE COMES FROM THE FILENAME, NOT MTIME. Run-dated artifacts embed YYYYMMDD in
their names; mtime is worthless in CI because a fresh checkout stamps every
file with clone time, which would make everything look 0 days old and nothing
ever prunable. A file whose name carries no parseable date is always KEPT.

Default is DRY-RUN: prints every candidate with its age and the reason it is
safe, plus everything it deliberately kept. --apply actually deletes. Deleting
tracked files only changes the working tree — the weekly workflow step that
calls --apply stages exactly those deletions and pushes (gather-data.yml);
untracked context packs just disappear locally, which is the whole point.

  python scripts/prune_artifacts.py            # dry-run, always exit 0
  python scripts/prune_artifacts.py --apply    # delete
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
POST_LOG = ROOT / "journal" / "x_posts.jsonl"

DRAFT_MAX_AGE_DAYS = 30
STUDY_MAX_AGE_DAYS = 30
CONTEXT_MAX_AGE_DAYS = 14

# First YYYYMMDD in the filename — run ids are <YYYYMMDD>-<hex>.
_DATE_RE = re.compile(r"(20\d{2})(\d{2})(\d{2})")


def _run_date(name: str):
    m = _DATE_RE.search(name)
    if not m:
        return None
    try:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return datetime(y, mo, d, tzinfo=timezone.utc).date()
    except ValueError:
        return None


def _consumed_drafts() -> set[str]:
    """Draft filenames the post log has seen — mirrors x_poster._already_posted(),
    which treats ANY log entry as consumed (x_poster.py:97-100). Fail-closed: an
    unreadable log line consumes nothing, so its draft is kept."""
    out: set[str] = set()
    if not POST_LOG.exists():
        return out
    for line in POST_LOG.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        d = rec.get("draft")
        if isinstance(d, str):
            out.add(d)
    return out


def collect(today=None) -> tuple[list[tuple[Path, str]], list[tuple[Path, str]]]:
    """(candidates, kept) — each entry is (path, human reason)."""
    today = today or datetime.now(timezone.utc).date()
    draft_cutoff = today - timedelta(days=DRAFT_MAX_AGE_DAYS)
    study_cutoff = today - timedelta(days=STUDY_MAX_AGE_DAYS)
    ctx_cutoff = today - timedelta(days=CONTEXT_MAX_AGE_DAYS)
    consumed = _consumed_drafts()

    candidates: list[tuple[Path, str]] = []
    kept: list[tuple[Path, str]] = []

    def age(d):
        return (today - d).days

    for f in sorted(STATE.glob("x_draft_*.txt")):
        d = _run_date(f.name)
        if d is None:
            kept.append((f, "no parseable run date — never deleted"))
            continue
        if d >= draft_cutoff:
            continue  # young; not even worth listing
        if f.name.endswith("_trade.txt"):
            if f.name in consumed:
                candidates.append(
                    (f, f"trade draft, {age(d)}d old, consumed per post log"))
            else:
                kept.append(
                    (f, f"trade draft {age(d)}d old but ABSENT from post log — "
                        f"unposted drafts are never selected"))
        else:
            candidates.append(
                (f, f"plain draft, {age(d)}d old — poster only posts *_trade.txt "
                    f"named with today's date; promotion falls back to "
                    f"journal/proposals"))

    for pattern in ("study_2*.json", "study_response_2*.txt"):
        for f in sorted(STATE.glob(pattern)):
            d = _run_date(f.name)
            if d is None:
                kept.append((f, "no parseable run date — never deleted"))
            elif d < study_cutoff:
                candidates.append(
                    (f, f"study artifact, {age(d)}d old — one-shot session "
                        f"input/audit, lesson persists in knowledge base"))

    for pattern in ("context_2*.json", "context_full_2*.json"):
        for f in sorted(STATE.glob(pattern)):
            d = _run_date(f.name)
            if d is None:
                kept.append((f, "no parseable run date — never deleted"))
            elif d < ctx_cutoff:
                candidates.append(
                    (f, f"local context pack, {age(d)}d old — untracked, "
                        f"all readers take the newest pack only"))

    return candidates, kept


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Prune expired state/ run artifacts (dry-run by default).")
    ap.add_argument("--apply", action="store_true",
                    help="actually delete; without it, only report")
    args = ap.parse_args()

    candidates, kept = collect()
    verb = "DELETE" if args.apply else "WOULD DELETE"

    counts: dict[str, int] = {}
    for f, why in candidates:
        kind = ("x_draft" if f.name.startswith("x_draft") else
                "study" if f.name.startswith("study") else "context")
        counts[kind] = counts.get(kind, 0) + 1
        print(f"{verb} {f.relative_to(ROOT)} — {why}")
        if args.apply:
            try:
                f.unlink()
            except OSError as e:
                # Report and continue: a locked file must not abort the sweep.
                print(f"  FAILED to delete: {e}", file=sys.stderr)

    for f, why in kept:
        print(f"KEEP {f.relative_to(ROOT)} — {why}")

    total = sum(counts.values())
    mode = "applied" if args.apply else "dry-run"
    print(f"\nprune summary ({mode}): {total} candidate(s) "
          f"[x_draft={counts.get('x_draft', 0)} study={counts.get('study', 0)} "
          f"context={counts.get('context', 0)}], {len(kept)} kept for safety")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
