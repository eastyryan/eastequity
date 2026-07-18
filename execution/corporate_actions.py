"""Corporate actions for the paper portfolio — dividends and splits.

The simulated broker fills orders and marks to market but ignores corporate
actions, so a long-held paper position silently loses dividend income and a
split would make quantity/avg_cost nonsense against real quotes. This module
reconciles both from yfinance. Fail-soft by design: a bad ticker or a yfinance
outage must never block a trading run, so per-ticker errors are reported, not
raised.

IDEMPOTENCY (rewritten 2026-07-18 after an audit reproduced a double-apply):

The old guard was a date-string marker, "actions_processed_through", parsed by
datetime.fromisoformat into MIDNIGHT UTC. yfinance returns action timestamps in
America/New_York, so an ex-date event resolved to 04:00 UTC > 00:00 UTC cutoff and
the guard never fired on the ex-date itself. With 6-7 slots a day the event
re-applied on every run: quantity 10 -> 160 and cost basis 50 -> 6.25 over four
runs in simulation, and on the live Alpaca path the persisted plan stop rescaled
90 -> 5.6, leaving the position effectively UNSTOPPED. The docstring claimed
"nothing is ever credited twice" while the opposite was true.

The guard is now a PER-EVENT KEY, "{type}:{ticker}:{date}", derived from the
records already written to state["history"] - the durable event trail that existed
all along but carried no stable identity. The marker survives only as reporting
metadata (runlib/publish.py reads it); it is deliberately NOT a suppression window
any more. A marker that advanced past an unprocessed event would lose that event
permanently, whereas re-scanning the full series and deduping precisely cannot.
The scan floor is therefore opened_at alone, which also makes a
closed-then-reopened position pick up only events from its own holding period.
"""

from __future__ import annotations

from datetime import datetime, timezone

from execution import simulated_broker

# A split outside this band is a vendor error, not a corporate action. yfinance is
# an unofficial scraper with no schema guarantee, and a split write is the only
# mutation in this system that PERMANENTLY rewrites share count, cost basis, and
# the enforced stop with no second source to reconcile against.
MIN_SPLIT_RATIO, MAX_SPLIT_RATIO = 0.01, 100.0


def _position_floor(pos: dict) -> datetime:
    """Earliest event this position may claim: its own opening time (tz-aware).

    Deliberately NOT max(opened_at, marker) — see the module docstring. Precise
    per-event dedupe replaced the suppression window.
    """
    opened = datetime.fromisoformat(pos["opened_at"])
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=timezone.utc)
    return opened


def _event_key(event_type: str, ticker: str, day: str) -> str:
    """Stable identity for one corporate action. Pure.

    Date-granular is correct: a security cannot have two dividends or two splits
    with the same ex-date, and the date is the one field both yfinance and the
    existing history records already agree on.
    """
    return f"{event_type}:{str(ticker).upper()}:{day}"


def processed_event_keys(state: dict) -> set:
    """Keys for every corporate action already applied, read from state["history"].

    History is append-only and persisted, so this survives restarts and makes the
    guard durable rather than dependent on an in-memory marker. Pure.
    """
    keys = set()
    for rec in (state.get("history") or []):
        rtype, ticker, day = rec.get("type"), rec.get("ticker"), rec.get("date")
        if rtype in ("dividend", "split") and ticker and day:
            keys.add(_event_key(rtype, ticker, day))
    return keys


def _event_day(ts) -> str | None:
    """ISO date of a yfinance action timestamp, normalized to UTC. None if unusable.

    Compare on the DATE, never the datetime: the vendor's tz (America/New_York) and
    the marker's implied tz (UTC) are what produced the original defect.
    """
    try:
        event_dt = ts.to_pydatetime()
    except Exception:
        return None
    if event_dt.tzinfo is None:
        event_dt = event_dt.replace(tzinfo=timezone.utc)
    return event_dt.date().isoformat()


