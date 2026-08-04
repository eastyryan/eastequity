"""Commentary reconciliation — the public prose may not claim a trade that never happened.

WHY THIS EXISTS (2026-08-04).

The brain writes `commentary` in the SAME JSON block as its proposals, which
means it is authored BEFORE the risk desk, the validator and the executor have
run. It cannot know whether the trade it just described actually happened. The
orchestrator then hands that prose straight to `refresh_dashboard`, unreconciled,
and the site publishes it verbatim next to a positions table built from real
broker state. When a proposal dies downstream the two disagree in public.

  * 2026-07-17, ABT: commentary said "I am buying Abbott" while the risk-desk
    veto left the site with an empty proposals list. `LAST_RISK_DESK_VETOES` was
    added for that incident — but it only folds the veto into the PROPOSALS
    list. It never touched the prose, so the false sentence stayed on the page.
  * 2026-08-04, DXCM (run 20260804-865280): commentary opened "I bought a small
    position in DexCom (DXCM) today". The risk desk vetoed the proposal for an
    unsourced CONNECT-trial catalyst; `approved=0`, `fills=[]`, and the position
    never existed. The digest card said bought, the open-positions section
    showed only BKR, and a reader had no way to tell which was true.

Surfacing the veto in a different card did not fix it, so the reconciliation is
mechanical and it runs on the OUTCOME, after fills are known:

  1. Anything the brain proposed that did not produce a fill is collected with
     the ground it died on (risk-desk veto / validator rejection / process gate /
     approved-but-unfilled).
  2. The commentary is scanned for first-person execution claims about exactly
     those tickers. Negated, conditional and future phrasings are skipped — "I
     did not buy DXCM" and "I would add DXCM near $82" are not claims.
  3. A deterministic correction is generated from the facts and published above
     the prose, so the page carries the contradiction instead of hiding it.

THE GUARD THAT KEEPS THIS QUIET: nothing fires unless the run actually had a
proposed-but-unexecuted action. A run whose commentary reminisces about a real
past trade ("HPE, the exact stock I sold at a loss three weeks ago") has no
unexecuted proposal to match against and is left alone.

Pure helpers — no network, no config reads, offline-testable.
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Execution verbs. A claim is only a claim in the first person: the brain
# describing what IT did.
# ---------------------------------------------------------------------------
EXECUTION_VERBS = (
    # opens / adds
    r"bought", r"buying", r"purchased", r"added", r"adding", r"opened",
    r"opening", r"initiated", r"initiating", r"picked\s+up", r"entered",
    r"started", r"starting", r"took", r"establish(?:ed|ing)", r"scaled\s+in",
    # closes / trims
    r"sold", r"selling", r"trimmed", r"trimming", r"closed", r"closing",
    r"exited", r"exiting", r"booked", r"cut",
)

# THE SUBJECT MUST GOVERN THE VERB. Requiring only "a first-person pronoun
# somewhere in the sentence" plus "an execution verb somewhere in the sentence"
# was measured against 160 archived runs and produced four false positives, all
# the same shape: the market doing the selling while the agent narrates.
#   "our Dell and HPE positions sold off sector-wide"      (20260713-9f805f)
#   "four insiders sold about $8.8 million ... which I'm watching" (20260724-2fe063)
#   "momentum leaders are getting sold while the index holds up"   (20260724-7cb7cf)
#   "watchlist names ... are being sold off hard"                  (20260728-f921ba)
# So the pronoun has to sit immediately before the verb, separated only by
# whitelisted fillers. Because the filler list is a whitelist, every modal and
# negation ("I would buy", "I did not buy", "I plan to buy") fails to match
# without needing a separate negation rule.
_PRONOUN = r"(?:I|we)(?:'(?:m|ve|re))?"
_FILLER = (r"(?:just|also|already|today|now|then|deliberately|finally|actually|"
           r"therefore|only|instead|have|had|has|am|are|been|being|did|ended\s+up)")
_CLAIM_RE = re.compile(
    r"\b" + _PRONOUN + r"\s+(?:" + _FILLER + r"\s+){0,3}"
    r"(?:" + r"|".join(EXECUTION_VERBS) + r")\b", re.I)

# "sold off" / "bought up" describe a tape, not an order. Checked on the text
# immediately AFTER the verb.
_MARKET_MOVE_RE = re.compile(r"^\s*(?:off|up|down|into|through)\b", re.I)

# Tier-2 only: a sentence anchored in the past is recalling a real earlier trade
# ("the exact stock I sold at a loss three weeks ago"), not claiming this run's.
_PAST_REFERENCE_RE = re.compile(
    r"\b(?:ago|previously|earlier|last\s+(?:week|month|year)|back\s+in|"
    r"yesterday|since\s+then|at\s+the\s+time)\b", re.I)


def _sentences(text: str) -> list[str]:
    """Split prose into sentences. Deliberately crude — a missed split only ever
    widens the window a claim is judged in, it never invents one."""
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text or "") if s.strip()]


def _claimed_in(sentence: str) -> bool:
    """True when this sentence asserts the agent itself executed something."""
    for m in _CLAIM_RE.finditer(sentence):
        if not _MARKET_MOVE_RE.match(sentence[m.end():]):
            return True
    return False


def _ground_for(reasons: Any) -> str:
    """Plain-English reason a proposal produced no fill, from the rejection text."""
    blob = " ".join(str(r) for r in (reasons or []) if r).lower()
    if "risk_desk_veto" in blob:
        return "vetoed by the risk desk"
    if "process_gate_blocked" in blob:
        return "blocked by a process gate"
    if "risk_desk_no_review" in blob:
        return "rejected — the risk desk returned no review for it"
    if blob:
        return "rejected by the validator"
    return "not executed"


def _as_dict(result: Any) -> dict:
    """Accept a ValidationResult object or an already-serialised dict."""
    if isinstance(result, dict):
        return {"proposal": result.get("proposal") or {},
                "approved": bool(result.get("approved")),
                "reasons": result.get("reasons") or []}
    return {"proposal": getattr(result, "proposal", None) or {},
            "approved": bool(getattr(result, "approved", False)),
            "reasons": getattr(result, "reasons", None) or []}


def unexecuted_actions(results: Any, fills: Any) -> list[dict]:
    """Every proposed action this run that did NOT produce a fill.

    Matched on (ticker, action): a BUY that filled does not excuse a SELL on the
    same name that did not. `approved` is carried so an approved-but-unfilled
    order — a real state, the order simply did not execute — reads differently
    from a rejection.
    """
    filled = {(str(f.get("ticker", "")).upper(), str(f.get("action", "")).upper())
              for f in (fills or [])
              if isinstance(f, dict) and str(f.get("status", "")).lower() == "filled"}
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for raw in (results or []):
        r = _as_dict(raw)
        ticker = str(r["proposal"].get("ticker", "")).upper().strip()
        action = str(r["proposal"].get("action", "")).upper().strip()
        if not ticker or (ticker, action) in filled or (ticker, action) in seen:
            continue
        seen.add((ticker, action))
        out.append({
            "ticker": ticker,
            "action": action,
            "approved": r["approved"],
            "ground": ("approved but the order did not fill" if r["approved"]
                       else _ground_for(r["reasons"])),
        })
    return out


def _mentions(sentence: str, ticker: str) -> bool:
    return re.search(r"\b" + re.escape(ticker) + r"\b", sentence, re.I) is not None


def build_correction(false_claims: list[dict], unexecuted: list[dict],
                     any_fills: bool) -> str:
    """The deterministic notice published above the prose. Facts only."""
    named = sorted({c["ticker"] for c in false_claims})
    by_ticker = {u["ticker"]: u for u in unexecuted}
    lines = []
    if named:
        detail = "; ".join(
            f"{t} {by_ticker.get(t, {}).get('action', 'BUY').replace('_', ' ').lower()} "
            f"was {by_ticker.get(t, {}).get('ground', 'not executed')}"
            for t in named)
        lines.append(
            "Correction (automatic): the commentary below describes a trade that "
            f"did not happen. {detail}. "
            + ("No position was opened or closed for "
               + ", ".join(named) + " on this run.")
        )
    else:
        detail = "; ".join(
            f"{u['ticker']} {u['action'].replace('_', ' ').lower()} was {u['ground']}"
            for u in unexecuted)
        lines.append(
            "Correction (automatic): the commentary below reads as though a trade "
            f"was executed. It was not. {detail}."
        )
    if not any_fills:
        lines.append("This run executed no trades.")
    lines.append("The open positions table is built from broker state and is "
                 "authoritative.")
    return " ".join(lines)


def reconcile(commentary: Any, results: Any, fills: Any) -> dict:
    """Check published prose against what the run actually executed.

    Returns a report; `correction` is None when the prose and the fills agree,
    which is the overwhelmingly common case.
    """
    text = commentary if isinstance(commentary, str) else ""
    unexecuted = unexecuted_actions(results, fills)
    report = {
        "checked": bool(text) and bool(unexecuted),
        "unexecuted": unexecuted,
        "false_claims": [],
        "correction": None,
        "issues": [],
    }
    # THE QUIET GUARD: with nothing proposed-but-unexecuted there is no
    # contradiction available, and an execution-sounding sentence is a reference
    # to a real past trade. Do not flag it.
    if not report["checked"]:
        return report

    any_fills = any(str(f.get("status", "")).lower() == "filled"
                    for f in (fills or []) if isinstance(f, dict))
    tickers = {u["ticker"] for u in unexecuted}
    claims: list[dict] = []
    generic: list[str] = []
    for sentence in _sentences(text):
        if not _claimed_in(sentence):
            continue
        hit = [t for t in tickers if _mentions(sentence, t)]
        if hit:
            claims.extend({"ticker": t, "sentence": sentence} for t in hit)
        elif not _PAST_REFERENCE_RE.search(sentence):
            generic.append(sentence)

    report["false_claims"] = claims
    if claims:
        report["issues"] = [f"commentary_claimed_unexecuted_trade:{c['ticker']}"
                            for c in claims]
        report["correction"] = build_correction(claims, unexecuted, any_fills)
    elif generic and not any_fills:
        # Name-only phrasing ("I bought a small position in DexCom today") with
        # the symbol absent. Only trusted when the run executed nothing at all,
        # so there is no fill the sentence could legitimately be describing.
        report["unverified_claims"] = generic
        report["issues"] = ["commentary_claimed_unexecuted_trade:unnamed"]
        report["correction"] = build_correction([], unexecuted, any_fills)
    return report
