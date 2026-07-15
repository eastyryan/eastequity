"""Post-exit runner tracking — learn to hold winners (and when locking gains was right).

When a position closes, we keep marking the ticker for 15 / 30 / 60 calendar days
after the exit. Even if the exit was a process_win (stop, thesis, horizon), the
agent needs a second scoreboard:

  leftover_pct          = (price_now - exit_price) / exit_price
  max_extension_pct     = peak after exit vs exit price
  runner_verdict        = left_on_table | good_lock_in | mixed | still_tracking

So the system can say: "You were right to bank +12%, AND the stock ran another +18%
by day 30 — next time consider partials / trail."

Does NOT second-guess risk management on pure stop-outs into free-fall (those become
good_lock_in when price keeps falling). Focus learning is on *winners* (exit above cost)
while still tracking losers for bounce-after-stop patterns.

Storage: data/post_exit_runners.json
Mark windows: 15, 30, 60 days after exit_date.

Fail-soft; public APIs never raise.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNNERS_FILE = ROOT / "data" / "post_exit_runners.json"

MARK_DAYS = (15, 30, 60)
# How much further upside after exit counts as "left on the table"
LEFT_ON_TABLE_PCT = 8.0
# How much further downside after a winning exit counts as "good lock-in"
GOOD_LOCK_IN_PCT = -5.0
MAX_OPEN = 60
MAX_CLOSED = 150

# Headline keywords for attribution (pure classify — no network)
_CATALYST_KEYWORDS = (
    "earn", "guidance", "outlook", "upgrade", "downgrade", "price target",
    "analyst", "contract", "deal", "partnership", "acquisition", "merger",
    "fda", "approval", "lawsuit", "settlement", "dividend", "buyback",
    "offering", "secondary", "ipo", "split", "ceo", "cfo", "resign",
    "lay off", "layoff", "restructur", "ai ", "gpu", "datacenter", "data center",
    "hyperscaler", "nvidia", "microsoft", "amazon", "google", "meta ",
    "revenue", "profit", "beat", "miss", "raises", "cuts", "forecast",
    "china", "tariff", "sanction", "export", "ban", "order", "backlog",
)
_TECHNICAL_KEYWORDS = (
    "breakout", "breakdown", "resistance", "support", "technical",
    "moving average", "rsi", "chart", "short squeeze", "squeeze",
    "overbought", "oversold", "trend", "momentum", "rally continues",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today() -> str:
    return _now().date().isoformat()


def _parse_day(s) -> datetime | None:
    if not s:
        return None
    try:
        raw = str(s).replace("Z", "+00:00")
        if "T" in raw:
            return datetime.fromisoformat(raw)
        return datetime.strptime(raw[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _load() -> dict:
    if not RUNNERS_FILE.exists():
        return {"version": 1, "tracking": [], "completed": [], "updated_at": None}
    try:
        d = json.loads(RUNNERS_FILE.read_text())
        if not isinstance(d, dict):
            return {"version": 1, "tracking": [], "completed": [], "updated_at": None}
        d.setdefault("tracking", [])
        d.setdefault("completed", [])
        return d
    except Exception:
        return {"version": 1, "tracking": [], "completed": [], "updated_at": None}


def _save(state: dict) -> None:
    RUNNERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = _now().isoformat()
    RUNNERS_FILE.write_text(json.dumps(state, indent=2, default=str))


def register_from_exit(autopsy: dict) -> dict | None:
    """Start post-exit tracking from a graded exit autopsy.

    Tracks winners always; tracks losers optionally (for bounce-after-stop study).
    """
    try:
        if not isinstance(autopsy, dict):
            return None
        ticker = str(autopsy.get("ticker") or "").upper()
        exit_px = autopsy.get("fill_price")
        avg = autopsy.get("avg_cost")
        try:
            exit_px = float(exit_px) if exit_px is not None else None
            avg = float(avg) if avg is not None else None
        except (TypeError, ValueError):
            return None
        if not ticker or not exit_px or exit_px <= 0:
            return None

        # Prefer filled_at date; fall back to today
        exit_dt = _parse_day(autopsy.get("filled_at") or autopsy.get("ts")) or _now()
        exit_date = exit_dt.date().isoformat() if hasattr(exit_dt, "date") else str(exit_dt)[:10]

        gain_at_exit_pct = None
        if avg and avg > 0:
            gain_at_exit_pct = round((exit_px / avg - 1) * 100, 2)
        was_winner = gain_at_exit_pct is not None and gain_at_exit_pct > 0
        was_loser = gain_at_exit_pct is not None and gain_at_exit_pct < 0

        # Always track winners; track losers too (bounce study) but tag differently
        if not was_winner and not was_loser and gain_at_exit_pct is None:
            # Unknown cost — still track price path after exit
            track_kind = "unknown_basis"
        elif was_winner:
            track_kind = "winner"
        else:
            track_kind = "loser"

        state = _load()
        # Dedupe same ticker+exit_date
        for t in state["tracking"]:
            if t.get("ticker") == ticker and t.get("exit_date") == exit_date:
                return None

        rec = {
            "id": f"PX-{uuid.uuid4().hex[:10]}",
            "ticker": ticker,
            "exit_date": exit_date,
            "exit_price": round(exit_px, 4),
            "avg_cost": round(avg, 4) if avg else None,
            "gain_at_exit_pct": gain_at_exit_pct,
            "realized_pnl_usd": autopsy.get("realized_pnl_usd"),
            "track_kind": track_kind,
            "forced": bool(autopsy.get("forced")),
            "forced_reason": autopsy.get("forced_reason"),
            "process_grade": (autopsy.get("brain_grade") or {}).get("process"),
            "days_held": autopsy.get("days_held"),
            "entry_plan": autopsy.get("entry_plan"),
            "high_after_exit": round(exit_px, 4),
            "low_after_exit": round(exit_px, 4),
            "last_price": round(exit_px, 4),
            "marks": {},  # "15" -> {date, price, leftover_pct, ...}
            "status": "tracking",
            "runner_verdict": None,
            "hold_lesson": None,
        }
        state["tracking"].append(rec)
        if len(state["tracking"]) > MAX_OPEN:
            state["tracking"] = state["tracking"][-MAX_OPEN:]
        _save(state)
        return rec
    except Exception:
        return None


def _age_days(exit_date: str) -> int | None:
    ed = _parse_day(exit_date)
    if not ed:
        return None
    d0 = ed.date() if hasattr(ed, "date") else ed
    return max(0, (_now().date() - d0).days)


def classify_headline_for_attribution(title: str) -> str:
    """Return 'catalyst' | 'technical' | 'neutral' for one headline. Pure."""
    if not title or not isinstance(title, str):
        return "neutral"
    low = title.lower()
    cat = sum(1 for k in _CATALYST_KEYWORDS if k in low)
    tec = sum(1 for k in _TECHNICAL_KEYWORDS if k in low)
    if cat > tec and cat > 0:
        return "catalyst"
    if tec > cat and tec > 0:
        return "technical"
    if cat > 0:
        return "catalyst"
    return "neutral"


def classify_path_shape(marks: dict, max_ext: float) -> dict:
    """Infer technical continuation vs gap-like jump from 15/30/60 marks. Pure.

    steady_grind: upside built across windows (trend continuation)
    early_spike: most extension by day 15 (event-like)
    late_spike: little by 15, big by 30/60 (delayed catalyst or rotation)
    """
    marks = marks or {}
    def _lp(w):
        m = marks.get(str(w)) or marks.get(w)
        if isinstance(m, dict) and isinstance(m.get("leftover_pct"), (int, float)):
            return float(m["leftover_pct"])
        return None
    p15, p30, p60 = _lp(15), _lp(30), _lp(60)
    shape = "unknown"
    note = "Insufficient marks to classify path shape."
    if p15 is not None and p60 is not None:
        if p15 >= LEFT_ON_TABLE_PCT and (p60 - p15) < 5:
            shape = "early_spike"
            note = (f"Most post-exit extension present by day 15 ({p15:+.1f}%) — "
                    "event/gap-like move more than a slow grind.")
        elif p15 < LEFT_ON_TABLE_PCT / 2 and p60 >= LEFT_ON_TABLE_PCT:
            shape = "late_spike"
            note = (f"Little by day 15 ({p15:+.1f}%), large by day 60 ({p60:+.1f}%) — "
                    "delayed catalyst or multi-week technical trend.")
        elif p30 is not None and p15 is not None and p60 is not None:
            # monotonic-ish grind
            if p15 < p30 <= p60 + 2 or (p15 > 0 and p60 > p15):
                shape = "steady_grind"
                note = (f"Extension built over time (15d {p15:+.1f}% → 60d {p60:+.1f}%) — "
                        "classic technical trend continuation after exit.")
            else:
                shape = "choppy"
                note = "Post-exit path choppy across mark windows."
        else:
            shape = "steady_grind" if max_ext >= LEFT_ON_TABLE_PCT else "unknown"
            note = f"Peak extension +{max_ext:.1f}% with partial marks."
    elif p15 is not None and p15 >= LEFT_ON_TABLE_PCT:
        shape = "early_spike"
        note = f"Strong by day 15 ({p15:+.1f}%); later marks pending."
    return {"path_shape": shape, "note": note, "mark_leftovers": {
        "15": p15, "30": p30, "60": p60,
    }}


def attribute_left_on_table(
    *,
    headlines: list | None,
    path_shape: dict | None,
    max_extension_pct: float | None = None,
) -> dict:
    """Combine news + path shape → why leftover gains happened. Pure core.

    primary_driver: catalyst | technical | mixed | unknown
    """
    headlines = headlines or []
    cats, tecs, neutrals = [], [], []
    for h in headlines:
        title = h.get("title") if isinstance(h, dict) else str(h)
        published = h.get("published") if isinstance(h, dict) else None
        label = classify_headline_for_attribution(title or "")
        row = {"title": (title or "")[:160], "label": label, "published": published}
        if label == "catalyst":
            cats.append(row)
        elif label == "technical":
            tecs.append(row)
        else:
            neutrals.append(row)

    path = path_shape or {}
    shape = path.get("path_shape") or "unknown"

    # Decision logic
    if len(cats) >= 2 or (len(cats) >= 1 and shape in ("early_spike", "late_spike")):
        primary = "catalyst"
        conf = "medium" if len(cats) >= 2 else "low_medium"
    elif len(cats) >= 1 and len(tecs) >= 1:
        primary = "mixed"
        conf = "medium"
    elif shape == "steady_grind" and len(cats) == 0:
        primary = "technical"
        conf = "medium" if path.get("mark_leftovers", {}).get("60") else "low_medium"
    elif shape == "early_spike" and len(cats) == 0:
        primary = "unknown"  # spike without news in window — could be gap we didn't scrape
        conf = "low"
    elif len(cats) >= 1:
        primary = "catalyst"
        conf = "low_medium"
    elif shape in ("steady_grind", "choppy"):
        primary = "technical"
        conf = "low_medium"
    else:
        primary = "unknown"
        conf = "low"

    why = []
    if cats:
        why.append(f"{len(cats)} catalyst-tagged headline(s) in post-exit window "
                   f"(e.g. “{(cats[0].get('title') or '')[:80]}”).")
    if tecs:
        why.append(f"{len(tecs)} technical-tagged headline(s).")
    if path.get("note"):
        why.append(path["note"])
    if not why:
        why.append("No clear news in window and path shape inconclusive.")

    return {
        "primary_driver": primary,
        "confidence": conf,
        "path_shape": shape,
        "catalyst_headlines": cats[:5],
        "technical_headlines": tecs[:3],
        "neutral_headlines": neutrals[:3],
        "evidence": why,
        "note": (
            "Attribution for LEFT_ON_TABLE only. catalyst = news/earnings/deals/analysts; "
            "technical = trend/breakout grind without a clear news driver; mixed = both; "
            "unknown = not enough evidence (common when news feeds lack history)."
        ),
    }


def _cached_headlines_from_marks(rec: dict) -> list:
    """Flatten headlines captured on 15/30/60 mark days (learning data quality)."""
    out: list = []
    seen = set()
    marks = rec.get("marks") if isinstance(rec.get("marks"), dict) else {}
    for key in ("15", "30", "60"):
        m = marks.get(key) or {}
        for h in (m.get("headlines") or []):
            if not isinstance(h, dict):
                continue
            title = str(h.get("title") or "").strip()
            if not title or title in seen:
                continue
            seen.add(title)
            out.append(h)
    # Also accept a top-level cache written by learning_mark
    for h in (rec.get("headline_cache") or []):
        if not isinstance(h, dict):
            continue
        title = str(h.get("title") or "").strip()
        if not title or title in seen:
            continue
        seen.add(title)
        out.append(h)
    return out


def cache_headlines_for_ticker(ticker: str, exit_date: str | None = None,
                               max_items: int = 8) -> list:
    """Fetch + filter post-exit headlines for a mark day. Network fail-soft."""
    t = str(ticker or "").upper()
    if not t:
        return []
    try:
        from tools.news_catalysts import get_news_and_catalysts
        news = get_news_and_catalysts([t], max_headlines=max_items)
        block = ((news.get("tickers") or {}).get(t) or {})
        raw = block.get("headlines") or []
        exit_dt = _parse_day(exit_date)
        out = []
        for h in raw:
            if not isinstance(h, dict):
                continue
            pub = _parse_day(h.get("published"))
            if exit_dt and pub:
                if pub.date() < exit_dt.date():
                    continue
                if (pub.date() - exit_dt.date()).days > 70:
                    continue
            out.append({
                "title": str(h.get("title") or "")[:240],
                "published": h.get("published"),
                "source": h.get("source"),
                "cached_at": _today(),
            })
            if len(out) >= max_items:
                break
        return out
    except Exception:
        return []


def research_left_on_table_why(rec: dict) -> dict:
    """Attribute leftover gains using cached mark-day headlines first, then live fetch."""
    try:
        t = str(rec.get("ticker") or "").upper()
        exit_date = rec.get("exit_date")
        max_ext = rec.get("max_extension_pct")
        path = classify_path_shape(rec.get("marks") or {}, float(max_ext or 0))
        headlines: list = _cached_headlines_from_marks(rec)
        used_cache = bool(headlines)

        # Live fill-in when cache is thin (free feeds are recency-limited).
        if len(headlines) < 2:
            try:
                live = cache_headlines_for_ticker(t, exit_date)
                seen = {str(h.get("title") or "") for h in headlines}
                for h in live:
                    title = str(h.get("title") or "")
                    if title and title not in seen:
                        headlines.append(h)
                        seen.add(title)
            except Exception as e:
                path = {**path, "news_error": str(e)[:120]}

        attr = attribute_left_on_table(
            headlines=headlines, path_shape=path, max_extension_pct=max_ext)
        attr["ticker"] = t
        attr["exit_date"] = exit_date
        attr["max_extension_pct"] = max_ext
        attr["researched_at"] = _today()
        attr["n_headlines_considered"] = len(headlines)
        attr["headline_source"] = "mark_cache" if used_cache else "live_fetch"
        return attr
    except Exception as e:
        return {
            "primary_driver": "unknown",
            "confidence": "none",
            "error": str(e)[:150],
            "evidence": ["research failed"],
        }


def _score_runner(rec: dict, attribution: dict | None = None) -> dict:
    """Compute runner_verdict + hold_lesson from marks and high/low water.

    If attribution is provided (left_on_table research), fold it into the lesson.
    """
    exit_px = float(rec["exit_price"])
    high = float(rec.get("high_after_exit") or exit_px)
    low = float(rec.get("low_after_exit") or exit_px)
    max_ext = round((high / exit_px - 1) * 100, 2)
    max_fade = round((low / exit_px - 1) * 100, 2)
    kind = rec.get("track_kind")
    marks = rec.get("marks") or {}

    # Latest mark leftover
    latest_left = None
    for w in ("60", "30", "15"):
        if w in marks and isinstance(marks[w].get("leftover_pct"), (int, float)):
            latest_left = marks[w]["leftover_pct"]
            break

    verdict = "mixed"
    lesson = ""
    t = rec.get("ticker")
    g0 = rec.get("gain_at_exit_pct")

    if kind == "winner":
        if max_ext >= LEFT_ON_TABLE_PCT:
            verdict = "left_on_table"
            lesson = (
                f"{t}: exited winner"
                + (f" (+{g0}% from cost)" if g0 is not None else "")
                + f" then ran further +{max_ext}% peak after exit. "
                f"Process may still be fine — learn PARTIALS / trail: bank some, let rest run "
                f"while thesis invalidators remain intact."
            )
        elif max_fade <= GOOD_LOCK_IN_PCT:
            verdict = "good_lock_in"
            lesson = (
                f"{t}: exited winner"
                + (f" (+{g0}%)" if g0 is not None else "")
                + f" and price faded to {max_fade}% after — locking gains was right; "
                f"do not regret the exit."
            )
        elif latest_left is not None and latest_left >= LEFT_ON_TABLE_PCT:
            verdict = "left_on_table"
            lesson = (
                f"{t}: still +{latest_left}% above exit at last mark window — "
                f"consider how a trailing stop or partial would have captured more."
            )
        elif latest_left is not None and latest_left <= GOOD_LOCK_IN_PCT:
            verdict = "good_lock_in"
            lesson = (
                f"{t}: faded {latest_left}% after exit — good lock-in on the winner."
            )
        else:
            verdict = "mixed"
            lesson = (
                f"{t}: post-exit path mild (peak +{max_ext}%, fade {max_fade}%). "
                f"Neither clear leave-on-table nor clear giveback."
            )
    elif kind == "loser":
        # After stop: did it keep falling (good stop) or bounce hard (stopped out early)?
        if max_fade <= -8:
            verdict = "good_stop_continued"
            lesson = (
                f"{t}: after loss exit, price kept falling (to {max_fade}% below exit) — "
                f"stop/exit protected capital."
            )
        elif max_ext >= 10:
            verdict = "stopped_then_recovered"
            lesson = (
                f"{t}: after loss exit, price recovered +{max_ext}% above exit — "
                f"possible noise stop or early invalidation; review stop width vs ATR."
            )
        else:
            verdict = "mixed"
            lesson = f"{t}: post-loss path mixed (peak +{max_ext}%, fade {max_fade}%)."
    else:
        verdict = "mixed"
        lesson = f"{t}: post-exit peak +{max_ext}%, fade {max_fade}%."

    # --- Left-on-table WHY (catalyst vs technical) ---
    attr = attribution
    if verdict == "left_on_table" and not attr:
        # Pure path-only attribution if research not passed in
        path = classify_path_shape(marks, max_ext)
        attr = attribute_left_on_table(headlines=[], path_shape=path,
                                       max_extension_pct=max_ext)

    if verdict == "left_on_table" and isinstance(attr, dict):
        driver = attr.get("primary_driver") or "unknown"
        why_bits = "; ".join(attr.get("evidence") or [])[:220]
        if driver == "catalyst":
            lesson += (
                f" WHY LEFT ON TABLE: CATALYST-DRIVEN — news/event after exit "
                f"({why_bits}). Next time: check catalyst calendar before full exit; "
                f"prefer partial into known event windows."
            )
        elif driver == "technical":
            lesson += (
                f" WHY LEFT ON TABLE: TECHNICAL CONTINUATION — trend/structure after exit "
                f"({why_bits}). Next time: if trend + invalidators still clear, trail "
                f"or sell_fraction instead of full close."
            )
        elif driver == "mixed":
            lesson += (
                f" WHY LEFT ON TABLE: MIXED catalyst + technical ({why_bits}). "
                f"Partials fit both event risk and trend."
            )
        else:
            lesson += (
                f" WHY LEFT ON TABLE: unclear driver ({why_bits}). "
                f"Log for review; default to partials on strong trends."
            )

    out = {
        "max_extension_pct": max_ext,
        "max_fade_pct": max_fade,
        "latest_leftover_pct": latest_left,
        "runner_verdict": verdict,
        "hold_lesson": lesson[:700],
    }
    if isinstance(attr, dict) and verdict == "left_on_table":
        out["left_on_table_attribution"] = attr
    return out


def mark_post_exit_runners(prices: dict, *, cache_news_on_marks: bool = True) -> dict:
    """Mark open post-exit tracks; complete when 60d mark filled or horizon passed.

    When cache_news_on_marks is True (default), each NEW 15/30/60 mark stores a
    snapshot of company headlines so left-on-table attribution later is not
    hostage to recency-limited news feeds.
    """
    try:
        state = _load()
        still = []
        completed = []
        news_cached = 0
        for rec in state.get("tracking") or []:
            rec = dict(rec)
            t = rec.get("ticker")
            px = prices.get(t)
            age = _age_days(rec.get("exit_date"))
            if age is None:
                still.append(rec)
                continue

            if isinstance(px, (int, float)) and px > 0:
                exit_px = float(rec["exit_price"])
                rec["last_price"] = round(float(px), 4)
                rec["last_mark_at"] = _today()
                rec["high_after_exit"] = round(
                    max(float(rec.get("high_after_exit") or exit_px), float(px)), 4)
                rec["low_after_exit"] = round(
                    min(float(rec.get("low_after_exit") or exit_px), float(px)), 4)
                leftover = round((float(px) / exit_px - 1) * 100, 2)
                rec["current_leftover_pct"] = leftover

                for w in MARK_DAYS:
                    key = str(w)
                    if age >= w and key not in (rec.get("marks") or {}):
                        mark = {
                            "window_days": w,
                            "marked_at": _today(),
                            "price": round(float(px), 4),
                            "leftover_pct": leftover,
                            "high_to_date_pct": round(
                                (float(rec["high_after_exit"]) / exit_px - 1) * 100, 2),
                            "low_to_date_pct": round(
                                (float(rec["low_after_exit"]) / exit_px - 1) * 100, 2),
                        }
                        if cache_news_on_marks:
                            heads = cache_headlines_for_ticker(
                                t, rec.get("exit_date"))
                            if heads:
                                mark["headlines"] = heads
                                news_cached += 1
                                # Rolling top-level cache for attribution
                                cache = list(rec.get("headline_cache") or [])
                                seen = {str(h.get("title") or "") for h in cache}
                                for h in heads:
                                    title = str(h.get("title") or "")
                                    if title and title not in seen:
                                        cache.append(h)
                                        seen.add(title)
                                rec["headline_cache"] = cache[-24:]
                        rec.setdefault("marks", {})[key] = mark

            # Complete after 60d mark exists, or age > 60+7 grace
            marks = rec.get("marks") or {}
            if "60" in marks or (age is not None and age >= 67):
                pre = _score_runner(rec, attribution=None)
                attr = None
                if pre.get("runner_verdict") == "left_on_table":
                    # Research WHY leftover: catalyst news vs technical continuation
                    attr = research_left_on_table_why({**rec, **pre})
                scored = _score_runner(rec, attribution=attr)
                rec.update(scored)
                rec["status"] = "completed"
                rec["completed_at"] = _today()
                completed.append(rec)
            else:
                # Live interim score for brain (optional)
                if age is not None and age >= 15:
                    interim = _score_runner(rec)
                    rec["runner_verdict_interim"] = interim.get("runner_verdict")
                    rec["hold_lesson_interim"] = interim.get("hold_lesson")
                    rec["max_extension_pct"] = interim.get("max_extension_pct")
                    if (interim.get("runner_verdict") == "left_on_table"
                            and not rec.get("left_on_table_attribution")):
                        try:
                            attr = research_left_on_table_why({**rec, **interim})
                            rec["left_on_table_attribution"] = attr
                            rec["hold_lesson_interim"] = _score_runner(
                                rec, attribution=attr).get("hold_lesson")
                        except Exception:
                            pass
                still.append(rec)

        state["tracking"] = still
        if completed:
            state.setdefault("completed", []).extend(completed)
            state["completed"] = state["completed"][-MAX_CLOSED:]
            # Feed concept memory for left_on_table winners (+ why catalyst/technical)
            try:
                from tools.concept_memory import append_lesson
                for c in completed:
                    if c.get("hold_lesson") and c.get("ticker"):
                        tags = ["post_exit_runner", c.get("runner_verdict") or ""]
                        attr = c.get("left_on_table_attribution") or {}
                        if c.get("runner_verdict") == "left_on_table":
                            tags.append("let_winners_run")
                            if attr.get("primary_driver"):
                                tags.append(f"why_{attr['primary_driver']}")
                        append_lesson(
                            c["ticker"], c["hold_lesson"],
                            source="post_exit_runner", tags=tags,
                        )
            except Exception:
                pass
        _save(state)
        return {
            "tracking": len(still),
            "completed_now": len(completed),
            "news_cached": news_cached,
        }
    except Exception as e:
        return {"status": "error", "reason": str(e)[:150]}


def brain_facing_runner_learning(limit: int = 12) -> dict:
    """For context: how to run winners + completed post-exit scores."""
    try:
        state = _load()
        completed = list(reversed(state.get("completed") or []))[:80]
        tracking = list(state.get("tracking") or [])

        left = [c for c in completed if c.get("runner_verdict") == "left_on_table"][:limit]
        locked = [c for c in completed
                  if c.get("runner_verdict") in ("good_lock_in", "good_stop_continued")][:limit]
        recovered = [c for c in completed
                     if c.get("runner_verdict") == "stopped_then_recovered"][:limit]

        open_interesting = []
        for t in tracking:
            if t.get("track_kind") != "winner":
                continue
            ext = t.get("max_extension_pct")
            if ext is None and t.get("high_after_exit") and t.get("exit_price"):
                try:
                    ext = round(
                        (float(t["high_after_exit"]) / float(t["exit_price"]) - 1) * 100, 2)
                except Exception:
                    ext = None
            if ext is not None and ext >= 5:
                open_interesting.append({
                    "ticker": t.get("ticker"),
                    "exit_date": t.get("exit_date"),
                    "exit_price": t.get("exit_price"),
                    "gain_at_exit_pct": t.get("gain_at_exit_pct"),
                    "max_extension_pct": ext,
                    "marks": t.get("marks"),
                    "hold_lesson_interim": t.get("hold_lesson_interim"),
                })
        open_interesting.sort(key=lambda x: float(x.get("max_extension_pct") or 0), reverse=True)

        n_done = len(state.get("completed") or [])
        n_left = len([c for c in (state.get("completed") or [])
                      if c.get("runner_verdict") == "left_on_table"])
        binding = n_done >= 5 and n_left >= 2

        return {
            "note": (
                "POST-EXIT RUNNER LEARNING (15/30/60d after every close). "
                "Even a correct exit can leave upside on the table — that is not always a mistake, "
                "but winners that keep running teach PARTIALS and trails: bank some, let the rest "
                "work while thesis invalidators are intact. "
                "left_on_table = price ran further after you sold a winner. "
                "good_lock_in = price faded after you sold — banking was right. "
                "stopped_then_recovered = loss exit then bounce (possible noise stop). "
                + ("Binding: when similar winners print, consider scale-out / trail language."
                   if binding else
                   f"Warming up ({n_done} completed tracks, {n_left} left-on-table).")
            ),
            "binding": binding,
            "mark_windows_days": list(MARK_DAYS),
            "n_tracking": len(tracking),
            "n_completed": n_done,
            "left_on_table": [
                {
                    "ticker": c.get("ticker"),
                    "exit_date": c.get("exit_date"),
                    "gain_at_exit_pct": c.get("gain_at_exit_pct"),
                    "max_extension_pct": c.get("max_extension_pct"),
                    "marks": c.get("marks"),
                    "hold_lesson": c.get("hold_lesson"),
                    "process_grade": c.get("process_grade"),
                    # catalyst | technical | mixed | unknown
                    "why_left_on_table": (
                        (c.get("left_on_table_attribution") or {}).get("primary_driver")
                    ),
                    "attribution": c.get("left_on_table_attribution"),
                }
                for c in left
            ],
            "good_lock_ins": [
                {
                    "ticker": c.get("ticker"),
                    "exit_date": c.get("exit_date"),
                    "gain_at_exit_pct": c.get("gain_at_exit_pct"),
                    "max_fade_pct": c.get("max_fade_pct"),
                    "hold_lesson": c.get("hold_lesson"),
                }
                for c in locked
            ],
            "stopped_then_recovered": [
                {
                    "ticker": c.get("ticker"),
                    "exit_date": c.get("exit_date"),
                    "max_extension_pct": c.get("max_extension_pct"),
                    "hold_lesson": c.get("hold_lesson"),
                }
                for c in recovered
            ],
            "open_winners_still_running": open_interesting[:8],
            "stats": {
                "left_on_table_rate_pct": round(
                    100 * n_left / max(1, n_done), 1) if n_done else None,
                "completed": n_done,
            },
            "hold_winners_playbook": {
                "when_left_on_table_repeats": (
                    "Use sell_fraction (e.g. 0.4–0.6) to bank, trail rest with structure "
                    "(raise stop under higher lows / 50-DMA) while invalidators clear."
                ),
                "when_why_is_catalyst": (
                    "Leftover gains were news/event-driven after exit — before full exits, "
                    "check earnings/catalyst calendar; prefer partials into known events."
                ),
                "when_why_is_technical": (
                    "Leftover gains were trend continuation without a clear new catalyst — "
                    "if structure + invalidators still clear, trail instead of full close."
                ),
                "when_good_lock_in_repeats": (
                    "Full exits into strength then fade are fine — do not force trails "
                    "when leadership is rolling over."
                ),
                "never": (
                    "Never ignore stops or invalidators just to 'let it run' — that is "
                    "hope. Running winners means planned partials, not deleting risk."
                ),
            },
        }
    except Exception as e:
        return {
            "status": "error", "reason": str(e)[:150],
            "left_on_table": [], "good_lock_ins": [], "binding": False,
        }


if __name__ == "__main__":
    print(json.dumps(brain_facing_runner_learning(), indent=2))