def apply_corporate_actions() -> dict:
    """Credit dividends and apply splits for events since each position opened.

    Returns {"dividends_credited_usd", "splits_applied", "events", "errors"}.
    Never raises: per-ticker failures land in "errors".
    """
    summary = {"dividends_credited_usd": 0.0, "splits_applied": 0,
               "events": [], "errors": []}
    state = simulated_broker._load()
    if not state.get("positions"):
        return summary

    # Backend split of responsibilities: the SIMULATION owns cash and share
    # counts, so dividends credit cash and splits rescale the position here.
    # On a REAL broker backend (alpaca_paper), the broker owns cash/qty — a
    # sync would overwrite anything we credited (and Alpaca paper pays no
    # dividends at all), so we only ATTRIBUTE dividend income as metadata
    # (per-trade reporting) and rescale the persisted PLAN levels on splits
    # (the broker rescales the position, never our stops).
    from execution import broker as broker_router
    sim_owns_ledger = broker_router.backend_name() == "simulation"

    try:
        import yfinance as yf
    except Exception as e:  # yfinance missing/broken — report, don't block the run
        summary["errors"].append({"ticker": None, "error": f"yfinance unavailable: {e}"})
        return summary

    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    today_d = now.date()
    changed = False

    # Durable dedupe set, rebuilt from the persisted event trail on every call.
    seen = processed_event_keys(state)

    for pos in state["positions"]:
        ticker = pos["ticker"].upper()
        try:
            floor_day = _position_floor(pos).date()
            tk = yf.Ticker(ticker)

            # Dividends: cash credit per share held at the event.
            divs = tk.dividends
            for ts, per_share in (divs.items() if divs is not None and len(divs) else []):
                day = _event_day(ts)
                if day is None:
                    continue
                day_d = datetime.fromisoformat(day).date()
                # Date-granular comparison on both ends — the tz mismatch between the
                # vendor (ET) and the old UTC marker is exactly what double-applied.
                if day_d <= floor_day or day_d > today_d:
                    continue
                key = _event_key("dividend", ticker, day)
                if key in seen:
                    continue
                amount = round(pos["quantity"] * float(per_share), 2)
                if sim_owns_ledger:
                    state["cash_usd"] = round(state["cash_usd"] + amount, 2)
                # Attribute the dividend to the position for per-trade P&L
                # reporting on close. Additive + idempotent: guarded by the same
                # cutoff as the cash credit, so it accrues exactly once per event.
                pos["dividends_received_usd"] = round(
                    float(pos.get("dividends_received_usd", 0.0) or 0.0) + amount, 2)
                record = {"type": "dividend", "ticker": ticker,
                          "per_share": float(per_share),
                          "quantity": pos["quantity"], "amount_usd": amount,
                          "cash_credited": sim_owns_ledger,
                          "date": day,
                          "processed_at": now.isoformat()}
                state["history"].append(record)
                seen.add(key)   # guard within this run too, not just across runs
                summary["events"].append(record)
                summary["dividends_credited_usd"] = round(
                    summary["dividends_credited_usd"] + amount, 2)
                changed = True

            # Splits: quantity scales up, avg_cost scales down; value unchanged.
            splits = tk.splits
            for ts, ratio in (splits.items() if splits is not None and len(splits) else []):
                day = _event_day(ts)
                if day is None or not float(ratio):
                    continue
                day_d = datetime.fromisoformat(day).date()
                if day_d <= floor_day or day_d > today_d:
                    continue
                key = _event_key("split", ticker, day)
                if key in seen:
                    continue
                ratio = float(ratio)
                # Refuse implausible ratios rather than permanently rewriting share
                # count, cost basis and the enforced stop from a single unverified
                # vendor row. Reported as an error so it is visible, not swallowed.
                if not (MIN_SPLIT_RATIO <= ratio <= MAX_SPLIT_RATIO):
                    summary["errors"].append({
                        "ticker": ticker,
                        "error": f"implausible split ratio {ratio} on {day} — refused"})
                    continue
                if sim_owns_ledger:  # a real broker rescales its own position
                    pos["quantity"] = round(pos["quantity"] * ratio, 4)
                    pos["avg_cost"] = round(pos["avg_cost"] / ratio, 4)
                    if pos.get("last_price"):
                        pos["last_price"] = round(pos["last_price"] / ratio, 4)
                # The persisted plan's PRICE levels must scale too, or the exit
                # guard compares post-split prices against pre-split stops and
                # force-closes a healthy position as "stop_loss_breached".
                plan = pos.get("plan")
                if isinstance(plan, dict):
                    for key in ("stop_loss", "target_price", "entry_price_max"):
                        if isinstance(plan.get(key), (int, float)):
                            plan[key] = round(plan[key] / ratio, 4)
                record = {"type": "split", "ticker": ticker, "ratio": ratio,
                          "quantity": pos["quantity"], "avg_cost": pos["avg_cost"],
                          "date": day,
                          "processed_at": now.isoformat()}
                state["history"].append(record)
                seen.add(key)
                summary["events"].append(record)
                summary["splits_applied"] += 1
                changed = True

            if pos.get("actions_processed_through") != today:
                pos["actions_processed_through"] = today
                changed = True
        except Exception as e:
            summary["errors"].append({"ticker": ticker, "error": str(e)})

    if changed:
        state["total_equity_usd"] = round(
            state["cash_usd"] + sum(p["market_value_usd"] for p in state["positions"]), 2)
        simulated_broker._save(state)
    return summary


if __name__ == "__main__":
    import json
    print(json.dumps(apply_corporate_actions(), indent=2, default=str))
