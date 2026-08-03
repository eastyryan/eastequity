"""Signal discipline — stop the system vetoing on its own measured-dead reads.

WHY THIS EXISTS (2026-08-03).

CLAUDE.md carries a 12-year, 88,200-observation ablation study whose verdict on
roughly twenty indicator blocks is "no discriminating power", and it says in
plain words what that implies: an indicator measured as non-predictive cannot
serve as evidence FOR an entry and *equally cannot serve as grounds to REJECT
one*. It even names the incident — 2026-07-21, CRWD and GE both reached their own
stated triggers and were both rejected on MACD state.

That warning was written, and then the same failure ran for another two weeks:

  * 07-23 and 07-24, four separate runs: ANET and GE reached their own stated
    price triggers and were rejected each time for "volume confirmation not met"
    / "volume_read: neutral". The study scored `confirmed_breakout` at -0.32pp
    ATR-matched and found demanding >=1.5x volume did NOT select better breakouts.
  * 07-21: CRWD cleared its $195 level and was rejected because "the confirming
    MACD bull cross has not printed". The study scored bull_cross_recent at
    -0.01pp — statistically nothing — and CLAUDE.md explicitly says not to write
    MACD conditions into triggers.

Prose did not fix this, twice. So the enforcement is mechanical:

  1. TRIGGERS CANNOT CARRY A DEAD CONFIRMATION LEG. `sanitize_trigger` strips the
     dead clause out of a watchlist `would_buy_at` and records that it did. The
     stored trigger becomes the part that survives measurement (a price level, a
     structure, a catalyst, or `no_supply_pullback` — the one volume read that
     passed its ablation). Next cycle the alert fires on the real condition and
     the run cannot hide behind an unmet leg it should never have written.

  2. DEAD-INDICATOR VETOES ARE NAMED. `dead_indicator_veto` scans a rejection
     rationale and reports when the ONLY substantive ground is a measured-dead
     read, so the miss is visible in the journal and fed back to the next run
     instead of reading as ordinary discipline.

  3. AN UNWIND IS NOT A HALT. `unwind_used_as_halt` catches the other documented
     failure: momentum_health flagging an unwind is a coded 0.5x SIZE haircut,
     and autonomy_config.json says so, yet ~15 runs across 07-20..07-29 cited it
     as the reason to stand down entirely on an empty book.

Pure helpers — no network, no config reads, offline-testable.
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# The measured-dead reads. Each entry: (label, pattern, measured verdict).
# Sourced from the MEASURED 2026-07-19 blocks in CLAUDE.md. ONLY reads the study
# scored as non-discriminating appear here — this list must never grow to include
# something merely unfashionable.
# ---------------------------------------------------------------------------
DEAD_SIGNALS: tuple[tuple[str, str, str], ...] = (
    ("volume_confirmation",
     r"(?:volume[\s\-_]*confirm\w*|confirm\w*\s+(?:on|with|by)\s+volume|"
     # Negated phrasings are how a veto actually reads in the journal — "volume
     # did not confirm", "neither confirmed with volume", "no volume confirmation".
     # Without these the detector missed the exact 07-23/24 rejection text.
     r"(?:no|without|lacking|missing|neither|never)\s+(?:\w+\s+){0,2}?"
     r"confirm\w*(?:\s+(?:on|with|by))?\s*(?:volume)?|"
     r"volume\s+(?:did|does|has|have|was|were)\s*(?:not|n't)\s*\w*\s*confirm\w*|"
     r"(?:did|does|has|have)\s*(?:not|n't)\s+confirm\w*(?:\s+\w+){0,2}\s+volume|"
     r"confirmed[\s\-_]*breakout|unconfirmed[\s\-_]*breakout\w*|"
     r"(?:above|on)[\s\-]*average[\s\-]*volume|"
     r"(?:\d+(?:\.\d+)?\s*x\s*(?:average\s*)?volume)|rvol\b|relative\s+volume)",
     "confirmed_breakout -0.32pp ATR-matched (21d); volume confirmation did NOT "
     "select better breakouts"),
    ("macd",
     r"\bmacd\b|bull[\s\-]*cross|bear[\s\-]*cross|histogram\s+flip",
     "bull_cross_recent -0.01pp — indistinguishable from the standing regime"),
    ("adx",
     r"\badx\b|average\s+directional",
     "breakouts with ADX>25 scored -0.35pp and were negative in 10 of 12 years"),
    ("anchored_vwap_above",
     r"(?:above|reclaim\w*|hold\w*)\s+(?:the\s+)?anchored[\s\-]*vwap",
     "above anchored VWAP -0.12pp; the level is useful for GEOMETRY, not direction"),
    ("pocket_pivot",
     r"pocket[\s\-]*pivot",
     "+0.01pp unconditional — nothing"),
    ("weekly_structure",
     r"weekly[\s\-]*(?:structure|uptrend|downtrend)|30w[\s\-]*ma|30[\s\-]*week[\s\-]*ma",
     "weekly uptrend -0.09pp, above-30w-MA -0.07pp — all inside noise"),
    ("sector_relative_strength",
     r"rel(?:ative)?[\s\-]*strength[\s\-]*(?:vs\.?|versus|against)[\s\-]*(?:its\s+)?sector|"
     r"rel_strength_vs_sector",
     "-0.09pp, and an arbitrary grouping of the same shape scored the same"),
    ("earnings_reaction",
     r"gap[\s\-]*(?:held|filled)|earnings[\s\-]*reaction|post[\s\-]*earnings[\s\-]*drift[\s\-]*flag",
     "gap_held -0.07pp while gap_filled +0.29 and down_gap +0.18 — inverted, all noise"),
)

# The single read that SURVIVED its ablation (+0.22pp / +0.25pp, 7 of 12 years).
# A trigger leaning on this is legitimate and must never be stripped.
SURVIVING_SIGNAL = re.compile(r"no[\s_\-]*supply[\s\-]*pullback", re.IGNORECASE)

_DEAD_RE = tuple((label, re.compile(pat, re.IGNORECASE), why)
                 for label, pat, why in DEAD_SIGNALS)

# Clause separators used to split a compound trigger into legs.
#
# NOT split on: "confirmed by" / "confirmed on" / "confirmed with". Splitting
# there severs the dead phrase from its own object — "close above $175,
# confirmed by volume" became the leg "volume", which matches no dead pattern
# (bare "volume" is not a signal) and survived, leaving a trigger that still
# demanded the confirmation it was supposed to lose.
_SPLIT = re.compile(
    r"\s*(?:,|;|\band\b|\bplus\b|\bwith\b|\bonce\b|\bafter\b|\bprovided\b|"
    r"\bbut only if\b|&)\s*", re.IGNORECASE)

# A leg worth keeping mentions a price, a percentage, a level, a date, or a
# structure/catalyst word. A leg that is ONLY a dead indicator has none of these.
_SUBSTANTIVE = re.compile(
    r"\$\s*\d|\d+(?:\.\d+)?\s*%|\b\d{2,}(?:\.\d+)?\b|"
    # Lookback levels — "the 20d high", "52-week low", "30-day base". These are
    # real, nameable levels; without them "above the 20d high" read as content-
    # free and a legitimate structure trigger was dropped whole.
    r"\b\d+\s*-?\s*(?:d|day|week|wk|month|mo)s?\b|\b(?:high|low|level)\b|"
    r"\b(?:base|breakout|higher\s+low|support|resistance|shelf|reclaim|pullback|"
    r"retest|close|earnings|print|guidance|catalyst|order|deal|filing|8-k|"
    r"upgrade|target|open|gap)\b", re.IGNORECASE)


# An INDEPENDENT GROUND for passing on a setup — deliberately stricter than
# _SUBSTANTIVE above, which the trigger sanitizer uses for a different question.
#
# _SUBSTANTIVE asks "does this clause contain real content?", so it counts
# structure words (base, breakout, level, high, support). _GROUND asks "is there
# an argument here that does not depend on the dead read?", and those same words
# routinely appear INSIDE a dead-indicator sentence — "still no volume
# confirmation on the breakout" is not a breakout argument, and "hit my level but
# volume did not confirm" is not a level argument. Sharing one regex made the
# detector miss the exact 07-23/24 rejections it was built for.
_GROUND = re.compile(
    r"\$\s*\d|\d+(?:\.\d+)?\s*%|"
    r"\b(?:earnings|print|reports?|reported|guidance|catalyst|binary|"
    r"valuation|multiple|p/?e|expensive|cheap|"
    r"theme|overlap|stack|concentration|correlat\w*|diversif\w*|"
    r"extended|stretched|stop|risk[\s\-]?reward|\brr\b|upside|"
    r"downgrade|estimate|revision|insider|dilut\w*|debt|leverage|margin|"
    r"competition|competitive|litigation|liquidity|illiquid|halt|regime)\b",
    re.IGNORECASE)


# "X but Y" — the rejection ground lives after the contrastive.
_CONTRASTIVE = re.compile(
    r"\b(?:but|however|though|although|yet|whereas|still)\b", re.IGNORECASE)


def dead_signals_in(text: Any) -> list[dict]:
    """Every measured-dead read named in a piece of text."""
    if not isinstance(text, str) or not text.strip():
        return []
    return [{"signal": label, "measured": why}
            for label, rx, why in _DEAD_RE if rx.search(text)]


# A dead read is usually attached to a real condition by a connective rather than
# living in its own clause: "reclaim of $195 CONFIRMED BY a MACD bull cross",
# "$345-348 higher low ON above average volume". Clause-splitting therefore does
# not find them — the dead phrase must be EXCISED IN PLACE together with the
# connective that attached it, leaving the condition that survives measurement.
_CONNECTIVE = (r"(?:\s*(?:,|;|\band\b|\bwith\b|\bon\b|\bplus\b|\bonce\b|"
               r"\bconfirmed\s+(?:by|on|with)\b|\bconfirming\b|\bprovided\b)\s*)?"
               r"(?:\s*(?:a|an|the)\s+)?\s*")
# Consume the rest of the clause the dead phrase heads, so "a MACD bull cross"
# goes entirely rather than leaving the orphan "bull cross".
#
# BUT STOP AT AN EVENT-GATE CUE (2026-08-03). A bare `[^,;]*` also swallowed the
# event half of a compound trigger: "close above $175 with volume confirmation
# after the 8/4 print" stored as "close above $175", silently converting a
# gated trigger into an ungated one. That is the AMD failure in reverse — the
# alert then fires before the catalyst, and with trigger accountability now live
# a spurious fire costs a forced resolution and counts toward the force-drop
# threshold. tools/trigger_conditions.py owns gate PARSING; this only has to
# avoid destroying the text it parses.
_EVENT_CUE = (r"after|before|post|once|until|following|ahead\s+of|prior\s+to|"
              r"past|pending|reaction\s+to|response\s+to")
_TAIL = rf"(?:(?!\s+(?:{_EVENT_CUE})\b)[^,;])*"

_EXCISE = tuple(
    (label, re.compile(_CONNECTIVE + r"(?:" + pat + r")" + _TAIL, re.IGNORECASE), why)
    for label, pat, why in DEAD_SIGNALS
)

# Same patterns without the run-on tail. The greedy tail is right for a trailing
# qualifier ("confirmed by a MACD bull cross") but wrong when the dead read
# PRECEDES a real level ("a confirmed_breakout above the 20d high") — there it
# would swallow the structure the trigger is actually about. Used as a fallback
# whenever full excision would leave nothing testable behind.
_EXCISE_NOTAIL = tuple(
    (label, re.compile(_CONNECTIVE + r"(?:" + pat + r")", re.IGNORECASE), why)
    for label, pat, why in DEAD_SIGNALS
)

# Punctuation / connectives left dangling once a dead phrase is removed.
_DANGLING = re.compile(
    r"(?:\s*(?:,|;|&|\band\b|\bwith\b|\bon\b|\bplus\b|\bonce\b|\bor\b|"
    r"\bconfirmed\s+(?:by|on|with)\b))+\s*$", re.IGNORECASE)
_LEADING = re.compile(r"^\s*(?:,|;|&|\band\b|\bwith\b|\bplus\b|\bonce\b)\s*",
                      re.IGNORECASE)


def sanitize_trigger(trigger: Any) -> dict:
    """Excise measured-dead confirmation legs from a watchlist trigger.

    Returns {trigger, original, stripped: [...], changed: bool, empty: bool}.
    `empty` means nothing testable survived — the trigger was made only of dead
    reads, and the caller should treat the name as having NO trigger rather than
    an unmeetable one. A trigger resting on `no_supply_pullback` (the single
    volume read that survived its ablation) is never touched.
    """
    original = trigger if isinstance(trigger, str) else ""
    if not original.strip():
        return {"trigger": original, "original": original, "stripped": [],
                "inline_dead": [], "changed": False, "empty": False}

    if SURVIVING_SIGNAL.search(original):
        return {"trigger": original.strip(), "original": original,
                "stripped": [], "inline_dead": [], "changed": False,
                "empty": False}

    def _excise(patterns) -> tuple[str, list[dict]]:
        text = original
        found: list[dict] = []
        for label, rx, why in patterns:
            for match in rx.findall(text):
                clause = match if isinstance(match, str) else str(match)
                if clause.strip():
                    found.append({"clause": clause.strip(),
                                  "signals": [{"signal": label, "measured": why}]})
            text = rx.sub(" ", text)
        return text, found

    def _tidy(text: str) -> str:
        text = re.sub(r"\s{2,}", " ", text).strip()
        text = _DANGLING.sub("", text)
        return _LEADING.sub("", text).strip(" ,;&")

    working, stripped = _excise(_EXCISE)

    if stripped and not _SUBSTANTIVE.search(_tidy(working)) \
            and _SUBSTANTIVE.search(original):
        # Full excision ate the real condition along with the dead qualifier.
        # Retry without the run-on tail so a level like "above the 20d high"
        # survives having "confirmed_breakout" removed from in front of it.
        retry, retry_stripped = _excise(_EXCISE_NOTAIL)
        if _SUBSTANTIVE.search(_tidy(retry)):
            working, stripped = retry, retry_stripped

    if not stripped:
        # Nothing dead to remove: the trigger stands exactly as written. Never
        # return a cosmetically reformatted version — it would be journaled as a
        # discipline fix and would overwrite the author's own wording.
        return {"trigger": original.strip(), "original": original,
                "stripped": [], "inline_dead": [], "changed": False,
                "empty": False}

    working = _tidy(working)

    if not working or not _SUBSTANTIVE.search(working):
        return {"trigger": "", "original": original, "stripped": stripped,
                "inline_dead": [], "changed": True, "empty": True}

    return {
        "trigger": working,
        "original": original,
        "stripped": stripped,
        "inline_dead": [],
        "changed": working != original.strip(),
        "empty": False,
    }


def dead_indicator_veto(reason: Any) -> dict | None:
    """Report when a pass/reject rationale rests ONLY on measured-dead reads.

    Returns None when the reason carries any independent ground (geometry, a
    catalyst, earnings timing, theme overlap, a price level). The point is not to
    forbid mentioning an indicator — CLAUDE.md wants them cited descriptively —
    it is to catch the case where a dead read IS the whole argument.
    """
    if not isinstance(reason, str) or not reason.strip():
        return None
    hits = dead_signals_in(reason)
    if not hits:
        return None
    if SURVIVING_SIGNAL.search(reason):
        return None
    # Remove the dead clauses and see whether an argument remains.
    residue = reason
    for _label, rx, _why in _DEAD_RE:
        residue = rx.sub(" ", residue)
    # In "X BUT Y", the rejection ground is Y — X is typically the condition that
    # WAS satisfied. "CRWD crossed the $195 price but the confirming MACD bull
    # cross has not printed" is not a price argument: the price was met, and the
    # only thing doing work is the MACD. Scoring the whole sentence let the
    # already-satisfied level launder a dead-read veto into looking grounded.
    tail = _CONTRASTIVE.split(residue)[-1] if _CONTRASTIVE.search(residue) else residue
    if _GROUND.search(tail):
        return None
    return {
        "signals": [h["signal"] for h in hits],
        "measured": [h["measured"] for h in hits],
        "reason": reason.strip()[:400],
        "note": ("the only stated ground is a read this system's own 12-year "
                 "ablation scored as non-discriminating; a measurement carrying "
                 "no information cannot veto a setup that stands on its own"),
    }


_UNWIND_WORDS = re.compile(
    r"momentum[\s\-]*(?:factor|health)?[\s\-]*unwind|factor[\s\-]*unwind|"
    r"\bunwind\b|mtum\b|spmo\b", re.IGNORECASE)


def unwind_used_as_halt(no_trade_reason: Any, rejected_ideas: Any,
                        *, momentum_status: str | None) -> dict | None:
    """Catch an unwind being treated as a trading halt rather than a size haircut.

    Fires only when the tape actually flagged an unwind AND the run traded
    nothing AND the unwind is doing the work in the rationale — i.e. the
    per-name reasons do not independently stand up. autonomy_config.json:
    "Momentum-unwind detection separately halves the risk budget via
    momentum_unwind_risk_scale RATHER THAN HARD-BLOCKING."
    """
    status = str(momentum_status or "").strip().lower()
    if status not in ("unwind", "softening"):
        return None
    text = no_trade_reason if isinstance(no_trade_reason, str) else ""
    if not _UNWIND_WORDS.search(text):
        return None
    ideas = rejected_ideas if isinstance(rejected_ideas, list) else []
    named = 0
    for item in ideas:
        if not isinstance(item, dict):
            continue
        r = str(item.get("reason") or "")
        if not r.strip():
            continue
        # A per-name reason that is itself just the unwind does not count as an
        # independent ground.
        if _UNWIND_WORDS.search(r) and not _SUBSTANTIVE.search(
                _UNWIND_WORDS.sub(" ", r)):
            continue
        named += 1
    if named >= 2:
        return None  # the names were rejected on their own merits — fine
    return {
        "momentum_status": status,
        "independent_reasons": named,
        "no_trade_reason": text.strip()[:400],
        "note": ("momentum_health is a coded 0.5x SIZE haircut, not a gate — the "
                 "only hard regime block is SPY below its 200-DMA. A proposal made "
                 "during an unwind is already half-sized by construction; standing "
                 "down on top of that double-counts the same caution. Reject names "
                 "on their own merits or take the half-sized trade."),
    }
