"""Build / refresh data/discovery_candidates.json from free index sources.

API / free-tier policy (East Equity keys as of 2026-07)
-------------------------------------------------------
This module is intentionally KEYLESS for bulk work:

  * NEVER uses Alpaca market data for constituent downloads or history
    (Alpaca free/paper rate limits are for live prices + brokerage, not a
    1k-name nightly download).
  * NEVER uses Polygon / Finnhub (optional keys; not required; bulk would
    exhaust free tiers).
  * NEVER uses FRED (macro only).
  * NEVER uses X API.

Sources (all free / public HTML or CSV):
  * Wikipedia S&P 500 and Nasdaq-100 tables (with GitHub CSV fallback for SPX)
  * iShares IWB (Russell 1000 ETF) public holdings CSV when reachable
  * data/em_adr_seed.json (curated liquid EM/international ADRs)
  * Merge of the previous discovery_candidates.json (keep survivors)

Liquidity and $1B floors are NOT applied here (that would need per-name
market data APIs). tools.discovery_screen drops illiquid names weekly via
yfinance OHLCV only — one weekly batched pass, chunked with sleeps.

CLI:
  python -m tools.discovery_pool              # build + write
  python -m tools.discovery_pool --dry-run    # print stats, no write
  python -m tools.discovery_pool --offline    # merge seeds only (no network)
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "data" / "discovery_candidates.json"
EM_SEED_PATH = ROOT / "data" / "em_adr_seed.json"

# Levered / inverse / single-stock daily products never enter discovery.
_BANNED = frozenset({
    "TQQQ", "SQQQ", "SOXL", "SOXS", "SPXL", "SPXU", "UPRO", "SDOW", "UDOW",
    "TNA", "TZA", "FAS", "FAZ", "LABU", "LABD", "NAIL", "CURE", "DRN", "DRV",
    "TSLL", "NVDL", "NVDS", "AAPB", "MSFX", "CONL", "MSTX", "NVDX",
})

_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,6}$")
_UA = {
    "User-Agent": (
        "EastEquityAgent/1.0 (discovery pool builder; easton.ryan@hws.edu; "
        "research; +https://github.com/)"
    )
}

# Coarse sector buckets used for Sunday diversity caps (Wikipedia GICS-ish).
_TECH_ADJACENT = frozenset({
    "Information Technology", "Technology", "Communication Services",
    "Communications", "Telecommunications",
})


def normalize_ticker(raw: str) -> str | None:
    """BRK.B / BRK-B → BRK-B style for yfinance. Pure."""
    if not raw:
        return None
    t = str(raw).strip().upper().replace(".", "-")
    t = re.sub(r"\s+", "", t)
    if t in _BANNED:
        return None
    if not _TICKER_RE.match(t):
        return None
    # Drop obvious non-equities
    if t.endswith("W") and len(t) >= 5:  # many warrant suffixes — keep short names
        pass
    return t


def merge_meta(
    base: dict[str, dict],
    tickers: Iterable[str],
    *,
    source: str,
    sector: str | None = None,
    sleeve: str = "us",
    country: str | None = None,
) -> None:
    """In-place union into meta map. Pure aside from mutation of `base`."""
    for raw in tickers:
        t = normalize_ticker(raw)
        if not t:
            continue
        m = base.setdefault(t, {
            "sources": [],
            "sector": None,
            "sleeve": sleeve,
            "country": country,
        })
        if source and source not in m["sources"]:
            m["sources"].append(source)
        if sector and not m.get("sector"):
            m["sector"] = sector
        if sleeve == "em_adr":
            m["sleeve"] = "em_adr"
        if country and not m.get("country"):
            m["country"] = country


# ---------------------------------------------------------------------------
# Free network sources (no API keys)
# ---------------------------------------------------------------------------

def _http_get(url: str, timeout: float = 30.0) -> str:
    import requests
    r = requests.get(url, headers=_UA, timeout=timeout)
    r.raise_for_status()
    return r.text


def _read_html_tables(html: str):
    """pandas.read_html with StringIO (pandas 2.x)."""
    import pandas as pd
    return pd.read_html(io.StringIO(html))


def _pairs_from_symbol_table(df, min_rows: int = 50) -> list[tuple[str, str | None]]:
    """Extract (ticker, sector) from a constituents DataFrame. Pure-ish."""
    sym_col = next((c for c in df.columns
                    if "symbol" in str(c).lower() or str(c).lower() == "ticker"
                    or "ticker" in str(c).lower()), None)
    if sym_col is None:
        return []
    sec_col = next((c for c in df.columns
                    if "gics" in str(c).lower() and "sector" in str(c).lower()), None)
    if sec_col is None:
        sec_col = next((c for c in df.columns if str(c).lower() == "sector"), None)
    out = []
    for _, row in df.iterrows():
        t = normalize_ticker(str(row[sym_col]))
        if not t:
            continue
        sec = None
        if sec_col is not None:
            sec = str(row[sec_col]).strip()
            if sec in ("nan", "None", "", "-"):
                sec = None
        out.append((t, sec))
    return out if len(out) >= min_rows else []


def fetch_wikipedia_constituents(
    url: str,
    *,
    min_rows: int,
    label: str,
) -> tuple[list[tuple[str, str | None]], str]:
    """Generic Wikipedia constituents table picker (largest qualifying table)."""
    html = _http_get(url)
    tables = _read_html_tables(html)
    best_pairs: list[tuple[str, str | None]] = []
    for df in tables:
        pairs = _pairs_from_symbol_table(df, min_rows=min_rows)
        if len(pairs) > len(best_pairs):
            best_pairs = pairs
    if best_pairs:
        return best_pairs, label
    return [], f"{label}_unavailable"


def fetch_sp500_wikipedia() -> tuple[list[tuple[str, str | None]], str]:
    """Return ([(ticker, sector)], source_label)."""
    pairs, label = fetch_wikipedia_constituents(
        "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        min_rows=400,
        label="wikipedia_sp500",
    )
    if pairs:
        return pairs, label
    # Fallback: GitHub datasets CSV (raw)
    csv_url = (
        "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/"
        "main/data/constituents.csv"
    )
    text = _http_get(csv_url)
    reader = csv.DictReader(io.StringIO(text))
    out = []
    for row in reader:
        t = normalize_ticker(row.get("Symbol") or row.get("symbol") or "")
        if not t:
            continue
        sec = (row.get("GICS Sector") or row.get("Sector") or "").strip() or None
        out.append((t, sec))
    return out, "github_sp500_csv"


def fetch_ndx() -> tuple[list[tuple[str, str | None]], str]:
    """Nasdaq-100: try Wikipedia tables, else offline seed file."""
    for url in (
        "https://en.wikipedia.org/wiki/Nasdaq-100",
        "https://en.wikipedia.org/wiki/NASDAQ-100",
    ):
        try:
            pairs, label = fetch_wikipedia_constituents(
                url, min_rows=80, label="wikipedia_ndx")
            if pairs:
                return pairs, label
        except Exception:
            continue
    # Offline seed (Wikipedia dropped the full table from the main page)
    seed = ROOT / "data" / "ndx_seed.json"
    if seed.exists():
        try:
            doc = json.loads(seed.read_text())
            out = []
            for t in doc.get("tickers") or []:
                nt = normalize_ticker(t)
                if nt:
                    out.append((nt, None))
            if len(out) >= 80:
                return out, "ndx_seed_json"
        except Exception:
            pass
    return [], "ndx_unavailable"


def fetch_russell1000() -> tuple[list[tuple[str, str | None]], str]:
    """Russell 1000 constituents from Wikipedia (public table ~1000 rows).

    iShares IWB CSV is often JS/HTML-gated without a browser; Wikipedia is free
    and keyless.
    """
    pairs, label = fetch_wikipedia_constituents(
        "https://en.wikipedia.org/wiki/Russell_1000_Index",
        min_rows=700,
        label="wikipedia_r1000",
    )
    if pairs:
        return pairs, label
    return [], "r1000_unavailable"


def load_em_seed() -> list[tuple[str, str | None, str | None]]:
    """[(ticker, sector_theme, country)] from seed file. Offline."""
    if not EM_SEED_PATH.exists():
        return []
    try:
        doc = json.loads(EM_SEED_PATH.read_text())
    except Exception:
        return []
    out = []
    for row in doc.get("tickers") or []:
        if isinstance(row, str):
            t = normalize_ticker(row)
            if t:
                out.append((t, None, None))
            continue
        if not isinstance(row, dict):
            continue
        t = normalize_ticker(row.get("ticker") or "")
        if not t:
            continue
        out.append((t, row.get("theme"), row.get("country")))
    return out


def load_existing_pool() -> tuple[list[str], dict[str, dict]]:
    if not OUT_PATH.exists():
        return [], {}
    try:
        doc = json.loads(OUT_PATH.read_text())
    except Exception:
        return [], {}
    tickers = [normalize_ticker(t) for t in doc.get("tickers") or []]
    tickers = [t for t in tickers if t]
    meta = {}
    raw_meta = doc.get("meta") or {}
    if isinstance(raw_meta, dict):
        for k, v in raw_meta.items():
            t = normalize_ticker(k)
            if t and isinstance(v, dict):
                meta[t] = {
                    "sources": list(v.get("sources") or ["prior_pool"]),
                    "sector": v.get("sector"),
                    "sleeve": v.get("sleeve") or "us",
                    "country": v.get("country"),
                }
    for t in tickers:
        meta.setdefault(t, {
            "sources": ["prior_pool"],
            "sector": None,
            "sleeve": "us",
            "country": None,
        })
    return tickers, meta


def build_pool(*, offline: bool = False) -> dict:
    """Assemble the discovery candidate pool. Network optional."""
    meta: dict[str, dict] = {}
    sources_status: dict[str, str] = {}

    prior_tickers, prior_meta = load_existing_pool()
    for t, m in prior_meta.items():
        meta[t] = dict(m)
    if prior_tickers:
        sources_status["prior_pool"] = f"ok:{len(prior_tickers)}"

    if not offline:
        try:
            pairs, label = fetch_sp500_wikipedia()
            merge_meta(meta, [t for t, _ in pairs], source="sp500", sleeve="us")
            for t, sec in pairs:
                if sec and t in meta and not meta[t].get("sector"):
                    meta[t]["sector"] = sec
            sources_status["sp500"] = f"ok:{len(pairs)} via {label}"
        except Exception as e:
            sources_status["sp500"] = f"error:{type(e).__name__}"
        time.sleep(0.8)

        try:
            pairs, label = fetch_ndx()
            merge_meta(meta, [t for t, _ in pairs], source="ndx", sleeve="us")
            for t, sec in pairs:
                if sec and t in meta and not meta[t].get("sector"):
                    meta[t]["sector"] = sec
            sources_status["ndx"] = f"ok:{len(pairs)} via {label}"
        except Exception as e:
            sources_status["ndx"] = f"error:{type(e).__name__}"
        time.sleep(0.8)

        try:
            pairs, label = fetch_russell1000()
            merge_meta(meta, [t for t, _ in pairs], source="r1000", sleeve="us")
            for t, sec in pairs:
                if sec and t in meta and not meta[t].get("sector"):
                    meta[t]["sector"] = sec
            sources_status["r1000"] = f"ok:{len(pairs)} via {label}"
        except Exception as e:
            sources_status["r1000"] = f"error:{type(e).__name__}"
    else:
        sources_status["network"] = "skipped_offline"

    # EM ADR sleeve (always; offline-safe)
    em = load_em_seed()
    for t, theme, country in em:
        merge_meta(meta, [t], source="em_adr_seed", sleeve="em_adr",
                   sector=theme, country=country)
    sources_status["em_adr_seed"] = f"ok:{len(em)}"

    tickers = sorted(meta.keys())
    n_em = sum(1 for t in tickers if (meta[t].get("sleeve") == "em_adr"))
    n_with_sector = sum(1 for t in tickers if meta[t].get("sector"))

    doc = {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m"),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "note": (
            "Broad discovery pool for the weekly discovery screen "
            "(tools/discovery_screen.py). Built by tools/discovery_pool.py from "
            "FREE public sources only (Wikipedia SPX + Russell 1000, NDX seed/Wikipedia, "
            "em_adr_seed, prior pool). No Alpaca/Polygon/Finnhub/FRED bulk calls. "
            "Universe members are INCLUDED on purpose; discovery_screen excludes them "
            "at runtime. Liquidity ($20M ADV) and tradeability are enforced at screen "
            "time via yfinance OHLCV (keyless), not here. No levered/inverse products. "
            "Refresh monthly or after major index rebalances: "
            "`python -m tools.discovery_pool`."
        ),
        "api_policy": {
            "bulk_history": "yfinance only (weekly discovery_screen, chunked)",
            "pool_build": "wikipedia + ishares csv + local seeds (no paid keys)",
            "never": ["alpaca_mass_history", "polygon_bulk", "finnhub_bulk"],
        },
        "sources_status": sources_status,
        "stats": {
            "n_tickers": len(tickers),
            "n_em_adr_sleeve": n_em,
            "n_with_sector": n_with_sector,
        },
        "tickers": tickers,
        "meta": meta,
    }
    return doc


def write_pool(doc: dict, path: Path = OUT_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=1, sort_keys=False) + "\n")
    return path


def load_pool_meta() -> dict[str, dict]:
    """Helper for discovery_screen: {TICKER: meta}."""
    if not OUT_PATH.exists():
        return {}
    try:
        doc = json.loads(OUT_PATH.read_text())
        meta = doc.get("meta") or {}
        return {str(k).upper(): v for k, v in meta.items() if isinstance(v, dict)}
    except Exception:
        return {}


if __name__ == "__main__":
    import sys
    dry = "--dry-run" in sys.argv
    offline = "--offline" in sys.argv
    print(f"building discovery pool (offline={offline})...")
    doc = build_pool(offline=offline)
    stats = doc.get("stats") or {}
    print(json.dumps({
        "stats": stats,
        "sources_status": doc.get("sources_status"),
        "sample": (doc.get("tickers") or [])[:12],
    }, indent=2))
    if dry:
        print("dry-run: not written")
    else:
        p = write_pool(doc)
        print(f"wrote {p} ({stats.get('n_tickers')} tickers)")
