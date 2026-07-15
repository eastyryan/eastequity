"""Dedicated learning mark job — shadow book + post-exit runners + lesson hygiene.

Run on a quiet schedule (e.g. evening) so learning quality does not depend on
trading cycles:

    python orchestrator.py --learning-mark
    python -m tools.learning_mark

Steps
-----
1. Price held shadows + open post-exit runners (yfinance / prices helper)
2. On new 15/30/60 mark days, cache company headlines for left-on-table WHY
3. Prune/cap adopted lessons (near-dup + max_active, superseded_by)

Fail-soft; never raises from run_learning_mark.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _tickers_needing_marks() -> list[str]:
    """Union of open shadow tickers + open post-exit runner tickers + open book."""
    tickers: set[str] = set()
    try:
        from tools.shadow_portfolio import _load as load_shadow
        for p in (load_shadow().get("positions") or []):
            if isinstance(p, dict) and p.get("ticker"):
                tickers.add(str(p["ticker"]).upper())
    except Exception:
        pass
    try:
        from tools.post_exit_runners import _load as load_runners
        for p in (load_runners().get("tracking") or []):
            if isinstance(p, dict) and p.get("ticker"):
                tickers.add(str(p["ticker"]).upper())
    except Exception:
        pass
    try:
        from tools.portfolio_state import get_portfolio_state
        for p in (get_portfolio_state().get("positions") or []):
            if isinstance(p, dict) and p.get("ticker"):
                tickers.add(str(p["ticker"]).upper())
    except Exception:
        pass
    return sorted(tickers)


def _fetch_prices(tickers: list[str]) -> dict:
    if not tickers:
        return {}
    try:
        from tools.prices import get_closes
        return get_closes(tickers) or {}
    except Exception:
        # Last resort: yfinance one-by-one
        out = {}
        try:
            import yfinance as yf
            for t in tickers:
                try:
                    px = yf.Ticker(t).fast_info.get("lastPrice") or yf.Ticker(t).fast_info.get("last_price")
                    if isinstance(px, (int, float)) and px > 0:
                        out[t] = float(px)
                except Exception:
                    continue
        except Exception:
            pass
        return out


def run_learning_mark(run_id: str | None = None,
                      prices: dict | None = None,
                      *,
                      cache_news: bool = True,
                      prune: bool = True,
                      max_active_lessons: int = 40) -> dict:
    """Full learning mark pass. Safe to call mid-cycle or standalone."""
    run_id = run_id or f"learn-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"
    out: dict = {
        "run_id": run_id,
        "ts": datetime.now(timezone.utc).isoformat(),
        "status": "ok",
    }
    try:
        tickers = _tickers_needing_marks()
        out["tickers"] = tickers
        px = prices if prices is not None else _fetch_prices(tickers)
        out["n_prices"] = len(px)

        # --- shadows ---
        try:
            from tools.shadow_portfolio import mark_shadows
            sh = mark_shadows(px)
            out["shadow"] = sh if isinstance(sh, dict) else {"result": sh}
        except Exception as e:
            out["shadow"] = {"status": "error", "reason": str(e)[:150]}

        # --- post-exit runners (+ headline cache on new marks) ---
        try:
            from tools.post_exit_runners import mark_post_exit_runners
            pr = mark_post_exit_runners(px, cache_news_on_marks=cache_news)
            out["post_exit_runners"] = pr
        except Exception as e:
            out["post_exit_runners"] = {"status": "error", "reason": str(e)[:150]}

        # --- lesson prune ---
        if prune:
            try:
                from tools.learning_adopt import prune_adopted_lessons
                out["lesson_prune"] = prune_adopted_lessons(
                    max_active=max_active_lessons)
            except Exception as e:
                out["lesson_prune"] = {"status": "error", "reason": str(e)[:150]}
        else:
            out["lesson_prune"] = {"skipped": True}

        # Persist a small audit trail
        try:
            path = ROOT / "state" / f"learning_mark_{run_id}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(out, indent=2, default=str))
            out["audit_path"] = str(path)
        except Exception:
            pass
        return out
    except Exception as e:
        out["status"] = "error"
        out["reason"] = str(e)[:200]
        return out


if __name__ == "__main__":
    result = run_learning_mark()
    print(json.dumps(result, indent=2, default=str))
    sys.exit(0 if result.get("status") != "error" else 1)
