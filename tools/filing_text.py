"""Filing text extraction — feed the brain actual filing PROSE, not just numbers.

Two entry points, both fail-soft (never raise; return {"status": "error", ...}):

  * get_mdna_excerpt(ticker)             — MD&A section of the latest 10-Q (10-K fallback)
  * get_latest_earnings_release(ticker)  — press-release exhibit (EX-99.x) of the latest 8-K

HTML stripping uses the stdlib html.parser only (no BeautifulSoup dependency),
and all EDGAR fetches go through tools.net.get_sec which rate-limits and falls
back to the Vercel proxy when the sandbox blocks sec.gov.

CLI: python -m tools.filing_text DELL
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from html import unescape
from html.parser import HTMLParser

# Tags whose contents are never prose.
_SKIP_TAGS = {"script", "style", "head", "title"}
# Tags that imply a break between text runs.
_BLOCK_TAGS = {"p", "div", "br", "tr", "td", "th", "li", "table", "h1", "h2",
               "h3", "h4", "h5", "h6", "hr", "section", "article"}


class _TextExtractor(HTMLParser):
    """Minimal HTML -> text converter (stdlib only)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data):
        if not self._skip_depth:
            self._chunks.append(data)

    def text(self) -> str:
        return "".join(self._chunks)


def _html_to_text(html: str) -> str:
    """Strip HTML to whitespace-normalized text using the stdlib parser."""
    try:
        p = _TextExtractor()
        p.feed(html)
        p.close()
        text = p.text()
    except Exception:
        # Malformed markup: crude regex fallback.
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html,
                      flags=re.I | re.S)
        text = re.sub(r"<[^>]+>", " ", text)
        text = unescape(text)
    text = text.replace("\xa0", " ").replace("​", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _clean_excerpt(text: str, max_chars: int) -> "tuple[str, bool]":
    """Flatten stray line noise (page numbers, lone symbols) and truncate."""
    lines = [ln.strip() for ln in text.split("\n")]
    kept = [ln for ln in lines if ln and not re.fullmatch(r"[\d.,()%$—–\-|]+", ln)]
    flat = "\n".join(kept)
    flat = re.sub(r"\n{3,}", "\n\n", flat).strip()
    truncated = len(flat) > max_chars
    return flat[:max_chars], truncated


def _fetch_text(url: str) -> str:
    from tools.net import get_sec
    r = get_sec(url)
    body = r.text
    # Some EDGAR docs are plain text already.
    if "<" in body and ">" in body:
        return _html_to_text(body)
    return body


_MDNA_RE = re.compile(
    r"Management[’ʼ'`]?s?\s+Discussion\s+and\s+Analysis", re.I)
# Next Item heading at a line start: "Item 3." / "Item 7A." etc.
_NEXT_ITEM_RE = re.compile(r"^\s*Item\s+\d+A?\b", re.I | re.M)
# The substantive MD&A subsections: results of operations (the actual revenue /
# margin walk) and liquidity (cash / debt discussion). The first ~1000 chars of
# MD&A are usually forward-looking-statement boilerplate + an overview; starting
# the excerpt at Results of Operations gets the brain the numbers-bearing prose.
_RESULTS_RE = re.compile(r"Results\s+of\s+Operations", re.I)


def _extract_mdna(text: str, max_chars: int) -> "tuple[str, bool, str]":
    """Return (excerpt, truncated, section_label) from full filing text.

    Locates the MD&A section (Item 7 in a 10-K, Item 2 in a 10-Q), then prefers
    to start the excerpt at "Results of Operations" so the results + liquidity
    discussion survives truncation instead of the opening boilerplate.
    """
    # Iterate forward: TOC rows fail the length test; the first substantive
    # hit is the section start (later hits are "(Continued)" page headers).
    # Two passes: prefer matches sitting under an "Item N." heading (the real
    # section title) over incidental prose mentions (e.g. safe-harbor text).
    matches = list(_MDNA_RE.finditer(text))
    for require_item_heading in (True, False):
        for m in matches:
            start = m.start()
            if require_item_heading and not re.search(
                    r"Item\s+\d+A?\s*[.:—\-]?\s*$", text[max(0, start - 40):start],
                    re.I):
                continue
            nxt = _NEXT_ITEM_RE.search(text, m.end())
            end = nxt.start() if nxt else len(text)
            body = text[start:end]
            if len(body) > 1500:  # real section, not a table-of-contents row
                # Skip the overview/forward-looking boilerplate: begin at Results
                # of Operations when it leaves enough substance for the excerpt.
                sub = _RESULTS_RE.search(body, 200)
                if sub and (len(body) - sub.start()) > 800:
                    excerpt, truncated = _clean_excerpt(body[sub.start():], max_chars)
                    return excerpt, truncated, "mdna_results_of_operations"
                excerpt, truncated = _clean_excerpt(body, max_chars)
                return excerpt, truncated, "mdna"
    # Fallback: first substantive prose after the table of contents.
    offset = 0
    toc = re.search(r"Table\s+of\s+Contents", text, re.I)
    if toc:
        offset = toc.end()
    para = re.search(r"[^\n]{400,}", text[offset:])
    start = offset + para.start() if para else offset
    excerpt, truncated = _clean_excerpt(text[start:], max_chars)
    return excerpt, truncated, "document_start"


def get_mdna_excerpt(ticker: str, max_chars: int = 12000) -> dict:
    """MD&A prose from the latest 10-Q (falling back to 10-K).

    Default raised to 12000 chars: the old ~1000-word cap usually returned only
    the overview/boilerplate. The excerpt now starts at Results of Operations
    (see _extract_mdna) and `section` labels what was returned. NOTE the caller
    should pass a wide max_chars (>=8000) to receive the full results+liquidity
    walk; the orchestrator currently passes 5000 and should be raised.
    """
    try:
        from tools.sec_filings import get_filing_brief
        brief = get_filing_brief(ticker)
        if brief.get("status") != "ok":
            return {"status": "error",
                    "reason": brief.get("reason", "filing brief unavailable")}
        filings = brief.get("recent_filings", [])
        match = next((f for f in filings if f["form"] == "10-Q"), None) \
            or next((f for f in filings if f["form"] == "10-K"), None)
        if match is None:
            return {"status": "error",
                    "reason": f"no recent 10-Q/10-K for {ticker}"}
        text = _fetch_text(match["url"])
        excerpt, truncated, section = _extract_mdna(text, max_chars)
        if len(excerpt) < 200:
            return {"status": "error",
                    "reason": f"could not extract prose from {match['url']}"}
        return {"status": "ok", "ticker": ticker.upper(), "form": match["form"],
                "filed": match["filed"], "url": match["url"],
                "section": section, "excerpt": excerpt, "truncated": truncated}
    except Exception as e:
        return {"status": "error", "reason": f"{type(e).__name__}: {e}"}


_EXHIBIT_NAME_RE = re.compile(r"ex(?:hibit)?[-_.]?99|press", re.I)

# Forward-looking guidance detector. The old coarse OR (guidance|outlook|...)
# fired on risk-factor boilerplate ("our outlook could be affected by..."). We
# now require BOTH a guidance/expectation word AND a nearby numeric MONEY range
# (or a "$N plus or minus" point estimate) — the pattern actual guidance takes
# ("revenue of $24.5 billion to $25.5 billion") and boilerplate almost never does.
_GUIDANCE_WORD_RE = re.compile(
    r"\b(?:guidance|outlook|expect\w*|anticipat\w*|forecast\w*|"
    r"project(?:s|ed|ing)?|for\s+(?:the\s+)?(?:first|second|third|fourth|full)"
    r"[- ](?:quarter|year|fiscal))\b", re.I)
_MONEY_RANGE_RE = re.compile(
    r"\$\s?\d[\d,]*(?:\.\d+)?\s*(?:billion|million|thousand|bn|mm)?"
    r"\s*(?:to|through|and|[-–—])\s*"
    r"\$?\s?\d[\d,]*(?:\.\d+)?\s*(?:billion|million|thousand|bn|mm)?", re.I)
_PLUS_MINUS_RE = re.compile(
    r"\$\s?\d[\d,]*(?:\.\d+)?\s*(?:billion|million|thousand)?\s*,?\s*"
    r"(?:plus or minus|\+/?-|±)", re.I)


def _has_guidance_language(text: str) -> bool:
    """True only when a guidance/expectation word sits within ~240 chars of a
    numeric money range or point estimate — a tight proxy for real forward
    guidance that ignores risk-factor boilerplate."""
    ranges = [m.start() for m in _MONEY_RANGE_RE.finditer(text)]
    ranges += [m.start() for m in _PLUS_MINUS_RE.finditer(text)]
    if not ranges:
        return False
    for gm in _GUIDANCE_WORD_RE.finditer(text):
        if any(abs(r - gm.start()) <= 240 for r in ranges):
            return True
    return False


def get_latest_earnings_release(ticker: str, max_chars: int = 6000) -> dict:
    """Press-release exhibit (EX-99.x) from the most recent 8-K (~120 days)."""
    try:
        from tools.net import get_sec
        from tools.sec_filings import ticker_to_cik
        cik = ticker_to_cik(ticker)
        if cik is None:
            return {"status": "error", "reason": f"no CIK found for {ticker}"}
        sub = get_sec(f"https://data.sec.gov/submissions/CIK{cik}.json").json()
        recent = sub["filings"]["recent"]
        cutoff = (date.today() - timedelta(days=120)).isoformat()
        base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}"

        for form, filed, accession in zip(recent["form"], recent["filingDate"],
                                          recent["accessionNumber"]):
            if form != "8-K":
                continue
            if filed < cutoff:
                break  # submissions list is newest-first
            acc = accession.replace("-", "")
            try:
                idx = get_sec(f"{base}/{acc}/index.json").json()
                items = idx.get("directory", {}).get("item", [])
            except Exception:
                continue
            exhibits = [it["name"] for it in items
                        if _EXHIBIT_NAME_RE.search(it.get("name", ""))
                        and re.search(r"\.(htm|html|txt)$", it["name"], re.I)]
            if not exhibits:
                continue
            exhibits.sort()  # ex99-1 before ex99-2
            url = f"{base}/{acc}/{exhibits[0]}"
            try:
                text = _fetch_text(url)
            except Exception:
                continue
            excerpt, truncated = _clean_excerpt(text, max_chars)
            if len(excerpt) < 200:
                continue  # graphic-only or stub exhibit; keep looking
            return {"status": "ok", "ticker": ticker.upper(), "filed": filed,
                    "url": url, "excerpt": excerpt, "truncated": truncated,
                    "contains_guidance_language": _has_guidance_language(text)}
        return {"status": "none_recent"}
    except Exception as e:
        return {"status": "error", "reason": f"{type(e).__name__}: {e}"}


if __name__ == "__main__":
    import sys
    t = sys.argv[1] if len(sys.argv) > 1 else "DELL"
    print(json.dumps(get_mdna_excerpt(t), indent=2)[:2000])
    print(json.dumps(get_latest_earnings_release(t), indent=2)[:2000])
