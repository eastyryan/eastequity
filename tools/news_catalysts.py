"""News & Catalyst Fetcher — swing-relevant headlines + upcoming earnings dates.

Sources (all free/keyless via yfinance):
  * per-ticker news headlines, filtered to the last NEWS_MAX_AGE_DAYS and
    stamped with age_days so the brain sees freshness instead of guessing
  * next earnings date (the single most important swing catalyst clock)
  * analyst consensus snapshot (recommendation mean/key, analyst count, mean
    price target vs price) — quantitative sentiment context, not a signal

Deliberately shallow: it surfaces raw headlines with links; Claude judges
swing relevance and tone. A paid news API can slot in behind the same
interface later.

EVERY HEADLINE CARRIES source / tier / subject (added 2026-07-25).

  * source — the publisher. All three feeds return one (yfinance
    content.provider.displayName, Finnhub `source`, Alpaca `source`) and this
    module used to drop all three on the floor, building {title, published, url}
    only. A measurement of the live 07-25 bundle found 0 of 96 headlines carried
    a publisher, so the brain could not tell a Reuters scoop from a Zacks
    template. Falls back to the URL's domain so nothing is ever unattributed.
  * tier — "primary" (wires/major papers/PR) vs "commentary" (content mills).
    Reported, NEVER filtered: a hard block would drop legitimate breaking news
    from an untiered outlet. The brain and risk desk weight it.
  * subject — "direct" (about this company), "peer_or_market" (about someone
    else), "roundup" (a multi-name list piece). THE reason this module changed:
    49% of per-ticker headlines in the live 07-25 bundle were about a DIFFERENT
    company — Yahoo's per-ticker feed splices in "related" peer articles, so MPC
    was served "Will UK interest rates fall in 2026?" and a Chevron re-rate
    piece, SNOW got Palantir articles, and TRV got five straight headlines about
    OTHER insurers' earnings. Handed a peer's news under this ticker's name, the
    brain reconciles it into a catalyst that does not exist; the risk desk then
    vetoes the fabrication and nothing trades. Off-subject rows are TAGGED, not
    dropped — peer news is genuinely useful context — but "direct" rows sort
    first so they win the limited max_headlines budget.

Optional upgrade: when FINNHUB_API_KEY is set (free tier, 60 calls/min —
https://finnhub.io) each ticker also pulls Finnhub company-news for the last
week, mapped to the same headline shape, deduped by title, and merged ahead
of the recency filter. No key -> byte-identical yfinance-only behavior.

Optional upgrade: when ALPACA_API_KEY/SECRET are set, ONE batched Alpaca News
call (Benzinga-sourced, free Basic plan) covers every requested ticker and is
distributed per symbol, merged the same way. Real timestamps, so headlines
survive the recency filter with honest age_days.

CLI: python -m tools.news_catalysts NVDA VRT
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone

NEWS_MAX_AGE_DAYS = 7

# A row tagging more than this many symbols is a market roundup ("12 Tech Stocks
# Moving In Friday's Session"), not a single-name catalyst. Alpaca distributes
# every row to EVERY symbol it tags, so without this one list piece became the
# headline catalyst for a dozen unrelated names.
MAX_SYMBOLS_BEFORE_ROUNDUP = 3

# Publisher -> tier. Substring match against a casefolded source/domain, so
# "reuters.com", "Reuters" and "alpaca_reuters" all land on the same tier.
# Deliberately partial: an outlet absent from both lists gets tier=None, which
# means "unrated", NOT "bad" — see the module docstring on why this never filters.
_PRIMARY_SOURCES = (
    "reuters", "bloomberg", "wsj", "wall street journal", "dow jones",
    "associated press", "apnews", "financial times", "ft.com", "cnbc",
    "barrons", "barron's", "businesswire", "business wire", "prnewswire",
    "pr newswire", "globenewswire", "globe newswire", "sec.gov", "nasdaq.com",
    "axios", "the information", "marketwatch",
)
_COMMENTARY_SOURCES = (
    "zacks", "fool.com", "motley fool", "247wallst", "24/7 wall", "trefis",
    "marketbeat", "insider monkey", "insidermonkey", "simply wall st",
    "simplywall", "benzinga", "investorplace", "stocktwits", "gurufocus",
    "seeking alpha", "seekingalpha", "talkmarkets", "moneyweek", "barchart",
)

# Corporate-form noise that carries no identifying signal: matching on these
# would make "Delek US Holdings" look like news about any other holdings company.
_NAME_STOPWORDS = frozenset({
    "inc", "corp", "corporation", "ltd", "limited", "holdings", "holding",
    "co", "company", "the", "plc", "group", "sa", "nv", "ag", "lp", "llc",
    "class", "common", "stock", "enterprise", "enterprises", "international",
    "technologies", "technology", "systems", "solutions",
})


def _source_from_url(url) -> str | None:
    """Registered domain of a headline URL, minus 'www.'. Used only as the
    fallback when a feed gave us no publisher. Pure; None on garbage."""
    if not url:
        return None
    m = re.match(r"^https?://([^/:?#]+)", str(url).strip(), re.I)
    if not m:
        return None
    host = m.group(1).lower()
    return host[4:] if host.startswith("www.") else host or None


def _tier_for(source) -> str | None:
    """'primary' | 'commentary' | None (unrated). Pure.

    Never a filter — a hard block on 'commentary' would kill a real scoop the
    moment an unrated outlet broke it first."""
    s = str(source or "").casefold()
    if not s:
        return None
    if any(k in s for k in _PRIMARY_SOURCES):
        return "primary"
    if any(k in s for k in _COMMENTARY_SOURCES):
        return "commentary"
    return None


def _company_tokens(company_name) -> set[str]:
    """Identifying lowercase tokens of a company name, corporate-form words and
    1-2 char fragments removed. 'Marathon Petroleum Corp' -> {marathon,
    petroleum}. Pure; empty set when nothing identifying survives."""
    words = re.split(r"[^A-Za-z]+", str(company_name or "").lower())
    return {w for w in words if w and len(w) > 2 and w not in _NAME_STOPWORDS}


def _headline_subject(ticker: str, name_tokens: set[str], headline: dict) -> str:
    """'direct' | 'roundup' | 'peer_or_market' for one headline. Pure.

    direct         — the title names this ticker or this company.
    roundup        — a multi-name list piece (see MAX_SYMBOLS_BEFORE_ROUNDUP);
                     checked BEFORE 'direct' because a roundup that happens to
                     name the company is still a list piece, not a catalyst.
    peer_or_market — everything else: a competitor's earnings, a macro story,
                     a sector note. Real context, but never this name's news.
    """
    title = str(headline.get("title") or "")
    if len(headline.get("symbols") or []) > MAX_SYMBOLS_BEFORE_ROUNDUP:
        return "roundup"
    if re.search(r"\b" + re.escape(str(ticker).upper()) + r"\b", title.upper()):
        return "direct"
    low = title.lower()
    if name_tokens and any(tok in low for tok in name_tokens):
        return "direct"
    return "peer_or_market"


def _parse_pubdate(value):
    """yfinance pubDate (ISO string) or legacy providerPublishTime (epoch) ->
    aware datetime, else None."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _filter_recent(items: list, now: datetime, max_age_days: float = NEWS_MAX_AGE_DAYS) -> list:
    """Drop headlines older than max_age_days; stamp age_days on survivors.
    Headlines with an unparseable date are KEPT with age_days=None — fail-open
    but honestly labeled, so the brain knows the age is unknown. Pure function."""
    out = []
    for h in items:
        dt = _parse_pubdate(h.get("published"))
        if dt is None:
            out.append({**h, "age_days": None})
            continue
        age = (now - dt).total_seconds() / 86400.0
        if age <= max_age_days:
            out.append({**h, "age_days": round(age, 1)})
    return out


