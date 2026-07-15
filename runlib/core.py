"""Shared paths and pure helpers for East Equity run modules."""
from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def json_safe(obj):
    """Recursively replace NaN/Infinity floats with None for strict JSON."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    return obj


def et_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return datetime.now(timezone.utc) - timedelta(hours=4)


def et_date() -> str:
    return et_now().date().isoformat()


def to_et_date(iso_ts: str | None) -> str | None:
    if not iso_ts:
        return None
    try:
        dt = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
        try:
            from zoneinfo import ZoneInfo
            return dt.astimezone(ZoneInfo("America/New_York")).date().isoformat()
        except Exception:
            return (dt - timedelta(hours=4)).date().isoformat()
    except Exception:
        return str(iso_ts)[:10] if iso_ts else None


def light_prices(tickers: list) -> dict:
    try:
        from tools.prices import get_closes
        return get_closes(tickers)
    except Exception:
        return {}
