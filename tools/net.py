"""Network helper: direct fetch with automatic fallback through our Vercel proxy.

Cloud trading runs execute in a sandbox that blocks most external hosts (SEC,
FRED) but can reach our own dashboard domain. These helpers try the real host
first and transparently fall back to the proxy routes in dashboard/app/api/.

Timeouts are deliberately SHORT and use the (connect, read) tuple form. In the
cloud sandbox only GitHub is reachable, so both the real host AND the Vercel
proxy are blocked there — a long timeout would burn the GitHub Action's 15-min
budget across ~8 FRED series and many SEC calls. A blocked host must fail in a
few seconds so the caller can degrade to the cached bundle promptly.
"""

from __future__ import annotations

import os
import time

import requests

PROXY_BASE = os.environ.get("DATA_PROXY_BASE", "https://east-equity-agent.vercel.app/api")
SEC_HEADERS = {"User-Agent": "East Equity Agent easton.ryan@hws.edu"}

# (connect, read) seconds. Short connect so a blocked/unreachable host fails fast;
# a modest read budget still tolerates a genuinely slow-but-reachable response.
DIRECT_TIMEOUT = (5, 20)
# The proxy is a quick last resort, not a slow default: if the real host was
# blocked, the proxy is almost certainly blocked too (only GitHub is reachable
# in-sandbox), so keep this tight to avoid stacking timeouts.
PROXY_TIMEOUT = (4, 8)


def _retry_after_seconds(header_val, attempt: int, base: float = 2.0,
                         cap: float = 30.0) -> float:
    """Backoff for a throttle response: honor a numeric Retry-After header,
    else exponential (base * 2^attempt), always capped. Pure/testable."""
    try:
        return min(float(header_val), cap)
    except (TypeError, ValueError):
        return min(base * (2 ** attempt), cap)


def get_with_throttle_retry(url: str, *, headers=None, params=None,
                            timeout=DIRECT_TIMEOUT, retries: int = 2) -> requests.Response:
    """GET that retries ONLY throttle responses (429/503) with bounded backoff,
    honoring Retry-After. Connection errors propagate immediately — in the cloud
    sandbox a blocked host must fail fast, never stack sleeps."""
    attempt = 0
    while True:
        r = requests.get(url, headers=headers, params=params, timeout=timeout)
        if r.status_code in (429, 503) and attempt < retries:
            time.sleep(_retry_after_seconds(r.headers.get("Retry-After"), attempt))
            attempt += 1
            continue
        r.raise_for_status()
        return r


def get_sec(url: str) -> requests.Response:
    """GET a sec.gov URL, falling back to the Vercel proxy if blocked.
    Direct attempt retries 429/503 throttles with backoff (rate-limit hardening);
    a blocked/unreachable host still fails fast to the proxy."""
    time.sleep(0.15)  # stay well under SEC's 10 req/s limit
    try:
        return get_with_throttle_retry(url, headers=SEC_HEADERS, timeout=DIRECT_TIMEOUT)
    except requests.RequestException:
        r = requests.get(f"{PROXY_BASE}/sec", params={"url": url}, timeout=PROXY_TIMEOUT)
        r.raise_for_status()
        return r


def get_fred_observations(series_id: str, observation_start: str) -> list[dict]:
    """FRED observations via the proxy (used when direct fredapi access fails)."""
    r = requests.get(f"{PROXY_BASE}/fred",
                     params={"series_id": series_id, "observation_start": observation_start},
                     timeout=PROXY_TIMEOUT)
    r.raise_for_status()
    return r.json().get("observations", [])