def _headlines_from_finnhub(payload) -> list[dict]:
    """Finnhub /company-news rows -> our headline shape
    [{title, published (iso|None), url, source}]. Non-list/garbage -> []. Pure.

    Finnhub's `source` was previously discarded here; it is the publisher the
    brain needs to weight the headline at all."""
    if not isinstance(payload, list):
        return []
    out = []
    for row in payload:
        if not isinstance(row, dict) or not row.get("headline"):
            continue
        ts = row.get("datetime")
        published = None
        if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0:
            try:
                published = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            except (OverflowError, OSError, ValueError):
                pass
        out.append({"title": str(row["headline"]).strip(),
                    "published": published, "url": row.get("url"),
                    "source": row.get("source") or _source_from_url(row.get("url"))})
    return out


def _title_key(title) -> str:
    """Casefold + strip punctuation + collapse whitespace -> dedupe key. Pure."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", str(title or "").casefold())).strip()


def _merge_headlines(base: list, extra: list) -> list:
    """Append extra headlines to base, skipping extras whose title duplicates a
    base title (or an earlier extra). Base rows pass through UNTOUCHED so the
    no-key path stays byte-identical. Pure."""
    seen = {_title_key(h.get("title")) for h in base if _title_key(h.get("title"))}
    out = list(base)
    for h in extra:
        key = _title_key(h.get("title"))
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out


def _fetch_finnhub_company_news(ticker: str, api_key: str, now: datetime) -> list[dict]:
    """Finnhub company-news for the last NEWS_MAX_AGE_DAYS, in headline shape.
    Fail-soft: any error -> []."""
    try:
        from tools.net import get_with_throttle_retry
        r = get_with_throttle_retry(
            "https://finnhub.io/api/v1/company-news",
            params={"symbol": ticker.upper(),
                    "from": (now - timedelta(days=NEWS_MAX_AGE_DAYS)).date().isoformat(),
                    "to": now.date().isoformat(),
                    "token": api_key},
            timeout=(4, 8), retries=1)
        return _headlines_from_finnhub(r.json())
    except Exception:
        return []


def _fetch_alpaca_news_by_symbol(tickers: list[str], max_age_days: float) -> dict:
    """One batched Alpaca News call for all tickers, distributed per symbol in
    headline shape [{title, published, url, source, symbols}].
    Fail-soft: {} on any error.

    CARRIES `source` AND `symbols` THROUGH (2026-07-25). alpaca_data.get_news
    already returns both and this function used to keep neither, which cost two
    things: the publisher (see module docstring) and any way to tell a
    single-name story from a roundup. Alpaca distributes one row to EVERY symbol
    it tags, so a 12-ticker "stocks moving today" piece was landing as a
    first-class catalyst under all twelve names. Keeping `symbols` lets
    _headline_subject label those rows 'roundup' so they can never anchor a
    thesis."""
    try:
        from tools import alpaca_data
        if not alpaca_data.has_keys():
            return {}
        rows = alpaca_data.get_news(symbols=tickers, hours=max_age_days * 24.0, limit=50)
    except Exception:
        return {}
    out: dict = {}
    wanted = {str(t).upper() for t in tickers}
    for row in rows:
        syms = [str(s).upper() for s in (row.get("symbols") or [])]
        h = {"title": row.get("title"), "published": row.get("published"),
             "url": row.get("url"),
             "source": row.get("source") or _source_from_url(row.get("url")),
             "symbols": syms}
        for sym in syms:
            if sym in wanted:
                out.setdefault(sym, []).append(h)
    return out


def _beat_streak(surprise_pcts: list) -> int:
    """Consecutive positive EPS surprises counting back from the most recent
    quarter. None/zero/negative breaks the streak. Pure function."""
    streak = 0
    for s in reversed(surprise_pcts):
        if isinstance(s, (int, float)) and s > 0:
            streak += 1
        else:
            break
    return streak


def _surprise_pct(value):
    """Normalize yfinance surprisePercent: modern versions emit a fraction
    (0.053 = +5.3%); values already looking like percents pass through. NaN and
    non-numbers -> None."""
    if not isinstance(value, (int, float)) or value != value:
        return None
    return round(value * 100, 1) if abs(value) <= 1.5 else round(value, 1)


def _ratings_from_info(info: dict):
    """Analyst consensus snapshot from a yfinance info dict. Pure/testable;
    returns None when nothing useful is present. recommendation_mean scale:
    1=strong buy .. 5=sell."""
    if not isinstance(info, dict):
        return None
    mean = info.get("recommendationMean")
    target = info.get("targetMeanPrice")
    price = info.get("currentPrice") or info.get("regularMarketPrice")
    out = {
        "recommendation_key": info.get("recommendationKey"),
        "recommendation_mean": mean,
        "n_analysts": info.get("numberOfAnalystOpinions"),
        "target_mean_price": target,
        "target_vs_price_pct": (round((target / price - 1) * 100, 1)
                                if isinstance(target, (int, float))
                                and isinstance(price, (int, float)) and price > 0
                                else None),
    }
    return out if any(v is not None for v in out.values()) else None


def get_news_and_catalysts(tickers: list[str], max_headlines: int = 6,
                           max_age_days: float = NEWS_MAX_AGE_DAYS,
                           company_names: dict | None = None) -> dict:
    """Per-focus-name headlines, earnings date, ratings and beat history.

    company_names: optional {TICKER: "Legal Name"} so subject tagging can tell a
    headline about THIS company from one about a peer. The bundle already
    resolves these (sec_filings[TICKER].company); omitted -> fall back to
    yfinance longName, then to ticker-symbol matching alone.

    STATUS CONTRACT (tools/freshness_audit.feed_status): this function used to
    hardcode {"status": "ok"} at line 199 and NEVER downgrade it, no matter how
    many per-ticker calls raised. orchestrator's only automated data-health check
    read that key, so `news_and_catalysts` could not fail - a run where every
    yfinance news fetch was rate-limited still advertised a healthy feed and the
    brain reasoned from an empty tape as if the tape were genuinely quiet.

    Now: "error" when every name failed, "degraded" with counts on partial
    failure, "empty" when every name was reachable but nobody had a fresh
    headline (a real, honest market state, and a different fact from a dead feed).
    """
    import yfinance as yf

    from tools.freshness_audit import stamp_feed

    now = datetime.now(timezone.utc)
    finnhub_key = os.environ.get("FINNHUB_API_KEY")
    alpaca_by_symbol = _fetch_alpaca_news_by_symbol(list(tickers), max_age_days)
    out = {"as_of": now.isoformat(),
           "news_max_age_days": max_age_days, "tickers": {}}
    tickers = list(tickers or [])
    failures: dict = {}
    fetched = with_headlines = 0
    for t in tickers:
        entry: dict = {"headlines": [], "next_earnings": None, "analyst_ratings": None}
        try:
            tk = yf.Ticker(t)
            raw = []
            for item in (tk.news or []):
                content = item.get("content", item)
                url = (content.get("canonicalUrl") or {}).get("url") or content.get("link")
                # provider.displayName is where modern yfinance puts the publisher;
                # older payloads used a flat "publisher". Domain is the last resort
                # so a headline is never handed to the brain unattributed.
                provider = content.get("provider")
                pub = (provider.get("displayName") if isinstance(provider, dict) else None) \
                    or content.get("publisher") or _source_from_url(url)
                raw.append({
                    "title": content.get("title"),
                    "published": content.get("pubDate") or content.get("providerPublishTime"),
                    "url": url,
                    "source": pub,
                })
            if alpaca_by_symbol.get(t.upper()):  # optional merge; no keys -> unchanged
                raw = _merge_headlines(raw, alpaca_by_symbol[t.upper()])
            if finnhub_key:  # optional merge; no key -> byte-identical behavior
                raw = _merge_headlines(raw, _fetch_finnhub_company_news(t, finnhub_key, now))
            # NOT sliced to max_headlines yet: subject tagging below decides which
            # rows deserve the budget, and slicing here would let three peer
            # articles crowd out the one headline actually about this company.
            recent = _filter_recent(raw, now, max_age_days)
            try:
                cal = tk.calendar
                dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
                # yfinance's calendar returns whatever it holds, INCLUDING a date that
                # has already passed. ZS on 2026-07-19 returned 2026-05-26 — its LAST
                # report — and that landed in the digest as `next_earnings`, 54 days in
                # the past. CLAUDE.md tells the brain to choose the earnings path
                # "through binary vs around it", so a stale date here is worse than no
                # date: it says a print is coming that already happened. Only a FUTURE
                # date is a next-earnings date.
                today = now.date() if hasattr(now, "date") else None
                for d in (dates or []):
                    try:
                        dd = d.date() if hasattr(d, "date") else None
                        if today is not None and dd is not None and dd < today:
                            continue
                    except Exception:
                        pass
                    entry["next_earnings"] = str(d)
                    break
            except Exception:
                pass
            info = None
            try:  # analyst consensus: tk.info is flaky — degrade to None, never fail the entry
                info = tk.info
                entry["analyst_ratings"] = _ratings_from_info(info)
            except Exception:
                entry["analyst_ratings"] = None

            # SUBJECT TAGGING. Caller-supplied name wins (the bundle already knows
            # it from sec_filings[TICKER].company); yfinance's longName is the
            # fallback. With NEITHER, name_tokens is empty and _headline_subject
            # degrades to ticker-symbol matching only — it can then under-call
            # 'direct', so a missing name must never be allowed to DROP a row.
            # That is precisely why off-subject rows are demoted, not deleted.
            company = (company_names or {}).get(t.upper()) or (
                (info or {}).get("longName") or (info or {}).get("shortName")
                if isinstance(info, dict) else None)
            entry["company"] = company
            name_tokens = _company_tokens(company)
            for h in recent:
                h["subject"] = _headline_subject(t, name_tokens, h)
                h["tier"] = _tier_for(h.get("source"))
                h.pop("symbols", None)   # bookkeeping for the roundup test only
            rank = {"direct": 0, "peer_or_market": 1, "roundup": 2}
            # Stable sort: within a subject class the feed's own recency order
            # survives, so this re-prioritizes without reshuffling fresh news.
            entry["headlines"] = sorted(
                recent, key=lambda h: rank.get(h.get("subject"), 1))[:max_headlines]
            entry["headline_mix"] = {
                k: sum(1 for h in recent if h.get("subject") == k)
                for k in ("direct", "peer_or_market", "roundup")}
            try:  # sell-side scoreboard: did management beat the number, quarter by quarter
                eh = tk.earnings_history
                quarters = []
                if eh is not None and len(eh):
                    for idx, row in eh.iterrows():
                        actual = row.get("epsActual")
                        est = row.get("epsEstimate")
                        quarters.append({
                            "period": str(idx)[:10],
                            "eps_actual": (round(float(actual), 2)
                                           if actual == actual and actual is not None else None),
                            "eps_estimate": (round(float(est), 2)
                                             if est == est and est is not None else None),
                            "surprise_pct": _surprise_pct(row.get("surprisePercent")),
                        })
                if quarters:
                    entry["earnings_surprises"] = {
                        "quarters": quarters[-8:],
                        "beat_streak": _beat_streak([q["surprise_pct"] for q in quarters]),
                    }
            except Exception:
                pass
            fetched += 1
            if entry["headlines"]:
                with_headlines += 1
        except Exception as e:
            entry["error"] = str(e)
            failures[t.upper()] = str(e)[:120]
        out["tickers"][t.upper()] = entry
    out["note"] = (
        "status/coverage describe the FETCH, not the tape. 'error' = every news "
        "call failed (absence of headlines is NOT a quiet tape - do not read it "
        "as 'no news'); 'degraded' = some names unfetched, listed in "
        "coverage.failures; 'empty' = every name reachable, none had a headline "
        "inside news_max_age_days. "
        "EVERY headline carries subject/source/tier. subject='direct' is about "
        "THIS company; 'peer_or_market' is about someone else (a competitor's "
        "print, a macro story) and must NEVER be cited as this ticker's catalyst "
        "- it is context only; 'roundup' is a multi-name list piece and cannot "
        "support a thesis at all. Rows are sorted direct-first. headline_mix "
        "counts each class BEFORE the max_headlines cut, so a name showing "
        "direct:0 genuinely has no company-specific news - say that rather than "
        "reaching for a peer headline. tier='primary' is a wire/major outlet, "
        "'commentary' is a content mill (Zacks/Fool/247wallst/trefis and the "
        "like) whose 'analysis' is not reporting, null is simply unrated - "
        "weight accordingly, and never treat a commentary headline as a "
        "sourced fact.")
    return stamp_feed(out, len(tickers), fetched, len(tickers) - fetched,
                      found=with_headlines,
                      failures=dict(list(failures.items())[:10]),
                      tickers_with_headlines=with_headlines)


if __name__ == "__main__":
    import sys
    try:  # pick up FINNHUB_API_KEY for CLI runs; library callers load env themselves
        from pathlib import Path

        from tools.envload import load_env
        load_env()
    except Exception:
        pass
    print(json.dumps(get_news_and_catalysts(sys.argv[1:] or ["NVDA"]), indent=2, default=str))
