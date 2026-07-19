"""Brain invoke, risk desk, parse, execute."""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

import journal
import validator
from execution import broker, corporate_actions, exit_guard
from tools.portfolio_state import get_portfolio_state
from runlib.core import ROOT, et_date, et_now, json_safe
from runlib.depths import depth_allows_new_buys

def apply_live_prices(context: dict, cfg: dict) -> dict:
    """Overlay the high-frequency holdings/watchlist live feed onto the bundle's
    daily-bar prices BEFORE stops are enforced, and record staleness.

    The heavy gather commits daily bars sparsely; state/live_prices.json is
    refreshed every few minutes during market hours by a dedicated Action. Using
    it here means an intraday stop breach (e.g. DELL through 408) is caught on
    the next run instead of waiting for the next full gather. A snapshot older
    than the freshness window is NOT overlaid (a broken feed must not overwrite a
    good daily bar) but IS flagged so the run fails safe. Returns the freshness
    report (also stored on context['price_freshness_live'])."""
    try:
        from tools.live_prices import (
            load_live_prices, overlay_live_prices, freshness_report,
        )
    except Exception as e:
        report = {"status": f"unavailable:{str(e)[:80]}", "stale": True, "applied": []}
        context["price_freshness_live"] = report
        return report

    max_age = float(((cfg.get("schedule") or {}).get("live_prices") or {})
                    .get("max_age_min", 30))
    scan = context.setdefault("universe_scan", {})
    prices = scan.get("prices") or {}
    held = [str(p.get("ticker", "")).upper()
            for p in (context.get("portfolio", {}).get("positions") or [])
            if p.get("ticker")]

    blob = load_live_prices()
    # The feed is already scoped to holdings + watchlist by the live-prices
    # Action, so overlay every name it carries (tickers=None). The bundle has no
    # brain 'watchlist' key yet at trade time, so filtering here would drop them.
    merged, applied = overlay_live_prices(prices, blob, tickers=None,
                                          max_age_min=max_age)
    if applied:
        scan["prices"] = merged
        print(f"  • live-price overlay applied to {applied} "
              f"(age {freshness_report(blob, applied, max_age_min=max_age).get('age_min')}m)")
        # Re-mark so market values + the safety layer use the freshest price.
        if cfg.get("mode", {}).get("trading_mode") != "dry_run":
            try:
                broker.mark_to_market(merged)
                context["portfolio"] = get_portfolio_state()
            except Exception as e:
                print(f"  (live-price re-mark failed: {e})")

    report = freshness_report(blob, applied, max_age_min=max_age)
    # Stops can only reflect a holding we actually have a fresh price for.
    report["stale_holdings"] = [t for t in held if t not in applied]
    if report.get("stale") or report["stale_holdings"]:
        print(f"  ⚠ live prices stale (age {report.get('age_min')}m); "
              f"stop enforcement may lag for {report['stale_holdings'] or 'holdings'}")
    context["price_freshness_live"] = report
    return report


def apply_safety_layer(context: dict, cfg: dict, run_id: str) -> list[dict]:
    """Dividends/splits applied, then stops/horizons enforced - the brain can
    never rationalize holding through its own written plan. Returns forced fills."""
    if cfg["mode"]["trading_mode"] == "dry_run":
        return []
    ca = corporate_actions.apply_corporate_actions()
    if ca.get("events") or ca.get("errors"):
        context["corporate_actions"] = ca
        context["portfolio"] = get_portfolio_state()
        if ca.get("errors"):
            print(f"  corporate-actions errors: {ca['errors']}")

    prices = context["universe_scan"].get("prices", {})
    # ATR per name lets the broker model stop gap-through (a stop rarely fills exactly
    # at its level; price gaps beyond it). Same map the volatility-stop floor uses.
    atr_map = context["universe_scan"].get("atr_by_ticker") or {}
    forced = exit_guard.check_forced_exits(context["portfolio"], prices, atr_map)
    if not forced:
        return []
    fills = exit_guard.execute_forced_exits(forced, prices, run_id, atr_map)
    context["portfolio"] = get_portfolio_state()  # brain must see post-exit state
    context["forced_exits"] = [
        {**ex, "note": "Closed deterministically by the safety layer before this run; "
                       "do not re-propose selling it. Explain the exit in your commentary."}
        for ex in forced
    ]
    for f in fills:
        print(f"  FORCED EXIT {f['ticker']} @ {f.get('fill_price')} "
              f"({context['forced_exits'][0].get('reason', '?')})")
    return fills


