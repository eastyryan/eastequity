"""Brain invoke, risk desk, parse, execute."""
from __future__ import annotations

import itertools
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
        # TELL THE TRUTH ABOUT FRESHNESS, not just the price.
        #
        # CLAUDE.md instructs the brain to read universe_scan.prices_meta[T].price_as_of
        # to decide whether a price is current ("Use universe_scan.prices_meta[T]
        # .price_as_of for freshness"). But the overlay only ever rewrote
        # universe_scan.prices, so an intraday quote kept wearing the daily bar's
        # provenance: source "daily_bar", price_as_of = the PREVIOUS session. The
        # bundle then disagreed with itself — a fresh number labelled stale.
        #
        # That is not cosmetic. On 2026-07-22 all three BUY proposals died at the risk
        # desk for exactly this: each tried to reconcile a stale-looking price against
        # same-bundle intraday news, and DDOG's fabricated a $311 JMP target that
        # appears nowhere in the bundle to explain the gap. A brain shown a current
        # price and told it is yesterday's close will either distrust a good price or
        # invent a story for the discrepancy. Both cost trades.
        #
        # The daily bar is preserved under daily_bar_close so nothing is lost.
        # Fail-soft: provenance bookkeeping must never be able to break a run.
        try:
            meta = scan.setdefault("prices_meta", {})
            live_asof = (blob or {}).get("as_of")
            live_src = (blob or {}).get("source") or "live_intraday"
            for t in applied:
                row = meta.setdefault(t, {})
                if row.get("source") == "daily_bar" and "daily_bar_close" not in row:
                    row["daily_bar_close"] = row.get("last_close")
                row["last_close"] = merged[t]
                row["price_as_of"] = live_asof
                row["source"] = live_src
        except Exception as e:
            print(f"  (prices_meta provenance update failed: {e})")
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
        return run_claude(prompt, call=f"brain:{depth}", run_id=run_id)

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
        return run_claude(prompt, call=f"brain:{depth}", run_id=run_id)

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
        return run_claude(prompt, call=f"brain:{depth}", run_id=run_id)

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
        return run_claude(prompt, call=f"brain:{depth}", run_id=run_id)

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
    return run_claude(prompt, call=f"brain:{depth}", run_id=run_id)


def llm_settings() -> dict:
    """The llm config block, fail-soft: missing block/file -> {} (CLI defaults)."""
    try:
        return validator.load_config().get("llm") or {}
    except Exception:
        return {}


# Claude Code tool names → Grok CLI tool ids. autonomy_config used to carry the
# Claude allowlist; either form is accepted so a stale checkout still pins tools.
_CLAUDE_TO_GROK_TOOLS = {
    "Read": "read_file",
    "Glob": "list_dir",
    "Grep": "grep",
    "WebSearch": "web_search",
    "WebFetch": "web_fetch",
}


def normalize_tools(allowed_tools: str | None) -> str | None:
    """Comma-separated Grok tool ids. Writers (bash/edit) are never granted here."""
    if not allowed_tools:
        return None
    parts = [p.strip() for p in allowed_tools.replace(",", " ").split() if p.strip()]
    if not parts:
        return None
    return ",".join(_CLAUDE_TO_GROK_TOOLS.get(p, p) for p in parts)


def grok_cmd(prompt: str, model: str | None, allowed_tools: str | None,
             output_format: str | None = None) -> list[str]:
    """Build the headless grok invocation (pure - offline-testable).

    Pinning --model keeps the public track record reproducible across CLI-default
    changes. --tools is an allowlist: Read/grep/list + web search/fetch to verify
    catalysts, and nothing that can write. An injected headline must never be
    able to touch the ledger. --yolo is required on GitHub Actions (no TTY).
    -p is last so a flag cannot be swallowed as the prompt.
    """
    cmd = ["grok", "--yolo", "--no-auto-update"]
    if model:
        cmd += ["--model", model]
    tools = normalize_tools(allowed_tools)
    if tools:
        cmd += ["--tools", tools]
        # Belt: even if a future default injects writers, deny them.
        cmd += ["--disallowed-tools", "run_terminal_cmd,search_replace"]
    if output_format:
        cmd += ["--output-format", output_format]
    cmd += ["-p", prompt]
    return cmd


# Backward-compatible name. Tests and orchestrator._claude_cmd still import this.
claude_cmd = grok_cmd


