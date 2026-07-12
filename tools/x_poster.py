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


def _style(text: str, offset_upper: int, offset_lower: int,
           digits_offset: int | None = None) -> str:
    out = []
    for ch in text:
        if "A" <= ch <= "Z":
            out.append(chr(ord(ch) - 65 + offset_upper))
        elif "a" <= ch <= "z":
            out.append(chr(ord(ch) - 97 + offset_lower))
        elif digits_offset and "0" <= ch <= "9":
            out.append(chr(ord(ch) - 48 + digits_offset))
        else:
            out.append(ch)
    return "".join(out)


def bold(text: str) -> str:
    return _style(text, 0x1D5D4, 0x1D5EE, 0x1D7EC)  # sans-serif bold


def italic(text: str) -> str:
    return _style(text, 0x1D608, 0x1D622)  # sans-serif italic (digits stay plain)


def _is_url(token: str) -> bool:
    return bool(re.search(r"https?://|www\.|\.[a-z]{2,4}(/|$)", token))


def _is_cashtag(token: str) -> bool:
    return bool(re.fullmatch(r"\$[A-Za-z]{1,5}[.,!?:;)]*", token))


def journal_header(date=None) -> str:
    from datetime import datetime as _dt
    d = date or _dt.now()
    return bold(f"East Equity Agent Journal - {d.strftime('%B %-d, %Y')}")


def format_for_x(text: str) -> str:
    """House style: **Company (TICK)** markers become Unicode bold; everything
    else (the agent's own commentary) becomes Unicode italic. URLs and the
    closing disclaimer line stay plain so they remain clickable/readable."""
    lines_out = []
    for line in text.split("\n"):
        if line.strip().lower().startswith("this is a paper"):
            lines_out.append(line)
            continue
        parts = re.split(r"\*\*(.+?)\*\*", line)
        styled = []
        for i, part in enumerate(parts):
            if i % 2 == 1:
                styled.append(bold(part))
            else:
                styled.append(" ".join(
                    tok if (_is_url(tok) or _is_cashtag(tok)) else italic(tok)
                    for tok in part.split(" ")))
        lines_out.append("".join(styled))
    return "\n".join(lines_out)


def _upload_media(session, path: str) -> str | None:
    with open(path, "rb") as f:
        r = session.post("https://upload.twitter.com/1.1/media/upload.json",
                         files={"media": f}, timeout=60)
    if r.status_code in (200, 201):
        return r.json().get("media_id_string")
    print(json.dumps({"media_upload_error": r.status_code, "body": r.text[:200]}))
    return None


def post_tweet(text: str, media_path: str | None = None) -> dict:
    """Single long-form post (X Premium allows up to 25k chars), optional image."""
    from requests_oauthlib import OAuth1Session
    keys = _keys()
    if keys is None:
        return {"status": "skipped", "reason": "X API keys not configured in .env"}
    session = OAuth1Session(keys["X_API_KEY"], keys["X_API_SECRET"],
                            keys["X_ACCESS_TOKEN"], keys["X_ACCESS_SECRET"])
    payload = {"text": text[:25000]}
    if media_path:
        media_id = _upload_media(session, media_path)
        if media_id:
            payload["media"] = {"media_ids": [media_id]}
    r = session.post("https://api.twitter.com/2/tweets", json=payload, timeout=60)
    if r.status_code in (200, 201):
        return {"status": "posted", "tweet_id": r.json().get("data", {}).get("id"),
                "with_media": "media" in payload}
    return {"status": "error", "code": r.status_code, "body": r.text[:300]}


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


def _posted_today(today: str) -> bool:
    if not POST_LOG.exists():
        return False
    return any(json.loads(line).get("ts", "").startswith(today)
               and json.loads(line).get("status") == "posted"
               for line in POST_LOG.read_text().splitlines())


def process_drafts(dry_run: bool = False) -> list[dict]:
    """User policy: at most ONE post per day, in the market-close window
    (4:00-6:59pm local), and ONLY if a trade was made that day. The post is the
    day's latest trade memo; everything else stays on the dashboard."""
    results = []
    posted = _already_posted()
    today_utc = datetime.now(timezone.utc).date().isoformat()
    today_local = datetime.now().strftime("%Y%m%d")

    if not (16 <= datetime.now().hour <= 18):
        return results
    if _posted_today(today_utc):
        return results

    todays_trades = [d for d in sorted((ROOT / "state").glob("x_draft_*_trade.txt"))
                     if today_local in d.name and d.name not in posted
                     and d.read_text().strip()]
    if not todays_trades:
        return results

    draft = todays_trades[-1]  # latest trade memo of the day
    text = journal_header() + "\n\n" + format_for_x(draft.read_text().strip())
    chart = None
    try:
        from tools.chart_card import render_equity_card
        chart = str(render_equity_card())
    except Exception as e:
        print(json.dumps({"chart_card_error": str(e)[:200]}))
    result = {"status": "dry_run"} if dry_run else post_tweet(text, media_path=chart)
    record = {"ts": datetime.now(timezone.utc).isoformat(), "draft": draft.name,
              "kind": "trade", **result}
    results.append(record)
    if result["status"] in ("posted", "dry_run"):
        POST_LOG.parent.mkdir(parents=True, exist_ok=True)
        with POST_LOG.open("a") as f:
            f.write(json.dumps(record) + "\n")
        # Retire the day's other trade drafts so they never post late.
        for d in todays_trades[:-1]:
            with POST_LOG.open("a") as f:
                f.write(json.dumps({"ts": record["ts"], "draft": d.name,
                                    "kind": "trade", "status": "superseded"}) + "\n")
    print(json.dumps(record))
    return results


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    process_drafts(dry_run="--dry-run" in sys.argv)
