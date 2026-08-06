"""Shared paths and pure helpers for East Equity run modules."""
from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path

from dotenv import load_dotenv

# CONSOLIDATED 2026-08-05 (audit): the ET calendar-day helpers lived as three
# divergent copies (here, runlib/analytics.py — which imported these names and
# then silently SHADOWED them with local redefinitions — and private copies in
# validator.py). tools/et_time.py is now the single source; these re-exports
# keep every existing `from runlib.core import et_now/et_date/to_et_date`
# caller working unchanged. Behavior note: naive timestamps are now treated as
# UTC everywhere (this module's old copy treated them as machine-local, which
# answered differently on the UTC cloud runner vs the operator's ET Mac);
# to_et_date keeps the legacy string contract including the first-10-chars
# salvage on unparseable input. et_time is stdlib-only, so this import adds no
# dependency weight.
from tools.et_time import et_now, et_date, to_et_date_str as to_et_date  # noqa: F401

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


def light_prices(tickers: list) -> dict:
    try:
        from tools.prices import get_closes
        return get_closes(tickers)
    except Exception:
        return {}