# ---------------------------------------------------------------------------
# Step 3 — Wake the brain (single Claude invocation via Claude Code CLI)
# ---------------------------------------------------------------------------
def ask_claude(context: dict, run_id: str, news_only: bool = False) -> str:
    """Invoke Claude Code headlessly with CLAUDE.md rules + the context bundle.

    Writes a FULL archive (state/context_full_*.json) for audit/act-on and a
    SLIM brain pack (state/context_*.json) so learning dumps do not drown the
    non-negotiables. The brain prompt points at the slim file.
    """
    context_file = ROOT / "state" / f"context_{run_id}.json"
    full_file = ROOT / "state" / f"context_full_{run_id}.json"
    try:
        from runlib.context_tiers import write_tiered_context
        slim = write_tiered_context(context, context_file, full_file)
        # Keep caller's in-memory bundle full; only the on-disk brain path is slim.
        _ = slim
    except Exception as e:
        # Fail open: write full context so a tier bug never blocks a run.
        print(f"  (tiered context failed, writing full: {e})")
        context_file.write_text(json.dumps(json_safe(context), indent=2, default=str))
        try:
            full_file.write_text(json.dumps(json_safe(context), indent=2, default=str))
        except Exception:
            pass
    depth = context.get("run_depth") or "full"

    if depth == "weekly_market" or (news_only and depth == "weekly_market"):
        prompt = (
            "You are running the WEEKLY MARKET CHECK-IN for East Equity Agent (NO trading). "
            f"Read CLAUDE.md, then the context bundle at {context_file}. "
            "This run scanned the full multi-sector universe plus a broad discovery sweep. "
            "Write a clear market-breadth review: sector leadership (who is winning/losing), "
            "regime (supportive/neutral/hostile), any geopolitical or commodity shocks from "
            "market_events, and 3-7 names worth watching next week (watchlist). "
            "Re-underwrite holdings briefly. proposals MUST be []. "
            "no_trade_reason = 'weekly market check-in - no trading'. "
            "End with an 'Improvement note:' line."
        )
        return run_claude(prompt)

    if not news_only and depth == "light":
        prompt = (
            "You are running a LIGHT East Equity Agent check (pre-market/overnight slot). "
            f"Read CLAUDE.md, then the context bundle at {context_file}. No universe scan "
            "was done this cycle. Review each open position against its original plan and "
            "the latest news; you MAY propose SELL_TO_CLOSE if a thesis has broken, but "
            "propose NO new BUYs this run (they will be discarded). Refresh your commentary "
            "and carry the watchlist forward (update thoughts only where news changed them). "
            "Output the exact JSON block per CLAUDE.md. End with an 'Improvement note:' line."
        )
        return run_claude(prompt)

    if news_only:
        prompt = (
            "You are running a scheduled East Equity Agent NEWS REVIEW (markets are closed - "
            f"no trading this run). Read CLAUDE.md, then the context bundle at {context_file}. "
            "Review weekend/overnight news for current holdings and the whole universe: "
            "earnings reports, guidance changes, analyst moves, macro developments. Update "
            "your view of each holding and each watchlist name. Output the JSON block from "
            "CLAUDE.md with proposals REQUIRED to be an empty list, no_trade_reason set to "
            "'news review - markets closed', fresh commentary (lead with anything that "
            "changes Monday's plan), and an updated watchlist. End with an "
            "'Improvement note:' line."
        )
        return run_claude(prompt)

    if depth == "holdings_watchlist":
        prompt = (
            f"Today's date in the US market timezone (ET) is {et_date()} - use THIS date in "
            f"any prose, never the UTC run_date. "
            "You are running a FOCUSED East Equity Agent cycle (holdings + watchlist + "
            "tape/8-K promotions). "
            f"Read CLAUDE.md, then the context bundle at {context_file}. "
            "Follow reasoning_process.process_checklist order: regime → book → idea → "
            "geometry → kill. Use stack_cards for differential (layer/customers/substitutes). "
            "Check prices_meta price_as_of before chasing triggers. "
            "Re-underwrite holdings vs plan + invalidators. EVERY watchlist entry needs "
            "status: drop|hold|buy. Honor watchlist_feedback hits_not_bought. "
            "Every BUY: thesis_invalidators + demand_driver + stack differential. "
            "Output exact JSON per CLAUDE.md. End with 'Improvement note:'."
        )
        return run_claude(prompt)

    prompt = (
        f"Today's date in the US market timezone (ET) is {et_date()} - use THIS date in "
        f"any prose, never the UTC run_date. "
        "FULL research depth. Read CLAUDE.md + context at "
        f"{context_file}. "
        "Process: regime → book (theme_exposure) → idea (scan/PED/tape/stack_cards) → "
        "geometry → kill. Use stack_cards.by_ticker for supply-chain differential. "
        "Use universe_scan.prices_meta[T].price_as_of for freshness. "
        "EVERY watchlist row needs status drop|hold|buy. "
        "If proposals is empty: REQUIRED rejected_ideas array with >=2 objects "
        "{{ticker, reason}} for scan ideas you passed on (not only free-text mood). "
        "Every BUY: variant_perception, scenarios, thesis_invalidators, demand_driver. "
        "Output exact JSON per CLAUDE.md. End with 'Improvement note:'."
    )
    return run_claude(prompt)


