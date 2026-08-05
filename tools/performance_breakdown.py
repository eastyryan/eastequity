"""Performance Breakdown — turn raw closed trades into a learning signal.

Slices the agent's closed-trade history along the dimensions that matter for
calibration: sector, stated confidence, holding period, exit verdict, and exit
month. Each bucket reports trade count, wins, win rate, total P&L, and average
R-multiple, so the brain can weight future confidence with its own evidence
instead of vibes. Feeds the track_record section of the context bundle.

All lookups are fail-soft: unknown tickers land in "off_universe", trades whose
original proposal cannot be found land in confidence bucket "unknown".
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_NOTE = ("Your own results by category - weight your confidence using this "
         "evidence. Rates are WITHHELD (null) below the sample floor - counts "
         "are facts, a win rate off two trades is not.")

_HOLDING_BUCKETS = (("0-7d", 0, 7), ("8-21d", 8, 21),
                    ("22-45d", 22, 45), ("46-90d", 46, 90))

# SAMPLE FLOOR. Below this many trades in a bucket, win_rate_pct and avg_r are
# None rather than a number.
#
# The live publish was `by_sector: servers_hardware, trades: 2, win_rate_pct: 0.0`
# — on a PUBLIC dashboard, and read by tools/calibration_gate.py, which has the
# power to demand a calibration_exception paragraph before a trade may enter a
# "losing" bucket. Two losing trades is not a 0% win rate; it is two losing
# trades. Publishing the rate turned noise into a gate.
#
# Kept EQUAL to calibration_gate.DEFAULTS["min_bucket_trades"] and to
# runlib/analytics.MIN_BUCKET_TRADES: the producer of these buckets and the gate
# that reads them must agree about what counts as evidence, or one will suppress
# a number the other still acts on. tests/test_truth_layer.py pins the equality.
MIN_BUCKET_TRADES = 5

# Phase language mirrors the Learning Protocol's sample-size rules.
_CAUTION_AT = 5
_BINDING_AT = 15


# ---------------------------------------------------------------------------
# Lookups (fail-soft)
# ---------------------------------------------------------------------------
def _sector_map() -> dict:
    """Ticker -> sector from data/universe.json (first occurrence wins)."""
    mapping: dict = {}
    try:
        sectors = json.loads((ROOT / "data" / "universe.json").read_text())["sectors"]
    except Exception:
        return mapping
    for sector, tickers in sectors.items():
        for t in tickers:
            mapping.setdefault(t.upper(), sector)
    return mapping


def _confidence_lookup() -> list:
    """All BUY proposals from the journal as (ts, ticker, confidence) tuples."""
    rows = []
    proposals_dir = ROOT / "journal" / "proposals"
    if not proposals_dir.exists():
        return rows
    for f in sorted(proposals_dir.glob("*.jsonl")):
        for line in f.read_text().splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            p = rec.get("proposal", {})
            if (str(p.get("action", "")).upper() == "BUY" and p.get("ticker")
                    and p.get("confidence") is not None):
                rows.append((rec.get("ts", ""), p["ticker"].upper(),
                             float(p["confidence"])))
    return rows


def _trade_confidence(trade: dict, proposals: list) -> float | None:
    """Confidence from the latest BUY proposal for this ticker on or before
    the trade's opened_at date. None if no match (fail-soft)."""
    ticker = str(trade.get("ticker", "")).upper()
    opened = str(trade.get("opened_at") or "")[:10]
    if not ticker or not opened:
        return None
    best_ts, best_conf = "", None
    for ts, t, conf in proposals:
        if t == ticker and ts[:10] <= opened and ts >= best_ts:
            best_ts, best_conf = ts, conf
    return best_conf


# ---------------------------------------------------------------------------
# Bucket assignment
# ---------------------------------------------------------------------------
def _confidence_bucket(conf: float | None) -> str:
    if conf is None:
        return "unknown"
    if conf >= 0.70:
        return "0.70+"
    if conf >= 0.65:
        return "0.65-0.70"
    if conf >= 0.60:
        return "0.60-0.65"
    if conf >= 0.50:
        return "0.50-0.60"  # calibration-probe lane fills (see autonomy_config)
    return "unknown"


def _holding_bucket(days_held) -> str:
    try:
        days = int(days_held)
    except (TypeError, ValueError):
        return _HOLDING_BUCKETS[0][0]
    for name, lo, hi in _HOLDING_BUCKETS:
        if lo <= days <= hi:
            return name
    return _HOLDING_BUCKETS[-1][0]  # beyond 90d: clamp into the last bucket