def unwrap_cli_json(stdout: str) -> tuple[str, dict]:
    """Split a --output-format json envelope into (response_text, telemetry).

    FAIL-SAFE BY CONTRACT: anything that is not a recognisable envelope is returned
    unchanged as the response text with empty telemetry, which is exactly the
    behaviour before telemetry existed. A CLI upgrade that changes this shape must
    cost us the metrics, never the run - the parse path downstream (parse_proposals)
    is load-bearing and this is not.
    """
    if not isinstance(stdout, str) or not stdout.lstrip().startswith("{"):
        return stdout, {}
    try:
        env = json.loads(stdout)
    except Exception:
        return stdout, {}
    if not isinstance(env, dict):
        return stdout, {}
    # Claude Code used `result`; grok --output-format json uses `text`.
    # Accept either so a CLI envelope change costs us metrics, never the parse.
    text = env.get("result") if isinstance(env.get("result"), str) else env.get("text")
    if not isinstance(text, str):
        return stdout, {}
    usage = env.get("usage") if isinstance(env.get("usage"), dict) else {}
    # modelUsage is keyed by the model that ACTUALLY served the request, so it
    # verifies the --model pin took effect rather than trusting that it did. The
    # public track record's reproducibility claim rests on that pin.
    model_usage = env.get("modelUsage") if isinstance(env.get("modelUsage"), dict) else {}
    # permission_denials records tools the allowlist blocked. CLAUDE.md declares
    # "WebSearch MANDATORY before any BUY"; a non-empty list here means the brain
    # tried to reach for something and could not — previously invisible.
    denials = env.get("permission_denials") if isinstance(
        env.get("permission_denials"), list) else []
    # A COUNT is half an instrument. The 2026-07-19 weekly run reported
    # permission_denials: 1 and there was no way to learn which tool the brain
    # reached for — so the signal said "something was blocked" and nothing else.
    # Names only; the arguments can carry bundle content and this record is
    # committed to a public repo.
    denied_tools = sorted({
        str(d.get("tool_name") or d.get("tool") or d.get("name") or "unknown")
        for d in denials if isinstance(d, dict)
    }) or None
    meta = {
        "duration_ms": env.get("duration_ms"),
        "duration_api_ms": env.get("duration_api_ms"),
        "num_turns": env.get("num_turns"),
        # NOTIONAL, NOT BILLED. The brain authenticates with a subscription (OAuth
        # via ~/.claude/.credentials.json); there is no ANTHROPIC_API_KEY anywhere
        # in this system. The CLI still reports what the same tokens would have
        # cost on the API, which is the only comparable unit we have — so read this
        # as QUOTA CONSUMED and as the relative weight of one call type against
        # another (daily_study ~$1.6 vs a full brain call ~$5), never as money out.
        "total_cost_usd": env.get("total_cost_usd"),
        "session_id": env.get("session_id") or env.get("sessionId"),
        "is_error": env.get("is_error"),
        "subtype": env.get("subtype"),
        "stop_reason": env.get("stop_reason") or env.get("stopReason"),
        "models_served_by": sorted(model_usage) or None,
        "permission_denials": len(denials) or None,
        "permission_denied_tools": denied_tools,
        # 0 is a MEANINGFUL value here, not a missing one: it says the brain ran
        # no web search this call. CLAUDE.md makes WebSearch mandatory before any
        # BUY and nothing had ever verified it. Kept even when zero (see the
        # is-not-None filter below) precisely so a zero is auditable.
        "web_search_requests": (usage.get("server_tool_use") or {}).get(
            "web_search_requests"),
        "web_fetch_requests": (usage.get("server_tool_use") or {}).get(
            "web_fetch_requests"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
    }
    return text, {k: v for k, v in meta.items() if v is not None}


def run_claude(prompt: str, model: str | None = None, *,
               call: str = "brain", run_id: str = "") -> str:
    """Invoke the brain and RECORD the call.

    Until 2026-07-19 nothing anywhere logged what a brain call cost, how long it
    took, which model actually ran, or whether it succeeded first try. The system
    made 15-30 Opus calls a day with no cost signal and no way to tell a run that
    thought hard from one that returned in four seconds.

    Three behaviours worth knowing:
      * Telemetry is best-effort. A CLI whose envelope we cannot parse costs us the
        metrics and nothing else - see unwrap_cli_json.
      * A TIMEOUT IS NOW A HANDLED FAILURE. subprocess.TimeoutExpired is not a
        CalledProcessError and was not caught by the returncode branch, so a hung
        CLI propagated all the way out of main(): no run summary, no journal line,
        no dashboard update, just a traceback in cron.log. A 30-minute hang burned
        the slot and left no record of itself.
      * EVERY LAUNCH FAILURE IS NOW RECORDED TOO (2026-08-03). Only TimeoutExpired
        was caught, so anything else subprocess.run can raise before the child
        answers - FileNotFoundError when the `grok` binary is not on PATH, an
        OSError on a fork/pipe failure - escaped with ZERO telemetry. That is not
        hypothetical: adversarial_review guards itself with shutil.which("grok"),
        but ask_claude, reviews.self_review / daily_study / universe_review and
        publish._brain_trade_memo do not, and each of those callers wraps the call
        in a bare `except Exception` that prints and returns. On a node without the
        CLI the system therefore failed silently AND recorded nothing about it. A
        call that errors is the one you most want a record of.
    """
    import time
    last_err = ""
    llm = llm_settings()
    model = model or llm.get("brain_model")
    allowed_tools = llm.get("allowed_tools")
    timeout_s = int(llm.get("timeout_seconds") or 1800)
    # NOTE: no --permission-mode plan. Newer Claude Code (2.x) diverts a plan-mode
    # answer to a plan file and returns only a prose summary on stdout, so our JSON
    # block never appears and parsing falls back to a no-trade. The default headless
    # mode prints the full response (incl. the fenced ```json) to stdout, which is what
    # we parse - matching universe_review(), which already runs claude -p without plan mode.
    seq = next(_CALL_SEQ)
    for attempt in (1, 2):  # one retry: overnight runs can hit transient/usage errors
        started = time.time()
        uid = call_uid(call, run_id, attempt, seq)
        _log_brain_call_start(call, run_id, model, attempt, timeout_s, uid=uid)
        try:
            result = subprocess.run(
                claude_cmd(prompt, model, allowed_tools, output_format="json"),
                cwd=ROOT, capture_output=True, text=True, timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            last_err = f"timeout after {timeout_s}s"
            _log_brain_call(call, run_id, model, allowed_tools, attempt,
                            elapsed_s=round(time.time() - started, 1),
                            ok=False, error=last_err, uid=uid)
            print(f"  grok attempt {attempt} timed out after {timeout_s}s")
            if attempt == 1:
                time.sleep(90)
            continue
        except Exception as e:
            # Deliberately broad, and deliberately NOT re-raised on attempt 1: a
            # missing CLI is permanent and a fork failure is usually transient, and
            # from here the two are the same 90-second retry. What matters is that
            # both now leave a journal line instead of vanishing into a caller's
            # `except Exception: print(...)`.
            last_err = f"{type(e).__name__}: {str(e)[:400]}"
            _log_brain_call(call, run_id, model, allowed_tools, attempt,
                            elapsed_s=round(time.time() - started, 1),
                            ok=False, error=f"launch_failed {last_err}", uid=uid)
            print(f"  grok attempt {attempt} could not launch: {last_err[:300]}")
            if attempt == 1:
                time.sleep(90)
            continue
        elapsed = round(time.time() - started, 1)
        if result.returncode == 0:
            text, meta = unwrap_cli_json(result.stdout)
            _log_brain_call(call, run_id, model, allowed_tools, attempt,
                            elapsed_s=elapsed, ok=True, meta=meta,
                            response_chars=len(text or ""), uid=uid)
            return text
        last_err = (f"exit={result.returncode} stderr={result.stderr[:1000]} "
                    f"stdout={result.stdout[:1000]}")
        _log_brain_call(call, run_id, model, allowed_tools, attempt,
                        elapsed_s=elapsed, ok=False, error=last_err[:500], uid=uid)
        print(f"  grok attempt {attempt} failed: {last_err[:300]}")
        if attempt == 1:
            time.sleep(90)
    raise RuntimeError(f"grok CLI failed after retry: {last_err}")


# Per-process invocation counter. `call` alone does NOT identify an invocation:
# adversarial_review runs the desk TWICE in one run when a repairable veto goes
# through the repair round (2026-08-03), so two records would otherwise share the
# same key and the start/outcome pairing below would silently mis-match them.
_CALL_SEQ = itertools.count(1)


def call_uid(call: str, run_id: str, attempt: int, seq: int) -> str:
    """Correlation key joining a started breadcrumb to its outcome record.

    Pure so the rollup can reason about it without importing process state: an
    unpaired start is unambiguously a call that began and never returned - a
    sandbox teardown, a SIGKILL, a Mac that slept mid-call - rather than a
    bookkeeping mismatch."""
    return f"{run_id or 'unknown'}:{call}:{seq}:{attempt}"


def _log_brain_call_start(call: str, run_id: str, model, attempt: int,
                          timeout_s: int, *, uid: str = "") -> None:
    """Breadcrumb written BEFORE the CLI launches. Never raises.

    See journal.log_brain_call_start for why this exists. Routed through its OWN
    journal function rather than log_brain_call so that a started record can never
    be counted as an invocation outcome by anything reading the stream."""
    try:
        import journal
        journal.log_brain_call_start({
            "call": call,
            "call_uid": uid,
            "model": model,
            "attempt": attempt,
            "timeout_s": timeout_s,
        }, run_id or "unknown")
    except Exception as e:  # pragma: no cover - telemetry is never load-bearing
        print(f"  (brain-call start breadcrumb skipped: {e})")


def _log_brain_call(call: str, run_id: str, model, allowed_tools, attempt: int,
                    *, elapsed_s: float, ok: bool, meta: dict | None = None,
                    error: str = "", response_chars: int = 0,
                    uid: str = "") -> None:
    """Journal one LLM invocation. Never raises - telemetry must not break a run."""
    try:
        import journal
        journal.log_brain_call({
            "call": call,
            "call_uid": uid,
            "transport": "cli_subprocess",
            "instrumented": True,
            "model": model,
            "allowed_tools": allowed_tools,
            "attempt": attempt,
            "ok": ok,
            "elapsed_s": elapsed_s,
            "response_chars": response_chars,
            "error": error,
            **(meta or {}),
        }, run_id or "unknown")
    except Exception as e:  # pragma: no cover - telemetry is never load-bearing
        print(f"  (brain-call telemetry skipped: {e})")


def record_brain_call(call: str, run_id: str, **fields) -> None:
    """Record a model invocation that did NOT go through the claude CLI subprocess.

    The public seam for any transport run_claude cannot see - today that is the
    scheduled cloud routine, which reasons in its own session and hands
    orchestrator a response FILE via --act-on (see journal._reconcile_brain_call
    for the measurement that found it). journal.log_run_summary already reconciles
    that case automatically, so this exists for a caller that knows more than the
    reconciler can infer (a real elapsed time, a slot label, a second model call
    inside one run) and wants to say so at the moment it happens.

    Stamps instrumented=false by default: never claim measurement you do not have.
    Never raises - same contract as every other line of telemetry here."""
    try:
        import journal
        journal.log_brain_call({
            "call": call,
            "transport": "external",
            "instrumented": False,
            "attempt": 1,
            "ok": True,
            **fields,
        }, run_id or "unknown")
    except Exception as e:  # pragma: no cover - telemetry is never load-bearing
        print(f"  (external brain-call telemetry skipped: {e})")


# ---------------------------------------------------------------------------
# Adversarial risk desk: an independent skeptic tries to kill every BUY.
# ---------------------------------------------------------------------------
# Risk-desk-vetoed proposals from the LAST adversarial_review call, as
# ValidationResult-shaped records. The orchestrator folds these into the
# published proposals list so the site can show WHY a trade the brain's
# commentary announced never happened (ABT 7/17: commentary said "I am
# buying Abbott" while the veto left the site with an empty proposals list).
LAST_RISK_DESK_VETOES: list = []

# The largest confidence haircut the desk may apply, matching the figure stated
# in its own prompt. A desk that could cut arbitrarily deep would be a veto by
# another name, routed around the veto's journaling and dashboard surfacing.
MAX_RISK_DESK_HAIRCUT = -0.10


REPAIRABLE_FIELDS = ("thesis", "catalysts", "variant_perception", "risk_map",
                     "macro_context", "confidence")


def _repair_proposals(queue: list[dict], reviews: dict, context_file: str,
                      run_id: str) -> dict[str, dict]:
    """Give the proposer one chance to correct a citation defect the desk named.

    Returns {TICKER: {field: corrected_value}} limited to REPAIRABLE_FIELDS —
    the narrative fields where a false claim lives. Prices, sizing, stops,
    targets, horizon and demand_driver are deliberately NOT repairable here: a
    proposal that needs its GEOMETRY changed to survive is a different trade and
    belongs in a fresh proposal, not a patch. Runs on the brain model, because
    the proposer owns the thesis and the desk must not repair its own veto.
    """
    asks = []
    for p in queue:
        t = str(p.get("ticker", "")).upper()
        r = reviews.get(t) or {}
        asks.append({
            "ticker": t,
            "repair_instruction": str(r.get("repair_instruction") or "")[:600],
            "objection": str(r.get("objection") or "")[:1200],
            "proposal": {k: p.get(k) for k in REPAIRABLE_FIELDS if k in p},
        })
    prompt = (
        "You proposed these BUYs and the risk desk vetoed each one for a SPECIFIC "
        "FACTUAL OR CITATION DEFECT that it judged correctable without changing the "
        f"trade. Full context bundle: {context_file} — read it.\n\n"
        f"{json.dumps(asks, indent=1)}\n\n"
        "For each ticker, return the corrected narrative fields. RULES:\n"
        "1) DELETE or CORRECT the specific claim the desk named. If you cannot "
        "source a replacement fact in the bundle or verify one with WebSearch, "
        "DELETE the claim — do not substitute a different unsourced number.\n"
        "2) Do NOT invent a new catalyst to replace the removed one. If removing "
        "the claim leaves the thesis without a dated resolution event, say so by "
        "returning \"withdraw\": true for that ticker. Withdrawing is the honest "
        "outcome and costs you nothing; a laundered thesis corrupts the record.\n"
        "3) You may LOWER confidence. You may not raise it.\n"
        "4) Do NOT touch price, stop, target, size, horizon or demand_driver — "
        "those are not repairable here and any change to them is ignored.\n"
        "5) Every number you keep must appear in the bundle or carry an explicit "
        "WebSearch citation in the text.\n\n"
        "Output ONLY a ```json block: {\"repairs\": [{\"ticker\": \"X\", "
        "\"withdraw\": false, \"thesis\": \"...\", \"catalysts\": [...], "
        "\"variant_perception\": \"...\", \"risk_map\": \"...\", "
        "\"confidence\": 0.62}]} — include only the fields you actually changed."
    )
    out = run_claude(prompt, model=llm_settings().get("brain_model"),
                     call="risk_desk_repair", run_id=run_id)
    parsed = {}
    for block in reversed(re.findall(r"```json\s*(.*?)```", out, re.DOTALL)):
        try:
            data = json.loads(block)
            if "repairs" in data:
                parsed = data
                break
        except (json.JSONDecodeError, TypeError):
            continue
    if not parsed:
        return {}
    original = {str(p.get("ticker", "")).upper(): p for p in queue}
    fixed: dict[str, dict] = {}
    for row in parsed.get("repairs") or []:
        if not isinstance(row, dict):
            continue
        t = str(row.get("ticker") or "").upper()
        if t not in original:
            continue
        if row.get("withdraw") is True:
            print(f"  repair: {t} WITHDRAWN by the proposer — thesis does not "
                  f"stand without the disputed claim")
            continue
        patch = {k: row[k] for k in REPAIRABLE_FIELDS
                 if k in row and row[k] not in (None, "", [])}
        if "confidence" in patch:
            try:
                # Confidence may only fall in a repair.
                if float(patch["confidence"]) > float(
                        original[t].get("confidence") or 0):
                    patch.pop("confidence")
            except (TypeError, ValueError):
                patch.pop("confidence", None)
        if patch:
            patch["repaired_from_veto"] = str(
                (reviews.get(t) or {}).get("repair_instruction") or "")[:300]
            fixed[t] = patch
    return fixed


def _desk_phase_instructions() -> str:
    """Strict kill-the-trade desk, with one phase carve-out.

    MEASURED: 29 of 45 lifetime rejections are risk_desk_veto, closed-trade n=2,
    and the shadow book is 66 regret_miss vs 36 good_skip. 'Merely adequate = veto'
    is the right rule once calibration can bind (min_trades_for_binding). Before
    that it prevents the sample the desk itself needs. Hard fails stay vetoes.
    """
    try:
        from runlib.analytics import compute_closed_trades
        n_closed = len(compute_closed_trades() or [])
    except Exception:
        n_closed = 0
    try:
        min_bind = int((validator.load_config().get("learning_controls") or {})
                       .get("min_trades_for_binding") or 15)
    except Exception:
        min_bind = 15
    shared = (
        "You are not the proposer's colleague. Your job is to find the reason this "
        "trade fails, state it plainly, and veto when you find a HARD fail: "
        "fabricated or unsourced catalyst, vague falsifiers, stop inside noise, "
        "target <10% or flattered RR, same demand_driver stacked without a distinct "
        "catalyst, or chasing a stale-price / melt-up print. A trade that survives "
        "a genuine attempt to kill it is worth more than one that was never attacked.\n"
    )
    if n_closed < min_bind:
        return shared + (
            f"PHASE: anecdote (closed_trades={n_closed}, binds at {min_bind}). "
            "Do NOT veto a setup that clears the hard-fail checklist merely because "
            "it is not the best idea of the quarter. For a merely-adequate case, "
            "APPROVE with a haircut (confidence_adjustment up to -0.10). This book "
            "cannot calibrate, unlock conviction, or grade the shadow ledger until "
            "it completes cycles. Fabrication is still a veto — never talk a bad "
            "citation through.\n"
        )
    return shared + "If the case is merely adequate, that is a veto.\n"


def adversarial_review(proposals: list[dict], context_file: str, run_id: str) -> list[dict]:
    """Second pass by a separate Grok session prompted to REFUTE each BUY.
    Veto drops the proposal (journaled); survivors may get a confidence haircut.
    Skipped where the grok CLI is unavailable - noted loudly."""
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

    if shutil.which("grok") is None:
        return _no_desk("risk desk unavailable in this environment")

    # PRECOMPUTED SOURCING CHECK (2026-07-25). The desk has been doing this grep
    # by hand and getting it right — the 07-22 DDOG veto reads "this string, and
    # any variant of 'JMP', appears ZERO times anywhere in the full context
    # bundle" — but by hand means it depends on the desk thinking to look, on a
    # 1MB bundle, under a token budget. validator.unsourced_claims does the same
    # search deterministically. It is handed over as EVIDENCE, not a verdict:
    # CLAUDE.md step 5 requires WebSearch before every BUY, so an entity missing
    # from the bundle may be a genuine WebSearch finding rather than a
    # fabrication, and only the desk can read the thesis and tell which.
    # Fail-soft throughout: the desk runs unchanged if any of this breaks.
    unsourced_block = ""
    try:
        _mc = {"_bundle": json.loads(Path(context_file).read_text())}
        found = {}
        for _p in buys:
            claims = validator.unsourced_claims(_p, _mc)
            if claims:
                found[_p.get("ticker")] = claims
        if found:
            unsourced_block = (
                "\nPRECOMPUTED SOURCING CHECK — these entities are cited by the "
                f"proposal but appear NOWHERE in the context bundle: {json.dumps(found)}. "
                "Treat each as UNVERIFIED, not automatically false: WebSearch is a "
                "required step before a BUY, so a real finding can legitimately be "
                "absent from the bundle. For every one, decide which it is. If the "
                "thesis leans on an entity that is neither in the bundle nor backed "
                "by a citation the proposer gives, that is a fabricated catalyst — "
                "veto it and say so explicitly.\n")
            print(f"  (risk desk: unsourced entities flagged {found})")
    except Exception as e:
        print(f"  (unsourced-claim precheck skipped: {str(e)[:100]})")

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
        f"{unsourced_block}"
        f"{_desk_phase_instructions()}"
        # PROBE LANE kept below regardless of phase.
        # PROBE LANE (2026-08-05): stated confidence under 0.60 no longer dies at
        # the validator — it executes as a half-risk calibration probe (max one
        # open). Without this carve-out the desk's "merely adequate = veto" rule
        # would kill every probe before the lane could gather a single fill.
        "EXCEPTION — calibration probes: a BUY whose stated confidence is below "
        "0.60 will execute as a HALF-RISK probe position (validator-enforced), "
        "not a full entry. Apply the same kill checklist — fabricated or "
        "unsourced claims, vague falsifiers, bad geometry, theme stacking are "
        "vetoes at any confidence — but do NOT veto a probe merely because the "
        "case is modest; modest conviction honestly stated is what the probe "
        "lane exists to measure.\n"
        # REPAIRABILITY (2026-08-03). 12 of 17 vetoes in the first month were
        # killed by ONE bad citation, not by a bad trade: BKR 07-28 said "Stifel
        # raised its PT to $75" when the bundle carried "TD Cowen ... $78" three
        # lines away — and BKR was re-proposed and filled later the same day, and
        # is up. DDOG (invented "JMP $311"), MELI (Loggi), IBKR (invented beat
        # streak) and UNP (inverted MOU date) are the same shape. The desk was
        # right every time; the loop around it was wrong, because a veto was
        # terminal and the run ended flat. Classifying the DEFECT lets the
        # proposer correct a citation without lowering the bar: the trade still
        # has to survive a fresh review afterwards, and the repaired thesis is
        # judged on what remains once the false claim is gone.
        "REPAIRABILITY: after deciding the verdict, classify the defect. Set "
        "\"repairable\": true ONLY when the objection is a specific factual or "
        "citation defect — a misattributed source, an unsourced figure, a stale "
        "number, a wrong date — that could be fixed by CORRECTING OR DELETING the "
        "claim while leaving the trade's geometry and core thesis standing. Set it "
        "false when the thesis DEPENDS on the false claim, or when the objection is "
        "about geometry, sizing, theme overlap, a straw-man variant perception, or "
        "a setup that is merely adequate. When true, give \"repair_instruction\": "
        "one sentence naming exactly which claim must go or be corrected, and to "
        "what. A repaired proposal is reviewed again from scratch — repairable does "
        "NOT mean approved.\n"
        "Output ONLY a ```json block: {\"reviews\": [{\"ticker\": \"X\", "
        "\"verdict\": \"approve\"|\"veto\", \"objection\": \"one paragraph covering the "
        "checklist\", \"confidence_adjustment\": 0.0, \"repairable\": false, "
        "\"repair_instruction\": \"\"}]} where confidence_adjustment is "
        "0 or negative (max -0.10) for approved-with-reservations."
    )
    def _desk(desk_prompt: str, call: str = "risk_desk") -> dict | None:
        """Run the desk and parse its reviews. None = unusable output.

        `call` labels the telemetry only - the prompt, model and parsing are
        identical either way. It exists because the repair round added on
        2026-08-03 can make this the THIRD model invocation of a single run
        (desk -> risk_desk_repair -> desk again), and a re-review that shares the
        `risk_desk` label is invisible in the rollup: the repair loop's true cost
        would read as one desk call that happened to be expensive. A repairable
        veto now shows up as what it is - roughly triple the desk spend on that
        run - so the loop can be judged on what it recovers versus what it burns.
        """
        try:
            out = run_claude(desk_prompt,
                             model=llm_settings().get("risk_desk_model"),
                             call=call, run_id=run_id)
        except Exception as exc:
            print(f"  (risk desk call failed: {str(exc)[:120]})")
            return None
        for block in reversed(re.findall(r"```json\s*(.*?)```", out, re.DOTALL)):
            try:
                data = json.loads(block)
                if "reviews" in data:
                    return {r["ticker"].upper(): r for r in data["reviews"]}
            except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
                continue
        return None

    reviews = _desk(prompt)
    if reviews is None:
        return _no_desk("risk desk failed or output unparsable")

    # One repair round. A repairable veto is a citation defect, not a bad trade —
    # the proposer gets exactly one chance to correct or delete the offending
    # claim, and the corrected proposal is then reviewed AGAIN from scratch by the
    # same adversarial desk. The bar does not move: a repaired thesis that no
    # longer stands without its false claim gets vetoed on the re-review, which is
    # the correct outcome. Capped at one round so a proposal cannot be laundered
    # through repeated rewrites, and fail-soft throughout — any failure in the
    # repair path leaves the original veto standing.
    repair_queue = [
        p for p in proposals
        if str(p.get("action", "")).upper() == "BUY"
        and (reviews.get(str(p.get("ticker", "")).upper()) or {}).get("verdict") == "veto"
        and (reviews.get(str(p.get("ticker", "")).upper()) or {}).get("repairable") is True
    ]
    if repair_queue:
        try:
            repaired = _repair_proposals(repair_queue, reviews, context_file, run_id)
        except Exception as exc:
            print(f"  (repair round skipped: {str(exc)[:120]})")
            repaired = {}
        if repaired:
            by_ticker = {str(p.get("ticker", "")).upper(): p for p in proposals}
            for tkr, fixed in repaired.items():
                if tkr in by_ticker:
                    by_ticker[tkr].update(fixed)
            re_prompt = prompt.replace(
                json.dumps(buys),
                json.dumps([by_ticker.get(str(p.get("ticker", "")).upper(), p)
                            for p in buys]))
            re_prompt += (
                "\n\nRE-REVIEW: these proposals were vetoed once for a factual or "
                "citation defect and have been corrected. Review them AGAIN from "
                "scratch against the full checklist. Do not approve because a "
                "correction was made — judge the thesis that remains now the "
                "disputed claim is gone. If it no longer stands on its own, veto "
                "it, and this time it is final.")
            second = _desk(re_prompt, call="risk_desk_rereview")
            if second:
                print(f"  risk desk re-reviewed {sorted(repaired)} after repair")
                for tkr, r in second.items():
                    r["repairable"] = False  # one round only
                    reviews[tkr] = r

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
            # The prompt states "max -0.10", but this only ever clamped the UPPER
            # bound at 0 — the floor was never enforced, so a desk returning -0.5
            # had it applied in full and could silently drive a proposal under the
            # 0.60 confidence floor. Clamp both ends to the documented range.
            adj = max(min(float(r.get("confidence_adjustment", 0) or 0), 0.0),
                      MAX_RISK_DESK_HAIRCUT)
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
# Documented OPTIONAL proposal-level keys the deterministic layer reads off a
# proposal without requiring them (grep evidence, 2026-08-05):
#   calibration_exception  tools/calibration_gate.py — losing-bucket override text
#   conviction_case        validator._risk_budget_pct / calibration gate — tier case
#   earnings_case          validator._check_earnings_window — trade-the-print case
#   exit_reason            brain_io.execute — discretionary-exit autopsy reason
#   instrument             validator._check_long_only — defaults "EQUITY" if absent
#   sell_fraction          validator._check_sell_fraction / execute — partial exits
#
# CONTRACT (2026-08-05): any field the validator layer reads off a proposal
# MUST be listed either in autonomy_config's
# trade_quality_requirements.required_proposal_fields / required_sell_fields
# (when required) or in OPTIONAL_PROPOSAL_FIELDS here (when optional).
# parse_proposals derives its pass-through whitelist from that UNION, so a
# schema field registered in either place can never again silently vanish
# between the brain's JSON block and the validator — the purely hand-maintained
# whitelist is what dropped seat_reviews/factor_response until 2d0e46e and
# caused the 07-30 process_gate_blocked incident.
OPTIONAL_PROPOSAL_FIELDS = (
    "calibration_exception",
    "conviction_case",
    "earnings_case",
    "exit_reason",
    "instrument",
    "sell_fraction",
)


def _allowed_output_keys() -> set:
    """Schema-derived whitelist: config's required proposal/sell fields UNION
    OPTIONAL_PROPOSAL_FIELDS (contract above). Config is read via
    validator.load_config() at call time — the same fail-soft pattern
    llm_settings uses — so a config problem costs only the derived keys, never
    the parse (the documented optional tuple still passes through)."""
    try:
        q = validator.load_config().get("trade_quality_requirements") or {}
        required = (list(q.get("required_proposal_fields") or [])
                    + list(q.get("required_sell_fields") or []))
    except Exception:
        required = []
    return set(required) | set(OPTIONAL_PROPOSAL_FIELDS)


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
                out = {
                    "proposals": _dicts(data["proposals"]),
                    "no_trade_reason": data.get("no_trade_reason"),
                    "commentary": data.get("commentary"),
                    "watchlist": _dicts(data.get("watchlist"), 10),
                    "rejected_ideas": _dicts(data.get("rejected_ideas")),
                    "x_post": data.get("x_post"),
                    "guidance_entries": _dicts(data.get("guidance_entries"), 20),
                    "universe_candidates": _dicts(data.get("universe_candidates"), 5),
                    "seat_reviews": data.get("seat_reviews"),
                    "factor_response": data.get("factor_response"),
                    # This dict is still a whitelist, but no longer the ONLY
                    # one (2026-08-05): it hand-carries the documented
                    # top-level output keys because each needs its own shape
                    # guard or cap. Every OTHER key the validator layer reads
                    # is derived from the schema by _allowed_output_keys()
                    # (config required lists + OPTIONAL_PROPOSAL_FIELDS — see
                    # the contract above) and passed through below, so the
                    # silent-drop class that took out seat_reviews/
                    # factor_response until 2d0e46e (07-30: CRWD and ITW died
                    # as process_gate_blocked) cannot recur for a registered
                    # field. A new top-level key with its own shape guard
                    # still belongs HERE; a validator-read field belongs in
                    # config's required lists or OPTIONAL_PROPOSAL_FIELDS.
                    "trigger_reviews": data.get("trigger_reviews"),
                    "waiting_for": data.get("waiting_for"),
                }
                # Schema-derived pass-through: keys the validator layer knows
                # about survive parsing verbatim when the brain wrote them.
                # Sorted so emitted key order is deterministic run to run.
                for k in sorted(_allowed_output_keys() - set(out)):
                    if k in data:
                        out[k] = data[k]
                return out
        except json.JSONDecodeError:
            continue
    # Graceful public-facing fallback (this text can surface on the dashboard).
    return {"proposals": [],
            "no_trade_reason": "No trade this run; the agent's written review did not include a "
                               "machine-readable order block, so no orders were placed.",
            "commentary": None, "watchlist": [], "rejected_ideas": [],
            "x_post": None, "guidance_entries": [], "universe_candidates": [],
            "seat_reviews": None, "factor_response": None,
            "trigger_reviews": None, "waiting_for": None}


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
            # calibration_probe rides along so the position itself records it —
            # validator._check_probe_limits counts open probes from plans, and
            # the exit-grade sweep buckets the fill under 0.50-0.60.
            plan = {k: p.get(k) for k in (
                "stop_loss", "target_price", "holding_horizon_days",
                "entry_price_max", "confidence", "demand_driver",
                "thesis_invalidators", "calibration_probe")}
        elif p["action"] == "SELL_TO_CLOSE" and isinstance(pos_before, dict):
            # CARRY THE ENTRY PLAN ONTO A DISCRETIONARY EXIT (2026-08-03). This
            # branch did not exist, so every discretionary sell placed with
            # "plan": None — which is literally why the HPE close of 2026-07-15
            # sits in the ledger with stop_loss None and forced_exit_reason None
            # and cannot be graded at all. A forced exit records the plan it was
            # judged against; a discretionary one recorded nothing, so the two
            # closed trades in this book's history are not comparable. The exit
            # is graded against the plan it is EXITING, so that is the plan to
            # persist.
            plan = pos_before.get("plan") or pos_before.get("original_plan")
        order = broker.place_order({
            "ticker": p["ticker"], "action": p["action"],
            "position_size_usd": p.get("position_size_usd"),
            "reference_price": ref, "proposal_id": run_id,
            "sell_fraction": p.get("sell_fraction"),  # partial exits (validated)
            # ATR lets the broker vol-scale slippage and model an entry gap (a $400 name
            # swinging 7%/day costs more to fill than a flat 10bps implies).
            "atr_pct": atr_map.get(p["ticker"].upper()),
            # TOP-LEVEL entry ceiling. alpaca_broker's live last-trade guard reads
            # order["entry_price_max"], and this key was never set - it existed only
            # nested inside `plan` - so that guard evaluated None and silently
            # no-opped on EVERY buy since it was written. The only ceiling actually
            # enforced was against the bundle's `ref`, which the live overlay allows
            # to be up to 30 minutes old. A name gapping between the overlay snapshot
            # and submission filled above the brain's stated maximum entry.
            "entry_price_max": p.get("entry_price_max"),
            # Numeric plan (+ theme/falsifiers) persisted ONTO the position at fill.
            "plan": plan,
            "demand_driver": p.get("demand_driver"),
            # Make the ledger row self-describing rather than requiring a journal
            # join to tell a deliberate rotation from a safety-layer stop. The
            # exit-grade sweep can recover this from the fill, but a row that
            # states its own reason is the one an audit can read.
            **({"exit_reason_kind": "discretionary",
                "exit_thesis": str(p.get("thesis") or "")[:300]}
               if p["action"] == "SELL_TO_CLOSE" else {}),
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
