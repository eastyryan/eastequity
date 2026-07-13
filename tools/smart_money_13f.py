"""13F Smart Money Tool — tracks what elite funds DID last quarter in our universe.

Uses keyless EDGAR submissions + information-table XML. For each curated manager
we pull the TWO most recent 13F-HR filings and DIFF them by CUSIP, so the signal
is quarter-over-quarter ACTIVITY (new / added / trimmed / exited) — the entire
point of a 13F — instead of a static snapshot of position levels. Holdings are
matched to our tickers by CUSIP when a ticker->CUSIP map is supplied, else by a
strict normalized issuer-name match (the old first-word substring was near-dead
and mismatched lookalikes).

Because there is no authoritative free ticker->CUSIP feed, we LEARN one over
time: every CUSIP resolved by a confident (unambiguous) strict name match is
persisted to data/cusip_map.json and reused authoritatively on later runs, so
the tool converges from name-matching toward CUSIP-matching. See the storage
section below for the anti-poisoning guards.

13Fs are ~45 days delayed - fine for swing context (conviction/positioning),
useless for timing; the brain is told so.

CLI: python -m tools.smart_money_13f NVDA VRT
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

HEADERS = {"User-Agent": "East Equity Agent easton.ryan@hws.edu"}

ROOT = Path(__file__).resolve().parent.parent

# Curated managers worth tracking for AI/semis positioning. CIKs are stable.
MANAGERS = {
    "Berkshire Hathaway": "0001067983",
    "Appaloosa (Tepper)": "0001656456",
    "Coatue Management": "0001135730",
    "Tiger Global": "0001167483",
    "D1 Capital": "0001747057",
    "Altimeter Capital": "0001541617",
    "Duquesne (Druckenmiller)": "0001536411",
    "Third Point (Loeb)": "0001040273",
    "ValueAct": "0001418814",
}

# Corporate suffixes / share-class descriptors stripped before name matching.
_NAME_SUFFIXES = {
    "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "LTD",
    "LIMITED", "PLC", "LP", "LLC", "LLP", "HOLDINGS", "HOLDING", "GROUP",
    "THE", "CL", "CLASS", "A", "B", "C", "COM", "COMMON", "STOCK", "SHS",
    "SHARES", "SA", "NV", "AG", "ADR", "ADS", "NEW", "PAR", "VALUE", "USD",
    "SPONSORED", "REPRESENTING", "ORD", "ORDINARY", "UNIT", "UNITS", "&",
}

# A share change smaller than this magnitude is treated as "held" (flat).
_FLAT_PCT = 0.05
_BUY_ACTIONS = ("new", "added")
_SELL_ACTIONS = ("trimmed", "exited")


# ---------------------------------------------------------------------------
# Learned ticker->CUSIP map (data/cusip_map.json)
#
# There is no authoritative free ticker->CUSIP feed, so without help this tool
# re-derives the CUSIP from issuer names on EVERY run. Instead we PERSIST the
# CUSIPs we resolve confidently (via the strict issuer-name matcher) and reuse
# them AUTHORITATIVELY on later runs. The file lives at data/cusip_map.json — a
# TRACKED path, so it survives the ephemeral cloud runtime; data/cache/ is
# gitignored and would be lost. All storage here is FAIL-SOFT: a missing or
# corrupt file degrades to today's name-matching and never raises.
#
# On-disk format is human-inspectable:
#   { "NVDA": {"cusip": "67066G104", "learned_at": "2026-07-12T...Z",
#              "source_manager": "Coatue Management"}, ... }
# A bare {"NVDA": "67066G104"} value is also accepted on read (hand-seeded maps).
# ---------------------------------------------------------------------------
CUSIP_MAP_FILE = ROOT / "data" / "cusip_map.json"


def _cusip_from_entry(entry) -> str:
    """Pull a CUSIP out of a map value, accepting either the rich form
    {"cusip": ...} or a bare "CUSIP" string. Returns "" if unusable."""
    if isinstance(entry, dict):
        entry = entry.get("cusip", "")
    return entry.upper() if isinstance(entry, str) else ""


def _load_cusip_map_raw() -> dict:
    """Raw on-disk map exactly as stored. Fail-soft: missing/corrupt -> {}."""
    if not CUSIP_MAP_FILE.exists():
        return {}
    try:
        data = json.loads(CUSIP_MAP_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError, ValueError):
        return {}


def _load_cusip_map() -> dict:
    """Flattened authoritative {TICKER: CUSIP} learned from prior runs."""
    out = {}
    for t, v in _load_cusip_map_raw().items():
        cusip = _cusip_from_entry(v)
        if cusip:
            out[t.upper()] = cusip
    return out


def _save_cusip_map(raw: dict) -> None:
    """Atomic write (temp file + os.replace) so a crash mid-write can never
    corrupt the map. Mirrors the guidance-ledger storage pattern."""
    CUSIP_MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(CUSIP_MAP_FILE.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(raw, indent=2, sort_keys=True))
        os.replace(tmp, CUSIP_MAP_FILE)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _merged_cusip_map(ticker_cusip) -> dict:
    """Authoritative {TICKER: CUSIP} = the learned on-disk map MERGED with any
    caller-passed map, where the PASSED arg wins. Caller hints are USED for
    matching but never persisted — only strict name matches are learned."""
    merged = _load_cusip_map()
    for k, v in (ticker_cusip or {}).items():
        if v:
            merged[k.upper()] = v.upper()
    return merged


def _resolve_learned(candidates: dict, blocked: set, existing: dict) -> dict:
    """Decide which strict name matches are safe to LEARN. This is the
    anti-poisoning gate: a ticker is learned ONLY if it resolved to exactly ONE
    CUSIP, was never ambiguous (no manager matched >1 issuer for it), and is not
    already authoritatively mapped. `candidates` is {TICKER: {cusip: manager}}
    accumulated across managers. Returns {TICKER: (cusip, source_manager)}."""
    out = {}
    for tu, by_cusip in candidates.items():
        if tu in blocked or tu in existing:
            continue
        if len(by_cusip) == 1:  # every manager that name-matched agreed on it
            cusip, mgr = next(iter(by_cusip.items()))
            out[tu] = (cusip, mgr)
    return out


def _persist_learned(learned: dict) -> dict:
    """Merge newly-learned {TICKER: (cusip, manager)} into the on-disk map and
    write it atomically. Never overwrites an existing entry (authoritative wins)
    and never raises — persistence is best-effort. Returns {TICKER: CUSIP}
    actually written, for logging."""
    if not learned:
        return {}
    try:
        raw = _load_cusip_map_raw()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        added = {}
        for tu, (cusip, mgr) in learned.items():
            if tu in raw:  # already recorded — do not clobber
                continue
            raw[tu] = {"cusip": cusip, "learned_at": now, "source_manager": mgr}
            added[tu] = cusip
        if added:
            _save_cusip_map(raw)
        return added
    except Exception:
        return {}


def _get(url: str):
    from tools.net import get_sec
    return get_sec(url)


def _norm_tokens(name: str) -> list:
    up = re.sub(r"[^A-Z0-9 ]", " ", (name or "").upper())
    return [t for t in up.split() if t and t not in _NAME_SUFFIXES]


def _names_match(issuer: str, company: str) -> bool:
    """Strict normalized match: significant tokens (suffixes stripped) must be
    equal, or - for multi-token names - differ by at most one descriptor. Single
    -token names must match exactly so "APPLE" cannot latch onto "APPLE HOSP"."""
    a, b = set(_norm_tokens(issuer)), set(_norm_tokens(company))
    if not a or not b:
        return False
    if a == b:
        return True
    smaller, larger = (a, b) if len(a) <= len(b) else (b, a)
    return len(smaller) >= 2 and smaller <= larger and len(larger) - len(smaller) <= 1


def _parse_info_table(xml_bytes: bytes) -> dict:
    """Parse a 13F information table into {CUSIP: aggregated long position}.
    Multiple rows for one CUSIP (e.g. across managers/accounts) are summed;
    option (put/call) rows are ignored - we track share ownership only."""
    root = ElementTree.fromstring(xml_bytes)
    ns = {"n": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}
    tag = "n:infoTable" if ns else "infoTable"
    by_cusip: dict = {}
    for it in root.iterfind(f".//{tag}", ns):
        def txt(path: str) -> str:
            el = it.find(f"n:{path}" if ns else path, ns)
            return el.text.strip() if el is not None and el.text else ""
        if txt("putCall"):
            continue
        cusip = txt("cusip").upper()
        if not cusip:
            continue
        try:
            shares = int(float(txt("shrsOrPrnAmt/sshPrnamt") or 0))
            # `value` is whole USD on post-2023Q3 filings; pre-2023Q3 it was in
            # thousands. We only ever diff the TWO most recent quarters, so both
            # sides share the same unit — no normalization needed. If this is ever
            # widened to compare across the 2023Q3 boundary, scale old values ×1000.
            value = int(float(txt("value") or 0))
        except ValueError:
            continue
        rec = by_cusip.setdefault(
            cusip, {"cusip": cusip, "issuer": txt("nameOfIssuer"),
                    "shares": 0, "value_usd": 0})
        rec["shares"] += shares
        rec["value_usd"] += value
    return by_cusip


def _fetch_info_table(cik: str, acc: str) -> dict:
    listing = _get(f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/").text
    xml_files = re.findall(r'href="([^"]+\.xml)"', listing)
    info_xml = next((f for f in xml_files if "primary_doc" not in f.lower()), None)
    if info_xml is None:
        return {}
    url = info_xml if info_xml.startswith("http") else \
        f"https://www.sec.gov{info_xml}" if info_xml.startswith("/") else \
        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{info_xml}"
    return _parse_info_table(_get(url).content)


def _two_latest_13f(cik: str) -> list:
    """Return [(period, {cusip: pos}), ...] for the two most recent DISTINCT 13F
    report periods (newest first). Amendments collapse into their period; for a
    period we keep the newest-filed table."""
    sub = _get(f"https://data.sec.gov/submissions/CIK{cik}.json").json()
    recent = sub["filings"]["recent"]
    chosen, seen = [], set()
    for form, acc, period in zip(recent["form"], recent["accessionNumber"],
                                 recent["reportDate"]):
        if form not in ("13F-HR", "13F-HR/A") or period in seen:
            continue
        seen.add(period)
        chosen.append((period, acc.replace("-", "")))
        if len(chosen) >= 2:
            break
    out = []
    for period, acc in chosen:
        try:
            out.append((period, _fetch_info_table(cik, acc)))
        except Exception:
            out.append((period, {}))
    return out


def _compute_deltas(latest: dict, prior: dict) -> dict:
    """Pure QoQ diff of two CUSIP->position maps. Classifies each CUSIP as
    new / added / held / trimmed / exited with share & value deltas."""
    out: dict = {}
    for c in set(latest) | set(prior):
        L, P = latest.get(c), prior.get(c)
        ls = int(L["shares"]) if L else 0
        ps = int(P["shares"]) if P else 0
        lv = int(L["value_usd"]) if L else 0
        pv = int(P["value_usd"]) if P else 0
        issuer = (L or P).get("issuer", "")
        share_delta = ls - ps
        if ps == 0:
            action = "new" if ls > 0 else "none"
        elif ls == 0:
            action = "exited"
        else:
            pct = share_delta / ps
            action = "held" if abs(pct) < _FLAT_PCT else ("added" if pct > 0 else "trimmed")
        out[c] = {
            "cusip": c, "issuer": issuer, "action": action,
            "latest_shares": ls, "prior_shares": ps, "share_delta": share_delta,
            "latest_value_usd": lv, "prior_value_usd": pv,
            "value_delta_usd": lv - pv,
            "pct_share_change": round(share_delta / ps * 100, 1) if ps else None,
        }
    return out


def get_smart_money(tickers, company_names=None, ticker_cusip=None) -> dict:
    """Curated managers' QUARTER-OVER-QUARTER 13F activity in our tickers.

    Returns a per-ticker `by_ticker` structure (net_activity, manager counts,
    net share/value deltas, notable increases/decreases) plus a flat `holdings`
    list of individual manager moves (backward compatible: still carries
    manager/ticker/period/issuer/shares/value_usd, now enriched with deltas).

    ticker_cusip: optional {TICKER: CUSIP} map for authoritative CUSIP matching;
    absent it, holdings are matched by strict normalized issuer-name match.
    """
    try:
        from tools.sec_filings import ticker_to_cik  # reuse ticker->CIK->name
        names = dict(company_names or {})
        # Authoritative CUSIP source: the LEARNED map from prior runs, merged with
        # any caller-passed map (passed wins). Both are used for matching; only
        # confident strict name matches get persisted back below.
        cusip_map = _merged_cusip_map(ticker_cusip)
        for t in tickers:
            if t not in names:
                cik = ticker_to_cik(t)
                if cik:
                    try:
                        sub = _get(f"https://data.sec.gov/submissions/CIK{cik}.json").json()
                        names[t] = sub.get("name", t)
                    except Exception:
                        names[t] = t

        agg = {t.upper(): {"ticker": t.upper(),
                           "managers_new": 0, "managers_added": 0,
                           "managers_held": 0, "managers_trimmed": 0,
                           "managers_exited": 0, "net_share_delta": 0,
                           "net_value_delta_usd": 0, "matched_by": None,
                           "moves": []} for t in tickers}
        holdings = []
        # Anti-poisoning accumulators for the learning loop:
        #   learn_candidates[TICKER] = {cusip: source_manager} from strict name
        #     matches (only for tickers we don't already have a CUSIP for);
        #   learn_blocked = tickers where some manager matched >1 issuer, so the
        #     name is ambiguous and must never be learned this run.
        learn_candidates: dict = {}
        learn_blocked: set = set()

        for mgr, cik in MANAGERS.items():
            try:
                filings = _two_latest_13f(cik)
            except Exception as e:
                holdings.append({"manager": mgr, "error": str(e)[:200]})
                continue
            if not filings:
                continue
            latest_period, latest = filings[0]
            prior_period, prior = filings[1] if len(filings) > 1 else ("", {})
            deltas = _compute_deltas(latest, prior)

            for t in tickers:
                tu = t.upper()
                want = cusip_map.get(tu)
                if want and want in deltas:
                    matched, mby = [deltas[want]], "cusip"
                else:
                    company = names.get(t, t)
                    matched = [d for d in deltas.values()
                               if _names_match(d["issuer"], company)]
                    mby = "name" if matched else None
                    # Learn only from CONFIDENT, UNAMBIGUOUS strict name matches
                    # on tickers we don't already have an authoritative CUSIP for.
                    if mby == "name" and not want:
                        if len(matched) == 1:
                            learn_candidates.setdefault(tu, {}).setdefault(
                                matched[0]["cusip"], mgr)
                        else:  # >1 issuer matched this name -> ambiguous, never learn
                            learn_blocked.add(tu)
                for d in matched:
                    if d["action"] == "none":
                        continue
                    a = agg[tu]
                    a["matched_by"] = a["matched_by"] or mby
                    a["managers_" + d["action"]] += 1
                    a["net_share_delta"] += d["share_delta"]
                    a["net_value_delta_usd"] += d["value_delta_usd"]
                    a["moves"].append({
                        "manager": mgr, "action": d["action"],
                        "share_delta": d["share_delta"],
                        "value_delta_usd": d["value_delta_usd"],
                        "latest_value_usd": d["latest_value_usd"],
                        "pct_share_change": d["pct_share_change"]})
                    holdings.append({
                        "manager": mgr, "ticker": tu, "cusip": d["cusip"],
                        "issuer": d["issuer"], "period": latest_period,
                        "prior_period": prior_period, "matched_by": mby,
                        "action": d["action"], "shares": d["latest_shares"],
                        "prior_shares": d["prior_shares"],
                        "share_delta": d["share_delta"],
                        "value_usd": d["latest_value_usd"],
                        "value_delta_usd": d["value_delta_usd"],
                        "pct_share_change": d["pct_share_change"]})

        # Persist confident new ticker->CUSIP pairs so later runs match by CUSIP
        # authoritatively instead of re-deriving them from issuer names. Fail-soft.
        learned = _resolve_learned(learn_candidates, learn_blocked, cusip_map)
        cusips_learned = _persist_learned(learned)

        by_ticker = {}
        for tu, a in agg.items():
            buyers = a["managers_new"] + a["managers_added"]
            sellers = a["managers_trimmed"] + a["managers_exited"]
            if buyers == 0 and sellers == 0:
                net = "no_tracked_activity"
            elif buyers > sellers:
                net = "net_buying"
            elif sellers > buyers:
                net = "net_selling"
            else:
                net = "mixed"
            moves = a.pop("moves")
            a["net_activity"] = net
            a["notable_increases"] = sorted(
                (m for m in moves if m["action"] in _BUY_ACTIONS),
                key=lambda m: m["value_delta_usd"], reverse=True)[:3]
            a["notable_decreases"] = sorted(
                (m for m in moves if m["action"] in _SELL_ACTIONS),
                key=lambda m: m["value_delta_usd"])[:3]
            by_ticker[tu] = a

        return {
            "status": "ok", "tickers": list(tickers),
            "note": ("13F data is ~45 days stale - conviction/positioning ONLY, never "
                     "timing. Signal is QUARTER-OVER-QUARTER activity: "
                     "by_ticker[T].net_activity says whether the 9 tracked elite funds "
                     "were net BUYING or SELLING the name last quarter; "
                     "notable_increases/decreases name the biggest dollar moves; "
                     "holdings lists each manager's move. Matched by CUSIP when a map "
                     "is supplied, else strict issuer-name match. no_tracked_activity "
                     "just means none of these 9 hold the name - NOT bearish."),
            "by_ticker": by_ticker,
            "holdings": holdings,
            "cusips_learned": cusips_learned,  # {TICKER: CUSIP} newly persisted this run
        }
    except Exception as e:
        return {"status": "error", "reason": str(e)[:200]}


if __name__ == "__main__":
    import sys
    print(json.dumps(get_smart_money(sys.argv[1:] or ["NVDA"]), indent=2))
