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

CLI: python -m tools.x_poster [--dry-run] [--force-window] [--draft PATH]

BOX-RUN (2026-10-08). GitHub's cron for x-post.yml was delivered hours late
(every run since 09-24 started after 19:00 ET), so the 16:00-18:59 window
silently rejected every trade day from 10-02 on. The box is now the poster:
scripts/post_x_daily.sh runs this module at ~16:30 ET on weekdays and commits
journal/x_posts.jsonl. Skips now print a one-line reason instead of exiting
silently. --force-window bypasses ONLY the clock gate (manual catch-ups); the
duplicate guard (journal/x_posts.jsonl) is never bypassed. --draft posts one
specific file (e.g. state/x_catchup/20261002.txt) as kind "catchup", which
does not count toward the one-trade-post-per-day policy.
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
    try:
        r = session.post("https://api.twitter.com/2/tweets", json=payload, timeout=60)
    except Exception as e:
        # The request may have been accepted before the connection died. Report
        # "unknown" so the caller logs it as consumed: never blind-retry a post.
        return {"status": "unknown", "error": type(e).__name__,
                "with_media": "media" in payload}
    if r.status_code in (200, 201):
        return {"status": "posted", "tweet_id": r.json().get("data", {}).get("id"),
                "with_media": "media" in payload}
    return {"status": "error", "code": r.status_code, "body": r.text[:300]}


# A log entry with one of these statuses CONSUMES its draft: it never posts
# again. "unknown" = the POST request may have reached X but we never saw the
# response (timeout / connection drop) - treat as posted, never auto-retry.
# "error" records (e.g. a 402 with no credits) are logged for audit but do NOT
# consume, so the draft can post once the cause is fixed. Legacy rows without
# a status (and July's dry_run rows) stay consumed, as before.
_CONSUMING = {"posted", "superseded", "unknown", "dry_run", None}
_COUNTS_AS_POSTED = {"posted", "unknown"}


def _log_records() -> list[dict]:
    if not POST_LOG.exists():
        return []
    out = []
    for line in POST_LOG.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _already_posted() -> set[str]:
    return {r.get("draft") for r in _log_records()
            if r.get("status") in _CONSUMING}


def _et_day_of(ts: str | None) -> str | None:
    from tools.et_time import to_et_date
    d = to_et_date(ts)
    return d.strftime("%Y%m%d") if d else None


def _summary_posted_today(today: str) -> bool:
    """today = YYYYMMDD in ET."""
    return any(r.get("kind") == "summary" and _et_day_of(r.get("ts")) == today
               for r in _log_records())


def _posted_today(today: str) -> bool:
    """True when the DAILY policy post (trade/summary) already went out today.

    today = YYYYMMDD in ET (was a UTC date: a post after 20:00 ET landed on the
    next UTC day). Catch-up and manual posts do not count toward the daily one.
    """
    return any(r.get("status") in _COUNTS_AS_POSTED
               and r.get("kind") in ("trade", "summary")
               and _et_day_of(r.get("ts")) == today
               for r in _log_records())


def _append_log(record: dict) -> None:
    POST_LOG.parent.mkdir(parents=True, exist_ok=True)
    with POST_LOG.open("a") as f:
        f.write(json.dumps(record) + "\n")


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
        if not _DATED_RUN_ID.match(str(run_id)):
            # Undated pseudo run ids (2026-10-08 fix). "out-of-band" is a REAL
            # broker-side fill (a resting stop); it used to write the undated
            # x_draft_out-of-band_trade.txt, which (a) the poster's same-day
            # filter could never match and (b) the early return below then
            # reused forever - the 09-18 HPE memo sat there while the 10-07
            # ILMN stop-out got no memo at all. Date-stamp it per day+ticker.
            # ghost-reconcile / not-at-broker-writeoff are ledger repairs,
            # not trades: no public memo.
            if str(run_id) != "out-of-band" and not (order or {}).get("out_of_band"):
                return None
            return _promote_out_of_band(order or {}, fill or {})
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