def llm_settings() -> dict:
    """The llm config block, fail-soft: missing block/file -> {} (CLI defaults)."""
    try:
        return validator.load_config().get("llm") or {}
    except Exception:
        return {}


def claude_cmd(prompt: str, model: str | None, allowed_tools: str | None) -> list[str]:
    """Build the headless claude invocation (pure - offline-testable).

    Pinning --model keeps the public track record reproducible across CLI-default
    changes. --allowedTools grants exactly what CLAUDE.md instructs the brain to
    do (Read the chart PNGs, WebSearch/WebFetch to verify catalysts) and nothing
    that can write - an injected headline must never be able to touch the ledger.
    The installed CLI accepts a space/comma-separated tool list as one argument."""
    cmd = ["claude", "-p", prompt]
    if model:
        cmd += ["--model", model]
    if allowed_tools:
        cmd += ["--allowedTools", allowed_tools]
    return cmd


def run_claude(prompt: str, model: str | None = None) -> str:
    import time
    last_err = ""
    llm = llm_settings()
    model = model or llm.get("brain_model")
    allowed_tools = llm.get("allowed_tools")
    # NOTE: no --permission-mode plan. Newer Claude Code (2.x) diverts a plan-mode
    # answer to a plan file and returns only a prose summary on stdout, so our JSON
    # block never appears and parsing falls back to a no-trade. The default headless
    # mode prints the full response (incl. the fenced ```json) to stdout, which is what
    # we parse - matching universe_review(), which already runs claude -p without plan mode.
    for attempt in (1, 2):  # one retry: overnight runs can hit transient/usage errors
        result = subprocess.run(
            claude_cmd(prompt, model, allowed_tools),
            cwd=ROOT, capture_output=True, text=True, timeout=1800,
        )
        if result.returncode == 0:
            return result.stdout
        last_err = (f"exit={result.returncode} stderr={result.stderr[:1000]} "
                    f"stdout={result.stdout[:1000]}")
        print(f"  claude attempt {attempt} failed: {last_err[:300]}")
        if attempt == 1:
            time.sleep(90)
    raise RuntimeError(f"claude CLI failed after retry: {last_err}")


