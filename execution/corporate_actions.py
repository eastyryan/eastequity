"""Corporate actions for the paper portfolio — dividends and splits.

The simulated broker fills orders and marks to market but ignores corporate
actions, so a long-held paper position silently loses dividend income and a
split would make quantity/avg_cost nonsense against real quotes. This module
reconciles both from yfinance, once per position per event, using an
"actions_processed_through" marker on each position so nothing is ever
credited twice. Fail-soft by design: a bad ticker or a yfinance outage must
never block a trading run, so per-ticker errors are reported, not raised.
"""

from __future__ import annotations

from datetime import datetime, timezone

from execution import simulated_broker


def _cutoff(pos: dict) -> datetime:
    """Latest of opened_at and the actions_processed_through marker (tz-aware)."""
    opened = datetime.fromisoformat(pos["opened_at"])
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=timezone.utc)
    marker = pos.get("actions_processed_through")
    if marker:
        m = datetime.fromisoformat(marker)
        if m.tzinfo is None:
            m = m.replace(tzinfo=timezone.utc)
        return max(opened, m)
    return opened


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
    changed = False

    for pos in state["positions"]:
        ticker = pos["ticker"].upper()
        try:
            cutoff = _cutoff(pos)
            tk = yf.Ticker(ticker)

            # Dividends: cash credit per share held at the event.
            divs = tk.dividends
            for ts, per_share in (divs.items() if divs is not None and len(divs) else []):
                event_dt = ts.to_pydatetime()
                if event_dt.tzinfo is None:
                    event_dt = event_dt.replace(tzinfo=timezone.utc)
                if event_dt <= cutoff or event_dt > now:
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
                          "date": event_dt.date().isoformat(),
                          "processed_at": now.isoformat()}
                state["history"].append(record)
                summary["events"].append(record)
                summary["dividends_credited_usd"] = round(
                    summary["dividends_credited_usd"] + amount, 2)
                changed = True

            # Splits: quantity scales up, avg_cost scales down; value unchanged.
            splits = tk.splits
            for ts, ratio in (splits.items() if splits is not None and len(splits) else []):
                event_dt = ts.to_pydatetime()
                if event_dt.tzinfo is None:
                    event_dt = event_dt.replace(tzinfo=timezone.utc)
                if event_dt <= cutoff or event_dt > now or not float(ratio):
                    continue
                ratio = float(ratio)
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
                          "date": event_dt.date().isoformat(),
                          "processed_at": now.isoformat()}
                state["history"].append(record)
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
