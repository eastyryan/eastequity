"""X (Twitter) auto-poster for East Equity Agent.

Every run writes a draft to state/x_draft_<run_id>.txt. This module decides
which drafts deserve a post and publishes them via the X API v2:

  * any draft containing a fill line ("Opened"/"Closed") posts immediately
    (trade alerts are the interesting events), and
  * at most one no-trade daily summary per day (the end-of-day run).

Posted drafts are recorded in journal/x_posts.jsonl so nothing double-posts.
Runs from the hourly relay job on the Mac, so API keys stay local-only in .env
(X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET - an X developer
app with the free tier's write access is enough at this volume).

CLI: python -m tools.x_poster [--dry-run]
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POST_LOG = ROOT / "journal" / "x_posts.jsonl"
MAX_LEN = 280


def _keys() -> dict | None:
    keys = {k: os.environ.get(k) for k in
            ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET")}
    return keys if all(keys.values()) else None


def _split_thread(text: str, limit: int = 272) -> list[str]:
    """Split long-form posts into a thread on paragraph, then sentence, boundaries."""
    if len(text) <= MAX_LEN:
        return [text]
    chunks, current = [], ""
    for para in [p.strip() for p in text.split("\n\n") if p.strip()]:
        pieces = [para] if len(para) <= limit else re.split(r"(?<=[.!?]) +", para)
        for piece in pieces:
            if current and len(current) + len(piece) + 2 > limit:
                chunks.append(current.strip())
                current = piece
            else:
                current = f"{current}\n\n{piece}" if (current and piece is pieces[0]
                                                      and len(pieces) == 1) else \
                          (f"{current} {piece}" if current else piece)
        if current and len(current) >= limit * 0.7:
            chunks.append(current.strip())
            current = ""
    if current.strip():
        chunks.append(current.strip())
    n = len(chunks)
    return [f"{c} ({i+1}/{n})" if n > 1 else c for i, c in enumerate(chunks)]


def post_tweet(text: str) -> dict:
    """Post text; long-form content goes out as a reply-chained thread."""
    from requests_oauthlib import OAuth1Session
    keys = _keys()
    if keys is None:
        return {"status": "skipped", "reason": "X API keys not configured in .env"}
    session = OAuth1Session(keys["X_API_KEY"], keys["X_API_SECRET"],
                            keys["X_ACCESS_TOKEN"], keys["X_ACCESS_SECRET"])
    ids, reply_to = [], None
    for chunk in _split_thread(text):
        payload = {"text": chunk[:MAX_LEN]}
        if reply_to:
            payload["reply"] = {"in_reply_to_tweet_id": reply_to}
        r = session.post("https://api.twitter.com/2/tweets", json=payload, timeout=30)
        if r.status_code not in (200, 201):
            return {"status": "error", "code": r.status_code, "body": r.text[:300],
                    "posted_ids": ids}
        reply_to = r.json().get("data", {}).get("id")
        ids.append(reply_to)
    return {"status": "posted", "tweet_id": ids[0] if ids else None,
            "thread_len": len(ids)}


def _already_posted() -> set[str]:
    if not POST_LOG.exists():
        return set()
    return {json.loads(line)["draft"] for line in POST_LOG.read_text().splitlines()}


def _summary_posted_today(today: str) -> bool:
    if not POST_LOG.exists():
        return False
    for line in POST_LOG.read_text().splitlines():
        rec = json.loads(line)
        if rec.get("kind") == "summary" and rec.get("ts", "").startswith(today):
            return True
    return False


def process_drafts(dry_run: bool = False) -> list[dict]:
    results = []
    posted = _already_posted()
    today = datetime.now(timezone.utc).date().isoformat()
    for draft in sorted((ROOT / "state").glob("x_draft_*.txt")):
        if draft.name in posted:
            continue
        text = draft.read_text().strip()
        if not text:
            continue
        has_fill = ("_trade" in draft.name
                    or bool(re.search(r"^(Opened|Closed) ", text, re.MULTILINE)))
        hour_local = datetime.now().hour
        is_eod_window = 17 <= hour_local <= 19
        if has_fill:
            kind = "trade"
        elif is_eod_window and not _summary_posted_today(today):
            kind = "summary"
        else:
            continue  # quiet drafts outside the summary window don't post

        result = {"status": "dry_run"} if dry_run else post_tweet(text)
        record = {"ts": datetime.now(timezone.utc).isoformat(), "draft": draft.name,
                  "kind": kind, **result}
        results.append(record)
        if result["status"] in ("posted", "dry_run"):
            POST_LOG.parent.mkdir(parents=True, exist_ok=True)
            with POST_LOG.open("a") as f:
                f.write(json.dumps(record) + "\n")
        print(json.dumps(record))
    return results


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    process_drafts(dry_run="--dry-run" in sys.argv)