# ---------------------------------------------------------------------------
# Adversarial risk desk: an independent skeptic tries to kill every BUY.
# ---------------------------------------------------------------------------
# Risk-desk-vetoed proposals from the LAST adversarial_review call, as
# ValidationResult-shaped records. The orchestrator folds these into the
# published proposals list so the site can show WHY a trade the brain's
# commentary announced never happened (ABT 7/17: commentary said "I am
# buying Abbott" while the veto left the site with an empty proposals list).
LAST_RISK_DESK_VETOES: list = []


def adversarial_review(proposals: list[dict], context_file: str, run_id: str) -> list[dict]:
    """Second pass by a separate Claude session prompted to REFUTE each BUY.
    Veto drops the proposal (journaled); survivors may get a confidence haircut.
    Skipped where the claude CLI is unavailable (cloud sandboxes) - noted loudly."""
    import shutil
    LAST_RISK_DESK_VETOES.clear()
    buys = [p for p in proposals if str(p.get("action", "")).upper() == "BUY"]
    if not buys:
        return proposals

    def _require_desk() -> bool:
        """risk_controls.require_risk_desk_for_buys, read defensively."""
        try:
            rc = validator.load_config().get("risk_controls") or {}
        except Exception:
            return False
        return bool(rc.get("require_risk_desk_for_buys"))

    def _no_desk(reason: str) -> list[dict]:
        """CLI absent or review failed: pass-through by default, or reject BUYs
        when risk_controls.require_risk_desk_for_buys is set (cloud discipline)."""
        if not _require_desk():
            print(f"  ({reason} - proposals pass unreviewed)")
            # Leave a public trace: CLAUDE.md promises every BUY an adversarial
            # review, so a run where that layer silently didn't exist must be
            # visible in the journal, not just a log line nobody reads.
            journal.log_improvement(
                f"Risk desk did not run ({reason}) - {len(buys)} BUY proposal(s) "
                f"passed to the validator unreviewed this run.", run_id)
            return proposals
        kept = []
        for p in proposals:
            if str(p.get("action", "")).upper() == "BUY":
                print(f"  RISK DESK REQUIRED - BUY {p.get('ticker')} rejected ({reason})")
                journal.log_rejection(p, [f"risk_desk_unavailable:{reason}"], run_id)
            else:
                kept.append(p)
        return kept

    if shutil.which("claude") is None:
        return _no_desk("risk desk unavailable in this environment")
    prompt = (
        "You are the RISK DESK for a paper-trading fund. Your ONLY job is to try to KILL "
        "each BUY. Another analyst proposed: "
        f"{json.dumps(buys)}. Full context: {context_file} - read it.\n"
        "KILL CHECKLIST (answer each for every BUY; veto if any hard fail):\n"
        "1) FALSIFIERS: Are thesis_invalidators specific and observable "
        "(invalidating_print, invalidating_structure, time_box)? Vague = veto.\n"
        "2) VARIANT PERCEPTION: Is consensus a straw man? Is the mechanism vague? "
        "Is there a dated resolution event? Weak = veto or haircut.\n"
        "3) THEME OVERLAP: demand_driver vs open positions / theme_exposure / "
        "portfolio_risk. Same hyperscaler_server_capex (or other driver) stack "
        "without a distinct catalyst = veto or force smaller size via haircut.\n"
        "4) GEOMETRY: stop inside noise? target <10%? RR flattered? earnings too close?\n"
        "5) FRESHNESS: if data_quality/stale_data_notice or price_freshness says stale "
        "on a catalyst day, haircut or veto chasing.\n"
        "6) CHARTS: if charts missing for the ticker, haircut confidence.\n"
        "Be a skeptic, not a contrarian for sport - approve genuinely sound trades.\n"
        "Output ONLY a ```json block: {\"reviews\": [{\"ticker\": \"X\", "
        "\"verdict\": \"approve\"|\"veto\", \"objection\": \"one paragraph covering the "
        "checklist\", \"confidence_adjustment\": 0.0}]} where confidence_adjustment is "
        "0 or negative (max -0.10) for approved-with-reservations."
    )
    try:
        out = run_claude(prompt, model=llm_settings().get("risk_desk_model"))
    except Exception as e:
        return _no_desk(f"risk desk failed ({str(e)[:120]})")
    reviews = {}
    for block in reversed(re.findall(r"```json\s*(.*?)```", out, re.DOTALL)):
        try:
            data = json.loads(block)
            if "reviews" in data:
                reviews = {r["ticker"].upper(): r for r in data["reviews"]}
                break
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
            continue
    if not reviews:
        return _no_desk("risk desk output unparsable")
    kept = []
    for p in proposals:
        r = reviews.get(str(p.get("ticker", "")).upper())
        is_buy = str(p.get("action", "")).upper() == "BUY"
        if r is None and is_buy:
            # FAIL CLOSED on an UNREVIEWED buy. This used to fall through to
            # kept.append(p): a desk response that returned valid JSON but simply
            # omitted a ticker waved that BUY through completely unreviewed, with no
            # journal line and no console warning — and _no_desk never fired, because
            # `reviews` was non-empty. A truncated or lazy desk reply therefore
            # bypassed the entire adversarial pass while looking like it had run.
            # Silence is not approval.
            if _require_desk():
                miss = ("risk_desk_no_review: the desk returned reviews but none for "
                        "this ticker — an unreviewed BUY is treated as vetoed")
                print(f"  RISK DESK MISSING REVIEW {p.get('ticker')}: rejected")
                journal.log_rejection(p, [miss], run_id)
                LAST_RISK_DESK_VETOES.append(
                    validator.ValidationResult(proposal=p, approved=False,
                                               reasons=[miss]))
                continue
            print(f"  (risk desk returned no review for {p.get('ticker')} — "
                  f"kept, require_risk_desk_for_buys is off)")
            kept.append(p)
            continue
        if r is None or not is_buy:
            kept.append(p)
            continue
        if r.get("verdict") == "veto":
            print(f"  RISK DESK VETO {p['ticker']}: {r.get('objection', '')[:120]}")
            # Full objection text: the veto rationale is the audit trail for a
            # killed trade (the 300-char slice lost the actual grounds — the
            # ABT 7/17 veto's stated reason was cut mid-sentence).
            veto_reason = f"risk_desk_veto: {r.get('objection', '')[:2000]}"
            journal.log_rejection(p, [veto_reason], run_id)
            LAST_RISK_DESK_VETOES.append(
                validator.ValidationResult(proposal=p, approved=False,
                                           reasons=[veto_reason]))
            continue
        try:
            adj = min(float(r.get("confidence_adjustment", 0) or 0), 0)
        except (TypeError, ValueError):
            adj = 0  # non-numeric desk output: keep the proposal, no haircut
        if adj:
            p["confidence"] = round(max(p.get("confidence", 0) + adj, 0), 2)
            p["risk_desk_note"] = r.get("objection", "")[:300]
            print(f"  risk desk haircut {p['ticker']}: {adj} -> {p['confidence']}")
        kept.append(p)
    return kept


