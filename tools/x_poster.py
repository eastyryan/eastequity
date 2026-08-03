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
# Script-style invocation (`python tools/x_poster.py`) puts tools/ on sys.path
# instead of the repo root, which silently broke `from tools.chart_card import
# ...` — every trade post lost its equity-card image (with_media: false).
# Bootstrap the root so both invocation styles work; `-m tools.x_poster` is
# still the documented CLI.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
POST_LOG = ROOT / "journal" / "x_posts.jsonl"
MAX_LEN = 280


def _keys() -> dict | None:
    keys = {k: os.environ.get(k) for k in
            ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET")}
    return keys if all(keys.values()) else None


def journal_header(date=None) -> str:
    from datetime import datetime as _dt
    d = date or _dt.now()
    return f"East Equity Agent Journal - {d.strftime('%B %-d, %Y')}"


def format_for_x(text: str) -> str:
    """Standard X font throughout (user direction 2026-07-13): the old Unicode
    sans-bold/italic styling rendered as a non-native typeface on X, so all
    mathematical-alphanumeric conversion is gone. **markers** from the brain's
    memo are simply stripped to plain text. Platform rule kept: at most ONE
    cashtag per post — the first $TICK stays (hotlink), later ones lose the $."""
    out = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    seen = [0]

    def _decash(m):
        seen[0] += 1
        return m.group(0) if seen[0] == 1 else m.group(1)

    return re.sub(r"\$([A-Za-z]{1,5})\b", _decash, out)


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


def promote_draft_for_fill(run_id: str | None, order: dict | None = None,
                           fill: dict | None = None) -> str | None:
    """Promote a run's plain draft to a `_trade` draft when its order FILLS.

    WHY (2026-08-03, the BKR incident). publish.draft_x_summary writes the
    `_trade` suffix only when the RUN ITSELF carries fills — but a cloud run
    cannot reach Alpaca, so it QUEUES the order to state/order_intents.json and
    its fills list is empty. The Actions executor filled BKR hours later and
    wrote no draft at all. Net effect: every executor-filled trade left only a
    plain draft, the poster posts only `x_draft_*_trade.txt`, and the system's
    first real BUY in 18 days went unannounced. (Jul 15's DELL exit DID post —
    that fill happened inside the run, which is why the gap was invisible.)

    AND the plain draft's CONTENT is wrong for this purpose: the run wrote it
    while the order was merely queued, so the BKR run's own draft literally
    reads "No new trades today". A promoted copy of that text would announce a
    trade with a memo denying one. So: keep the source draft only when it
    already carries a fill line; otherwise COMPOSE the memo from the fill and
    the original proposal's thesis (journal/proposals/, keyed by proposal_id).

    Called from record_fill with the order+fill in hand. Idempotent, fail-soft:
    a posting defect must never be able to break order reconciliation.
    """
    if not run_id:
        return None
    try:
        src = ROOT / "state" / f"x_draft_{run_id}.txt"
        dst = ROOT / "state" / f"x_draft_{run_id}_trade.txt"
        if dst.exists():
            return str(dst)
        src_text = src.read_text().strip() if src.exists() else ""
        if re.search(r"\b(Opened|Closed)\b", src_text):
            dst.write_text(src_text + "\n")
            print(f"  promoted draft to trade memo: {dst.name}")
            return str(dst)
        composed = _compose_trade_memo(run_id, order or {}, fill or {})
        if not composed:
            return None
        dst.write_text(composed + "\n")
        print(f"  composed trade memo from fill: {dst.name}")
        return str(dst)
    except Exception as e:
        print(f"  (draft promotion skipped: {str(e)[:120]})")
        return None


def _find_proposal(run_id: str) -> dict | None:
    """The original proposal record for a run id (YYYYMMDD-hex), if journaled."""
    try:
        day = f"{run_id[:4]}-{run_id[4:6]}-{run_id[6:8]}"
        f = ROOT / "journal" / "proposals" / f"{day}.jsonl"
        if not f.exists():
            return None
        for line in f.read_text().splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("run_id") == run_id:
                return rec.get("proposal") or rec
    except Exception:
        pass
    return None


def _compose_trade_memo(run_id: str, order: dict, fill: dict) -> str | None:
    """A factual trade memo from the fill + the proposal's own thesis.

    Every number comes from the fill/order/proposal records — nothing invented.
    Format matches the brain's house style (poster strips ** and de-cashes
    extra tickers downstream).
    """
    ticker = str(fill.get("ticker") or order.get("ticker") or "").upper()
    action = str(fill.get("action") or order.get("action") or "").upper()
    price = fill.get("fill_price")
    if not ticker or not price:
        return None
    notional = fill.get("notional_usd") or order.get("position_size_usd")
    plan = order.get("plan") or {}
    prop = _find_proposal(run_id) or {}
    verb = "Opened" if action == "BUY" else "Closed"
    lines = [f"{verb} **{ticker}** ${ticker} at ${float(price):,.2f}"
             + (f" (${float(notional):,.0f} position)." if notional else ".")]
    stop, tgt, hor = (plan.get("stop_loss"), plan.get("target_price"),
                      plan.get("holding_horizon_days"))
    if action == "BUY" and stop and tgt:
        lines.append(f"Plan: stop ${float(stop):,.2f}, target ${float(tgt):,.2f}"
                     + (f", horizon {int(hor)}d." if hor else "."))
    thesis = str(prop.get("thesis") or order.get("exit_thesis") or "").strip()
    if thesis:
        # First two sentences of the brain's own thesis — its words, not a summary.
        parts = re.split(r"(?<=[.!?])\s+", thesis)
        lines.append(" ".join(parts[:2]))
    lines.append("This is a paper-trading experiment running in public, not advice.")
    return "\n\n".join(lines)


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
    from tools.envload import load_env
    load_env()
    process_drafts(dry_run="--dry-run" in sys.argv)
