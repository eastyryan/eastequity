"""Persisted ATR% map for chandelier trail + stop gap model between gathers.

The scanner writes atr_by_ticker into the in-memory bundle, but light /
evening depths leave it empty and stop_watch historically read a file that
never existed. Persist the last good map to state/atr_map.json whenever a
gather produces one, and read it back when the live bundle is empty so the
trail degrades loudly instead of silently.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ATR_FILE = ROOT / "state" / "atr_map.json"


def persist_atr_map(atr_by_ticker: dict | None, *, source: str = "universe_scan") -> int:
    """Write a non-empty atr map. Returns number of tickers persisted. Fail-soft."""
    atr = {str(k).upper(): float(v)
           for k, v in (atr_by_ticker or {}).items()
           if v is not None}
    if not atr:
        return 0
    try:
        ATR_FILE.parent.mkdir(parents=True, exist_ok=True)
        ATR_FILE.write_text(json.dumps({
            "as_of": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "n": len(atr),
            "atr_by_ticker": atr,
        }, indent=2))
    except Exception:
        return 0
    return len(atr)


def load_atr_map() -> dict:
    """{TICKER: atr_pct} from state/atr_map.json, or {}."""
    try:
        blob = json.loads(ATR_FILE.read_text())
        atr = (blob or {}).get("atr_by_ticker") or {}
        return {str(k).upper(): float(v) for k, v in atr.items() if v is not None}
    except Exception:
        return {}


def ensure_atr_on_scan(scan: dict | None) -> dict:
    """If scan.atr_by_ticker is empty, backfill from the persisted map.
    Persist whenever the scan already has values. Returns the atr map in use.
    """
    scan = scan if isinstance(scan, dict) else {}
    atr = scan.get("atr_by_ticker") or {}
    if atr:
        persist_atr_map(atr, source=str(scan.get("status") or "universe_scan"))
        return {str(k).upper(): v for k, v in atr.items()}
    saved = load_atr_map()
    if saved:
        scan["atr_by_ticker"] = saved
        scan.setdefault(
            "atr_map_note",
            "atr_by_ticker backfilled from state/atr_map.json (last full/mini scan); "
            "trail can ratchet but figures may be a session stale",
        )
    return saved