# ---------------------------------------------------------------------------
# Step 4 — Parse structured proposals out of the brain's response
# ---------------------------------------------------------------------------
def parse_proposals(response: str) -> dict:
    """Extract the structured output block: proposals, commentary, watchlist. Accepts a
    fenced ```json block or, as a fallback, a bare JSON object containing a 'proposals'
    key (so a run still parses if the model omits the fence)."""
    candidates = re.findall(r"```json\s*(.*?)```", response, re.DOTALL)
    # Fallback: any brace-balanced object mentioning "proposals", fence or not.
    for m in re.finditer(r'\{[^{}]*"proposals"[\s\S]*?\}\s*$', response):
        candidates.append(m.group(0))
    for block in reversed(candidates):  # last valid block wins
        try:
            data = json.loads(block)
            if isinstance(data, dict) and "proposals" in data:
                # Shape-guard every list field: "proposals": null / a dict / a
                # string must degrade to the no-trade path, not crash the run
                # after the LLM spend.
                def _dicts(v, cap=None):
                    out = [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []
                    return out[:cap] if cap else out
                return {
                    "proposals": _dicts(data["proposals"]),
                    "no_trade_reason": data.get("no_trade_reason"),
                    "commentary": data.get("commentary"),
                    "watchlist": _dicts(data.get("watchlist"), 10),
                    "rejected_ideas": _dicts(data.get("rejected_ideas")),
                    "x_post": data.get("x_post"),
                    "guidance_entries": _dicts(data.get("guidance_entries"), 20),
                    "universe_candidates": _dicts(data.get("universe_candidates"), 5),
                }
        except json.JSONDecodeError:
            continue
    # Graceful public-facing fallback (this text can surface on the dashboard).
    return {"proposals": [],
            "no_trade_reason": "No trade this run; the agent's written review did not include a "
                               "machine-readable order block, so no orders were placed.",
            "commentary": None, "watchlist": [], "rejected_ideas": [],
            "x_post": None, "guidance_entries": [], "universe_candidates": []}


# ---------------------------------------------------------------------------
# Step 6 — Execution (approved proposals only; broker readback required)
# ---------------------------------------------------------------------------
def execute(approved: list[validator.ValidationResult], context: dict,
            cfg: dict, run_id: str) -> list[dict]:
    fills = []
    prices = context["universe_scan"].get("prices", {})
    atr_map = context["universe_scan"].get("atr_by_ticker") or {}
    max_orders = cfg["risk_controls"]["max_orders_per_run"]

    if cfg["mode"]["trading_mode"] == "dry_run":
        print("  DRY RUN — approved proposals journaled, no orders placed.")
        return fills

    # Per-DAY new-position cap across multiple intraday runs (validator only sees one batch).
    today = datetime.now(timezone.utc).date().isoformat()
    trades_file = ROOT / "journal" / "trades" / f"{today}.jsonl"
    buys_today = 0
    if trades_file.exists():
        for line in trades_file.read_text().splitlines():
            # Guarded for the same reason the intents loop below already is: a
            # truncated JSONL line from a crash mid-append must not abort execution
            # with proposals already validated.
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                print("  (warning: unparseable line in today's trade journal — "
                      "daily BUY cap may under-count)")
                continue
            if rec.get("fill", {}).get("action") == "BUY":
                buys_today += 1
    # BUY intents queued for the Actions executor count against the daily cap
    # too — they are commitments, just not filled yet (cloud->executor latency).
    intents_file = ROOT / "journal" / "intents" / f"{today}.jsonl"
    if intents_file.exists():
        for line in intents_file.read_text().splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("order", {}).get("action") == "BUY" \
                    and rec.get("status") in broker.QUEUED_STATUSES:
                buys_today += 1
    max_buys_per_day = cfg["swing_rules"]["max_new_positions_per_day"]

    # risk_controls.market_hours_only: BUYs place only during the regular session
    # (Mon-Fri 9:30-4 ET). Pre-market slots become research/exit-only - this also
    # closes a look-ahead exploit where a 6am BUY filled at YESTERDAY's close
    # after overnight news. Sells and forced exits are always allowed (risk-reducing).
    market_hours_only = cfg["risk_controls"].get("market_hours_only", False)

    for vr in approved[:max_orders]:
        p = vr.proposal
        # Normalize ONCE and write it back. validator.py upper()s every comparison,
        # so a lowercase "buy" validated cleanly and then bypassed every gate below
        # (daily cap, market hours, entry-price ceiling) AND plan construction —
        # leaving a filled position with plan=None, which exit_guard skips outright
        # ("if not plan: continue"). One case variation produced a permanently
        # unstopped position that contributed zero portfolio heat. The action is
        # written back onto the proposal so place_order and the broker's own
        # branches see the same canonical value.
        p["action"] = str(p.get("action", "")).strip().upper()
        if p["action"] == "BUY" and buys_today >= max_buys_per_day:
            journal.log_rejection(p, [f"max_new_positions_per_day_reached:{buys_today}"], run_id)
            continue
        if p["action"] == "BUY" and market_hours_only and not validator.is_market_hours(et_now()):
            journal.log_rejection(p, ["outside_market_hours"], run_id)
            continue
        ref = prices.get(p["ticker"].upper())
        if ref is None:
            journal.log_rejection(p, ["no_reference_price_available"], run_id)
            continue
        if p["action"] == "BUY" and ref > float(p["entry_price_max"]):
            journal.log_rejection(p, [f"price_above_entry_max:{ref}>{p['entry_price_max']}"], run_id)
            continue
        # Snapshot position before sell so exit autopsy retains plan/days_held.
        pos_before = None
        if str(p.get("action", "")).upper() == "SELL_TO_CLOSE":
            try:
                pos_before = next(
                    (x for x in (get_portfolio_state().get("positions") or [])
                     if str(x.get("ticker", "")).upper() == str(p.get("ticker", "")).upper()),
                    None)
            except Exception:
                pos_before = None
        plan = None
        if p["action"] == "BUY":
            plan = {k: p.get(k) for k in (
                "stop_loss", "target_price", "holding_horizon_days",
                "entry_price_max", "confidence", "demand_driver",
                "thesis_invalidators")}
        order = broker.place_order({
            "ticker": p["ticker"], "action": p["action"],
            "position_size_usd": p.get("position_size_usd"),
            "reference_price": ref, "proposal_id": run_id,
            "sell_fraction": p.get("sell_fraction"),  # partial exits (validated)
            # ATR lets the broker vol-scale slippage and model an entry gap (a $400 name
            # swinging 7%/day costs more to fill than a flat 10bps implies).
            "atr_pct": atr_map.get(p["ticker"].upper()),
            # Numeric plan (+ theme/falsifiers) persisted ONTO the position at fill.
            "plan": plan,
            "demand_driver": p.get("demand_driver"),
        })
        # risk_controls.require_broker_readback_confirmation: readback is ALWAYS
        # performed and a non-filled readback is a rejection - the flag documents
        # the contract and can only ever tighten it, never loosen it.
        fill = broker.readback(order["order_id"])  # mandatory readback
        status = (fill or {}).get("status")
        if fill is not None and status in broker.QUEUED_STATUSES + broker.RESTING_STATUSES:
            # Cloud node queued the order for the Actions executor (or the order
            # is resting at the broker until the next session). This is neither
            # a fill nor a rejection: journal the intent; the executor/reconcile
            # journals the real trade when it completes.
            journal.log_intent(order, status, run_id)
            if p["action"] == "BUY" and status in broker.QUEUED_STATUSES:
                buys_today += 1
            print(f"  QUEUED {p['action']} {p['ticker']} ({status}) — "
                  f"executor completes the fill")
            continue
        if fill is None or status != "filled":
            journal.log_rejection(p, [f"fill_failed:{status}"], run_id)
            continue
        journal.log_trade(order, fill, run_id)
        fills.append(fill)
        if fill["action"] == "BUY":
            buys_today += 1
            try:
                from tools.shadow_portfolio import close_shadow_if_bought
                close_shadow_if_bought(fill.get("ticker"), fill.get("fill_price"))
            except Exception:
                pass
        elif fill["action"] == "SELL_TO_CLOSE":
            try:
                from tools.exit_autopsy import (
                    build_exit_autopsy_from_fill, grade_and_persist_autopsy,
                )
                rec = build_exit_autopsy_from_fill(
                    fill, order, pos_before, forced=False,
                    reason=str(p.get("thesis") or p.get("exit_reason") or "")[:300])
                if p.get("thesis"):
                    rec["brain_exit_thesis"] = str(p["thesis"])[:500]
                grade_and_persist_autopsy(rec)
            except Exception as e:
                print(f"  (exit autopsy skipped: {e})")
        print(f"  FILLED {fill['action']} {fill['ticker']} "
              f"{fill['quantity']} @ {fill['fill_price']}")
    return fills