_DATED_RUN_ID = re.compile(r"^\d{8}-")


def _fill_et_day(order: dict, fill: dict) -> str:
    """YYYYMMDD (ET) of the fill, falling back to today in ET."""
    for ts in (fill.get("filled_at"), order.get("submitted_at"),
               fill.get("submitted_at")):
        d = _et_day_of(ts) if ts else None
        if d:
            return d
    from tools.et_time import et_now
    return et_now().strftime("%Y%m%d")


def _promote_out_of_band(order: dict, fill: dict) -> str | None:
    ticker = re.sub(r"[^A-Z0-9.]", "",
                    str(fill.get("ticker") or order.get("ticker") or "").upper())
    if not ticker:
        return None
    day = _fill_et_day(order, fill)
    dst = ROOT / "state" / f"x_draft_{day}-oob-{ticker}_trade.txt"
    if dst.exists():          # idempotent per day+ticker (same fill re-seen)
        return str(dst)
    composed = _compose_trade_memo(f"{day}-oob-{ticker}", order, fill)
    if not composed:
        return None
    dst.write_text(composed + "\n")
    print(f"  composed out-of-band trade memo from fill: {dst.name}")
    return str(dst)


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
    if action != "BUY":
        reason = str(order.get("forced_exit_reason") or fill.get("forced_exit_reason") or "")
        bits = []
        if reason == "resting_stop_breached":
            bits.append("The resting stop at the broker filled.")
        pnl = fill.get("realized_pnl_usd")
        if isinstance(pnl, (int, float)):
            sign = "-" if pnl < 0 else "+"
            bits.append(f"Realized P&L {sign}${abs(float(pnl)):,.2f}.")
        if bits:
            lines.append(" ".join(bits))
    thesis = str(prop.get("thesis") or order.get("exit_thesis") or "").strip()
    if thesis:
        # First two sentences of the brain's own thesis — its words, not a summary.
        parts = re.split(r"(?<=[.!?])\s+", thesis)
        lines.append(" ".join(parts[:2]))
    lines.append("This is a paper-trading experiment running in public, not advice.")
    return "\n\n".join(lines)


WINDOW_START_HOUR, WINDOW_END_HOUR = 16, 18     # 16:00-18:59 ET inclusive


def _skip(reason: str) -> list[dict]:
    """Print a one-line skip reason (never a secret) and post nothing."""
    print(json.dumps({"status": "skipped", "reason": reason}))
    return []


def _trade_times(day: str) -> dict[str, str]:
    """run_id -> latest journaled fill ts for an ET day (YYYYMMDD).

    Out-of-band fills are keyed as "<day>-oob-<TICKER>" to match their drafts.
    """
    f = ROOT / "journal" / "trades" / f"{day[:4]}-{day[4:6]}-{day[6:8]}.jsonl"
    out: dict[str, str] = {}
    if not f.exists():
        return out
    for line in f.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        rid = str(rec.get("run_id") or "")
        if rid == "out-of-band":
            tk = str((rec.get("fill") or {}).get("ticker")
                     or (rec.get("order") or {}).get("ticker") or "").upper()
            rid = f"{day}-oob-{tk}"
        ts = str(rec.get("ts") or "")
        if rid and ts > out.get(rid, ""):
            out[rid] = ts
    return out


def _is_thin(text: str) -> bool:
    """A bare template / probe line rather than a real memo (e.g. the 10-02 CLS
    'swing update' stub or the 10-05 OKTA 'proposing a probe' one-liner)."""
    t = text.strip()
    return len(t) < 250 or t.startswith("East Equity Agent swing update")


def pick_daily_draft(drafts: list[Path], day: str) -> Path:
    """Newest-wins by FILL time (journal/trades), not by the random hex in the
    filename (sorted() made 'latest' an alphabetical accident). A real memo
    beats a thin template; ties fall back to file mtime, then name."""
    times = _trade_times(day)

    def key(d: Path):
        rid = d.name[len("x_draft_"):-len("_trade.txt")]
        text = d.read_text()
        return (not _is_thin(text), times.get(rid, ""), d.stat().st_mtime, d.name)

    return max(drafts, key=key)


