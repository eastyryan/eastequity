"""Network helper: direct fetch with automatic fallback through our Vercel proxy.

Cloud trading runs execute in a sandbox that blocks most external hosts (SEC,
FRED) but can reach our own dashboard domain. These helpers try the real host
first and transparently fall back to the proxy routes in dashboard/app/api/.
"""

from __future__ import annotations

import os
import time

import requests

PROXY_BASE = os.environ.get("DATA_PROXY_BASE", "https://east-equity-agent.vercel.app/api")
SEC_HEADERS = {"User-Agent": "East Equity Agent easton.ryan@hws.edu"}


def get_sec(url: str) -> requests.Response:
    """GET a sec.gov URL, falling back to the Vercel proxy if blocked."""
    time.sleep(0.15)  # stay well under SEC's 10 req/s limit
    try:
        r = requests.get(url, headers=SEC_HEADERS, timeout=30)
        r.raise_for_status()
        return r
    except requests.RequestException:
        r = requests.get(f"{PROXY_BASE}/sec", params={"url": url}, timeout=45)
        r.raise_for_status()
        return r


def get_fred_observations(series_id: str, observation_start: str) -> list[dict]:
    """FRED observations via the proxy (used when direct fredapi access fails)."""
    r = requests.get(f"{PROXY_BASE}/fred",
                     params={"series_id": series_id, "observation_start": observation_start},
                     timeout=45)
    r.raise_for_status()
    return r.json().get("observations", [])