def _summarize(groups: dict, order: list | None = None) -> list:
    """Turn {bucket: [trades]} into the standard stats rows.

    COUNTS ARE FACTS and always survive: trades, wins and total_pnl_usd are
    observations, and suppressing them would hide the very evidence the rate was
    computed from. Only the DERIVED statistics — win_rate_pct and avg_r — go None
    below MIN_BUCKET_TRADES, each row carrying sufficient_sample and
    min_trades_for_rate so a consumer can tell "not enough data" from "zero".
    """
    keys = [k for k in (order or sorted(groups)) if k in groups]
    keys += [k for k in sorted(groups) if k not in keys]
    rows = []
    for bucket in keys:
        trades = groups[bucket]
        pnls = [float(t.get("pnl_usd") or 0) for t in trades]
        wins = sum(1 for p in pnls if p > 0)
        rs = [float(t["r_multiple"]) for t in trades
              if t.get("r_multiple") is not None]
        enough = len(trades) >= MIN_BUCKET_TRADES
        rows.append({
            "bucket": bucket,
            "trades": len(trades),
            "wins": wins,
            "win_rate_pct": (round(wins / len(trades) * 100, 1)
                             if enough and trades else None),
            "total_pnl_usd": round(sum(pnls), 2),
            "avg_r": round(sum(rs) / len(rs), 2) if (enough and rs) else None,
            "sufficient_sample": enough,
            "min_trades_for_rate": MIN_BUCKET_TRADES,
        })
    return rows


def _sample_phase(n: int) -> str:
    """anecdote | caution | binding — the Learning Protocol's own vocabulary."""
    if n >= _BINDING_AT:
        return "binding"
    if n >= _CAUTION_AT:
        return "caution"
    return "anecdote"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def build_performance_breakdown(closed_trades: list[dict]) -> dict:
    """Slice closed trades (from orchestrator.compute_closed_trades) into
    per-category performance buckets the brain can learn from."""
    if not closed_trades:
        return {"total_trades": 0, "note": _NOTE, "sufficient_sample": False,
                "sample_phase": "anecdote",
                "min_trades_for_rate": MIN_BUCKET_TRADES}

    sectors = _sector_map()
    proposals = _confidence_lookup()

    by_sector: dict = {}
    by_confidence: dict = {}
    by_holding: dict = {}
    by_verdict: dict = {}
    by_month: dict = {}
    for t in closed_trades:
        ticker = str(t.get("ticker", "")).upper()
        by_sector.setdefault(sectors.get(ticker, "off_universe"), []).append(t)
        by_confidence.setdefault(
            _confidence_bucket(_trade_confidence(t, proposals)), []).append(t)
        by_holding.setdefault(_holding_bucket(t.get("days_held")), []).append(t)
        by_verdict.setdefault(str(t.get("verdict") or "unknown"), []).append(t)
        by_month.setdefault(str(t.get("closed_at") or "")[:7] or "unknown",
                            []).append(t)

    return {
        "total_trades": len(closed_trades),
        "note": _NOTE,
        "sufficient_sample": len(closed_trades) >= MIN_BUCKET_TRADES,
        "sample_phase": _sample_phase(len(closed_trades)),
        "min_trades_for_rate": MIN_BUCKET_TRADES,
        "by_sector": _summarize(by_sector),
        "by_confidence": _summarize(
            by_confidence,
            ["0.50-0.60", "0.60-0.65", "0.65-0.70", "0.70+", "unknown"]),
        "by_holding_period": _summarize(
            by_holding, [b[0] for b in _HOLDING_BUCKETS]),
        "by_verdict": _summarize(by_verdict),
        "by_exit_month": _summarize(by_month),
    }


def render_breakdown_markdown(breakdown: dict) -> str:
    """Compact markdown tables of the breakdown (for dashboard/report use)."""
    total = breakdown.get("total_trades", 0)
    phase = breakdown.get("sample_phase") or _sample_phase(int(total or 0))
    lines = [f"## Performance breakdown ({total} closed trades)"]
    if not total:
        lines.append("\nNo closed trades yet - nothing to break down.")
        return "\n".join(lines)
    lines.append(f"\nSample phase: {phase} "
                 f"(rates shown only for buckets with >= {MIN_BUCKET_TRADES} trades; "
                 f"'-' means not enough sample, NOT zero)")
    titles = [("by_sector", "By sector"), ("by_confidence", "By confidence"),
              ("by_holding_period", "By holding period"),
              ("by_verdict", "By verdict"), ("by_exit_month", "By exit month")]
    for key, title in titles:
        rows = breakdown.get(key) or []
        if not rows:
            continue
        lines.append(f"\n### {title}\n")
        lines.append("| Bucket | Trades | Wins | Win % | P&L $ | Avg R |")
        lines.append("|---|---|---|---|---|---|")
        for r in rows:
            avg_r = "-" if r.get("avg_r") is None else f"{r['avg_r']:+.2f}"
            # A suppressed rate renders as "-", never "0.0%". The dashboard is
            # public and 0.0% is a claim about skill; "-" is a claim about sample.
            wr = ("-" if r.get("win_rate_pct") is None
                  else f"{r['win_rate_pct']:.1f}%")
            lines.append(
                f"| {r['bucket']} | {r['trades']} | {r['wins']} "
                f"| {wr} | {r['total_pnl_usd']:+,.2f} "
                f"| {avg_r} |")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(ROOT))
    import orchestrator
    b = build_performance_breakdown(orchestrator.compute_closed_trades())
    print(json.dumps(b, indent=2))
    print(render_breakdown_markdown(b))