def _post_one(draft: Path, kind: str, dry_run: bool) -> dict:
    text = journal_header() + "\n\n" + format_for_x(draft.read_text().strip())
    chart = None
    try:
        from tools.chart_card import render_equity_card
        chart = str(render_equity_card())
    except Exception as e:
        print(json.dumps({"chart_card_error": str(e)[:200]}))
    if dry_run:
        rec = {"status": "dry_run", "chars": len(text), "media": chart,
               "text": text}
        print(json.dumps(rec))
        return rec        # dry runs are NOT logged: logging consumed the draft
    result = post_tweet(text, media_path=chart)
    try:
        key = str(draft.relative_to(ROOT / "state"))
    except ValueError:
        key = draft.name
    record = {"ts": datetime.now(timezone.utc).isoformat(), "draft": key,
              "kind": kind, **result}
    if result.get("status") in ("posted", "unknown", "error"):
        _append_log(record)
    print(json.dumps(record))
    return record


def process_drafts(dry_run: bool = False, force_window: bool = False,
                   draft: str | None = None) -> list[dict]:
    """User policy: at most ONE post per day, in the market-close window
    (4:00-6:59pm ET), and ONLY if a trade was made that day. The post is the
    day's best trade memo (newest real memo by fill time); everything else
    stays on the dashboard. `draft` posts one explicit file as a catch-up."""
    from tools.et_time import et_now
    now = et_now()
    today = now.strftime("%Y%m%d")
    posted = _already_posted()

    if not force_window and not (WINDOW_START_HOUR <= now.hour <= WINDOW_END_HOUR):
        return _skip(f"outside the 16:00-18:59 ET window (now {now:%H:%M} ET); "
                     "use --force-window for a manual catch-up")

    if draft:
        path = Path(draft)
        if not path.is_absolute():
            path = ROOT / path
        if not path.exists() or not path.read_text().strip():
            return _skip(f"draft not found or empty: {draft}")
        try:
            key = str(path.relative_to(ROOT / "state"))
        except ValueError:
            key = path.name
        if key in posted or path.name in posted:
            return _skip(f"{key} is already in journal/x_posts.jsonl (duplicate guard)")
        return [_post_one(path, "catchup", dry_run)]

    if _posted_today(today):
        return _skip(f"already posted the daily trade memo for {today}")

    todays = [d for d in (ROOT / "state").glob("x_draft_*_trade.txt")
              if d.name.startswith(f"x_draft_{today}-") and d.name not in posted
              and d.read_text().strip()]
    if not todays:
        return _skip(f"no same-day trade draft for {today}; nothing to post")

    best = pick_daily_draft(todays, today)
    record = _post_one(best, "trade", dry_run)
    if record.get("status") in ("posted", "unknown"):
        # Retire the day's other trade drafts so they never post late.
        for d in todays:
            if d != best:
                _append_log({"ts": record["ts"], "draft": d.name,
                             "kind": "trade", "status": "superseded"})
    return [record]


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m tools.x_poster")
    ap.add_argument("--dry-run", action="store_true",
                    help="render and print, never call X, never log")
    ap.add_argument("--force-window", action="store_true",
                    help="ignore the 16:00-18:59 ET gate (manual catch-ups)")
    ap.add_argument("--draft", help="post one specific draft file as a catch-up")
    args = ap.parse_args(argv)
    from tools.envload import load_env
    load_env()
    results = process_drafts(dry_run=args.dry_run, force_window=args.force_window,
                             draft=args.draft)
    # Non-zero ONLY when X was called and did not confirm success; a skip
    # (no draft, outside window, duplicate) is a clean 0.
    return 2 if any(r.get("status") in ("error", "unknown") for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
