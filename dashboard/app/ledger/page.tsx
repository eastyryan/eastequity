import fs from "node:fs";
import path from "node:path";
import Link from "next/link";
import type { Metadata } from "next";
import EquityChart from "@/components/EquityChart";
import ClosedTrades from "@/components/ClosedTrades";
import Calibration from "@/components/Calibration";
import CalibrationDiagram from "@/components/CalibrationDiagram";
import PositionChart from "@/components/PositionChart";
import CostsChart from "@/components/CostsChart";
import SectorExposure from "@/components/SectorExposure";
import UniverseLog from "@/components/UniverseLog";
import WatchlistOutcomes from "@/components/WatchlistOutcomes";
import LiveQuotes from "@/components/LiveQuotes";
import type {
  HistoryPoint,
  Latest,
  PositionChartData,
  UniverseLogEntry,
  WatchlistOutcome,
} from "@/lib/types";

// The instrument panel behind the front page: every chart, table, and cost line
// the Arena deliberately leaves out. The Arena is the story; this is the record.

export const metadata: Metadata = {
  title: "The Ledger — East Equity Agent",
  description:
    "Full instrument panel: equity curve, performance record, calibration, positions, sector exposure, closed trades, costs, and universe history.",
};

const STARTING_CAPITAL = 10000;

function readJson<T>(file: string): T {
  return JSON.parse(fs.readFileSync(path.join(process.cwd(), "data", file), "utf-8")) as T;
}

// Optional data files (may not exist until a run emits them) — never crash the build.
function readJsonSafe<T>(file: string, fallback: T): T {
  try {
    return JSON.parse(fs.readFileSync(path.join(process.cwd(), "data", file), "utf-8")) as T;
  } catch {
    return fallback;
  }
}

function clean(s: string) {
  return s
    .replace(/—/g, "-")
    .replace(/–/g, "-")
    .replace(/\*\*/g, "")
    .replace(/[`*]/g, "");
}

function usd(n: number) {
  return n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
}

// Cents-precise, for the small figures (fees, per-trade dividends) where rounding to
// whole dollars would hide or distort the number.
function usd2(n: number) {
  return n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 });
}

type Freshness = { label: string; tone: "live" | "stale" | "degraded"; detail?: string };

// Derive a small data-health badge from the (optional) data_quality / stale_data_notice fields.
// Returns null when we have no freshness signal at all, so healthy-but-silent runs stay clean.
function freshness(l: Latest): Freshness | null {
  // data_quality is the structured verdict and takes precedence; the prose
  // stale_data_notice exists for the BRAIN and is set even when the relay bundle
  // is minutes old (cloud runs always use it), so notice-first wrongly branded
  // every routine cloud run DEGRADED over fresh data.
  const dq = l.data_quality;
  if (dq) {
    if (dq.source === "degraded_empty") {
      return { label: "DEGRADED", tone: "degraded", detail: l.stale_data_notice ?? dq.note };
    }
    if (dq.stale) {
      const age = dq.age_hours != null ? ` ${Math.round(dq.age_hours)}h` : "";
      return { label: `STALE${age}`, tone: "stale", detail: l.stale_data_notice ?? dq.note };
    }
    if (dq.source === "live_partial") {
      return { label: "PARTIAL", tone: "stale", detail: dq.note };
    }
    const age = dq.age_hours != null && dq.age_hours >= 1 ? ` ${Math.round(dq.age_hours)}h` : "";
    return { label: `LIVE DATA${age}`, tone: "live", detail: dq.note ?? dq.source };
  }
  if (l.stale_data_notice) {
    return { label: "DEGRADED", tone: "degraded", detail: l.stale_data_notice };
  }
  return null;
}

export default function Ledger() {
  const latest = readJson<Latest>("latest.json");
  const history = readJson<HistoryPoint[]>("equity_history.json");
  const positionCharts = readJsonSafe<Record<string, PositionChartData>>("position_charts.json", {});
  const universeLog = readJsonSafe<UniverseLogEntry[]>("universe_log.json", []);
  const watchlistOutcomes =
    latest.watchlist_outcomes ?? readJsonSafe<WatchlistOutcome[]>("watchlist_outcomes.json", []);

  const equity = latest.portfolio.total_equity_usd;
  const cash = latest.portfolio.cash_usd;
  const positions = latest.portfolio.positions ?? [];

  const lastRunDate = new Date(latest.generated_at);
  const lastRun = `${lastRunDate.toLocaleDateString("en-US", {
    month: "long",
    day: "numeric",
    timeZone: "America/New_York",
  })}, ${lastRunDate.toLocaleTimeString("en-US", {
    hour: "numeric",
    minute: "2-digit",
    timeZone: "America/New_York",
  })} ET`;

  const perf = latest.performance;
  const closedTrades = latest.closed_trades ?? [];
  const rejected = latest.proposals.filter((p) => !p.approved);
  const health = latest.health ?? null;
  const healthDegraded = !!health && (health.status ?? "ok") !== "ok";
  const riskHalts = latest.risk_halts ?? [];
  const forcedExits = latest.forced_exits ?? [];

  const fresh = freshness(latest);
  const freshClass =
    fresh?.tone === "degraded"
      ? "border-neg/30 bg-neg-soft text-neg"
      : fresh?.tone === "stale"
        ? "border-attn/40 bg-attn-soft text-attn"
        : "border-pos/30 bg-pos-soft text-pos-strong";

  const perfTiles: { label: string; value: string; accent?: boolean; sub?: string }[] = perf
    ? [
        { label: "Closed trades", value: String(perf.closed_trades) },
        { label: "Win rate", value: `${perf.win_rate_pct}%` },
        {
          label: "Realized P&L",
          value: `${perf.realized_pnl_usd >= 0 ? "+" : ""}${usd(perf.realized_pnl_usd)}`,
          accent: perf.realized_pnl_usd > 0,
          sub:
            perf.realized_pnl_incl_dividends_usd != null &&
            perf.realized_pnl_incl_dividends_usd !== perf.realized_pnl_usd
              ? `${perf.realized_pnl_incl_dividends_usd >= 0 ? "+" : ""}${usd2(
                  perf.realized_pnl_incl_dividends_usd,
                )} incl. dividends`
              : undefined,
        },
        { label: "Avg R multiple", value: perf.avg_r_multiple === null ? "n/a" : `${perf.avg_r_multiple}R` },
        { label: "Avg days held", value: String(perf.avg_days_held) },
        { label: "Max drawdown", value: `${perf.max_drawdown_pct}%` },
        {
          label: "Dividends",
          value:
            latest.total_dividends_usd == null
              ? "n/a"
              : latest.total_dividends_usd
                ? `+${usd2(latest.total_dividends_usd)}`
                : "$0.00",
          accent: (latest.total_dividends_usd ?? 0) > 0,
        },
        {
          label: "Fees paid",
          value:
            perf.total_fees_paid_usd == null
              ? "n/a"
              : perf.total_fees_paid_usd
                ? `-${usd2(perf.total_fees_paid_usd)}`
                : "$0.00",
        },
      ]
    : [];

  return (
    <main className="mx-auto max-w-5xl px-5 pb-10 sm:px-8">
      {/* Header */}
      <header className="flex h-[68px] items-center justify-between border-b border-line">
        <span className="flex items-center gap-2.5">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src="/avatar.jpg" alt="" className="h-9 w-9 rounded-full border border-line" />
          <span className="flex flex-col leading-none">
            <span className="text-[15px] font-semibold tracking-tight text-ink">The Ledger</span>
            <span className="ds-label mt-1">East Equity Agent</span>
          </span>
        </span>
        <span className="flex items-center gap-2">
          {fresh && (
            <span
              title={fresh.detail}
              className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[11px] font-medium font-[family-name:var(--font-mono)] ${freshClass}`}
            >
              <span className="inline-block h-1.5 w-1.5 rounded-full bg-current" aria-hidden />
              {fresh.label}
            </span>
          )}
          <Link
            href="/"
            className="text-[13px] text-ink-2 underline decoration-line underline-offset-4 hover:text-ink"
          >
            ← Front page
          </Link>
        </span>
      </header>

      <section className="pt-10 pb-2">
        <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">The Ledger</h1>
        <p className="mt-3 max-w-2xl text-ink-2 leading-relaxed">
          The full instrument panel behind the front page: the equity curve with every trade marked, the
          performance record, calibration, live quotes, each position against its own plan, sector
          concentration, closed results, the honest cost drag, and the universe&apos;s history.
        </p>
        <p className="mt-6 text-[13px] text-ink-3">
          Last run {lastRun}. {latest.schedule_note ?? "Runs on weekdays"}.
        </p>

        {/* Pipeline heartbeat: scheduled runs completed vs expected + data-bundle age.
            Built precisely so a silently-dead pipeline is visible here, not just in logs. */}
        {health && (
          <p
            className={`mt-2 text-[13px] font-[family-name:var(--font-mono)] ${
              healthDegraded ? "text-neg" : "text-ink-3"
            }`}
          >
            Pipeline: {health.completed_scheduled_runs ?? 0}/{health.expected_runs_so_far ?? 0} scheduled runs
            today
            {health.bundle_age_hours != null && <> · data bundle {health.bundle_age_hours}h old</>}
            {healthDegraded && <> · {health.status}</>}
          </p>
        )}

        {/* Risk halts: new BUYs are blocked until equity recovers. */}
        {riskHalts.length > 0 && (
          <div className="mt-6 rounded-lg border border-neg/30 bg-neg-soft px-4 py-3">
            <p className="text-sm font-medium text-neg">
              Risk halt active — new buys are blocked until equity recovers
            </p>
            <ul className="mt-1 list-disc pl-5 text-[13px] text-neg">
              {riskHalts.map((h, i) => (
                <li key={i} className="font-[family-name:var(--font-mono)]">
                  {clean(h)}
                </li>
              ))}
            </ul>
          </div>
        )}
      </section>

      {/* Equity curve — the chart card carries its own header and timeframe controls */}
      <section className="mt-5">
        <EquityChart points={history} startingCapital={STARTING_CAPITAL} events={latest.trade_events} />
        <p className="mt-3 text-[13px] text-ink-3">
          {usd(STARTING_CAPITAL)} starting capital, marked each session against buying and holding the S&amp;P
          500.
        </p>
      </section>

      {/* Performance record, appears once trades have closed */}
      {perf && (
        <section className="ds-card mt-5 p-5 sm:p-7">
          <h2 className="text-lg font-semibold tracking-tight">Performance record</h2>
          <dl className="mt-6 grid grid-cols-2 gap-y-8 sm:grid-cols-4">
            {perfTiles.map((s) => (
              <div key={s.label} className="border-l border-line pl-4">
                <dt className="text-[13px] text-ink-3">{s.label}</dt>
                <dd
                  className={`mt-1 text-xl font-medium tracking-tight font-[family-name:var(--font-mono)] ${
                    s.accent ? "text-accent" : ""
                  }`}
                >
                  {s.value}
                </dd>
                {s.sub && (
                  <dd className="mt-0.5 text-[11px] text-ink-3 font-[family-name:var(--font-mono)]">{s.sub}</dd>
                )}
              </div>
            ))}
          </dl>
        </section>
      )}

      {/* Confidence calibration, honesty feature: stated confidence vs realized win rate */}
      {latest.calibration && <Calibration data={latest.calibration} />}
      {latest.calibration && Object.keys(latest.calibration.by_confidence ?? {}).length > 0 && (
        <section className="ds-card mt-5 p-5 sm:p-7">
          <h2 className="text-lg font-semibold tracking-tight">Calibration curve</h2>
          <p className="mt-1 text-sm text-ink-2">
            Each confidence bucket plotted against perfect calibration - below the line is overconfidence.
          </p>
          <div className="mt-6 mx-auto max-w-sm">
            <CalibrationDiagram calibration={latest.calibration} />
          </div>
        </section>
      )}

      {/* Near-live quotes for held + watched names (hydrates client-side) */}
      <LiveQuotes
        tickers={[
          ...positions.map((p) => p.ticker),
          ...(latest.watchlist ?? []).map((w) => w.ticker),
        ].filter((t, i, a) => t && a.indexOf(t) === i)}
        avgCost={Object.fromEntries(positions.map((p) => [p.ticker, p.avg_cost]))}
      />

      {/* Positions */}
      <section className="ds-card mt-5 p-5 sm:p-7">
        <h2 className="text-lg font-semibold tracking-tight">Open positions</h2>
        {positions.length === 0 ? (
          <div className="mt-6 rounded-lg border border-dashed border-line px-6 py-10 text-center">
            <p className="font-medium">Holding 100% cash</p>
            <p className="mx-auto mt-1.5 max-w-md text-sm text-ink-2 leading-relaxed">
              No current setup met the agent&apos;s bar for conviction and risk-reward. Passing is a position
              too. The reasoning is on the front page.
            </p>
          </div>
        ) : (
          <ul className="mt-6 divide-y divide-line/60">
            {positions.map((p) => {
              const pnl = p.market_value_usd - p.quantity * p.avg_cost;
              const pnlPct = (pnl / (p.quantity * p.avg_cost)) * 100;
              const plan = p.original_plan;
              const risk = latest.position_risk?.[p.ticker];
              const horizon = plan?.holding_horizon_days;
              const horizonPct = horizon ? ((p.days_held ?? 0) / horizon) * 100 : null;
              return (
                <li key={p.ticker}>
                  <details className="group py-4">
                    <summary className="flex cursor-pointer list-none items-center gap-3.5 rounded-lg px-1 transition-colors hover:bg-sunken/60 [&::-webkit-details-marker]:hidden">
                      <span className="num flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-line bg-sunken text-[12px] font-semibold text-ink">
                        {p.ticker}
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="num block text-[14px] font-semibold text-ink">{p.ticker}</span>
                        <span className="num mt-0.5 block text-[12px] text-faint">
                          {p.quantity} sh @ ${p.avg_cost.toFixed(2)}
                        </span>
                      </span>
                      <span className="flex flex-col items-end gap-0.5 text-right">
                        <span className="num text-[14px] font-semibold text-ink">{usd(p.market_value_usd)}</span>
                        <span
                          className={`num text-[12.5px] font-semibold ${pnl >= 0 ? "text-pos" : "text-neg"}`}
                        >
                          <span className="text-[0.8em]">{pnl >= 0 ? "▲" : "▼"}</span> {pnl >= 0 ? "+" : ""}
                          {usd(pnl)} ({pnl >= 0 ? "+" : ""}
                          {pnlPct.toFixed(1)}%)
                        </span>
                      </span>
                      <span className="shrink-0 text-ink-3 transition-transform group-open:rotate-90" aria-hidden>
                        ›
                      </span>
                    </summary>

                    <div className="mt-4 space-y-4 pl-0 sm:pl-[4.5rem]">
                      {plan && (
                        <div className="grid grid-cols-2 gap-y-4 sm:grid-cols-5 text-sm">
                          {[
                            { label: "Stop loss", value: plan.stop_loss ? `$${plan.stop_loss}` : "n/a" },
                            { label: "Target", value: plan.target_price ? `$${plan.target_price}` : "n/a" },
                            {
                              label: "Horizon",
                              value: plan.holding_horizon_days
                                ? `${p.days_held ?? 0}d of ${plan.holding_horizon_days}d`
                                : "n/a",
                            },
                            {
                              label: "Confidence",
                              value: plan.confidence != null ? `${Math.round(plan.confidence * 100)}%` : "n/a",
                            },
                            {
                              label: "Reward / risk",
                              value: plan.risk_reward_ratio != null ? `${plan.risk_reward_ratio}` : "n/a",
                            },
                          ].map((s) => (
                            <div key={s.label} className="border-l border-line pl-3">
                              <div className="text-[12px] text-ink-3">{s.label}</div>
                              <div className="mt-0.5 font-[family-name:var(--font-mono)]">{s.value}</div>
                            </div>
                          ))}
                        </div>
                      )}

                      {/* Live risk: distance-to-stop in ATR terms and how far into the horizon we are */}
                      {(risk?.cushion_in_atr != null || horizonPct !== null) && (
                        <div className="flex flex-wrap items-start gap-x-8 gap-y-4">
                          {risk?.cushion_in_atr != null && (
                            <div>
                              <div className="text-[12px] text-ink-3">Cushion to stop</div>
                              <div
                                className={`mt-0.5 text-sm font-[family-name:var(--font-mono)] ${
                                  risk.inside_noise_band ? "text-attn" : ""
                                }`}
                              >
                                {risk.cushion_in_atr.toFixed(1)}× ATR
                                {risk.inside_noise_band && (
                                  <span className="ml-1.5 text-[11px]">inside noise band</span>
                                )}
                              </div>
                            </div>
                          )}
                          {horizonPct !== null && (
                            <div className="min-w-[150px] max-w-[240px] flex-1">
                              <div className="flex items-baseline justify-between text-[12px] text-ink-3">
                                <span>Horizon</span>
                                <span className="font-[family-name:var(--font-mono)]">
                                  {p.days_held ?? 0}d / {horizon}d
                                </span>
                              </div>
                              <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-line">
                                <div
                                  className={`h-full rounded-full ${horizonPct >= 100 ? "bg-neg" : "bg-accent"}`}
                                  style={{ width: `${Math.min(100, Math.max(2, horizonPct))}%` }}
                                />
                              </div>
                            </div>
                          )}
                        </div>
                      )}

                      {positionCharts[p.ticker]?.bars?.length ? (
                        <div>
                          <h3 className="text-sm font-medium">Price vs. the plan</h3>
                          <p className="mt-0.5 text-[12px] text-ink-3">
                            Dashed lines mark the target and stop; the flag marks entry.
                          </p>
                          <div className="mt-2">
                            <PositionChart ticker={p.ticker} data={positionCharts[p.ticker] ?? null} />
                          </div>
                        </div>
                      ) : null}

                      {plan?.thesis && (
                        <div>
                          <h3 className="text-sm font-medium">Thesis</h3>
                          <p className="mt-1 text-sm text-ink-2 leading-relaxed">{clean(plan.thesis)}</p>
                        </div>
                      )}

                      {plan?.catalysts && plan.catalysts.length > 0 && (
                        <div>
                          <h3 className="text-sm font-medium">Catalysts</h3>
                          <ul className="mt-1 list-disc space-y-1 pl-5 text-sm text-ink-2 leading-relaxed">
                            {plan.catalysts.map((c, i) => (
                              <li key={i}>{clean(c)}</li>
                            ))}
                          </ul>
                        </div>
                      )}

                      {plan?.risk_map && (
                        <div>
                          <h3 className="text-sm font-medium">What kills this trade</h3>
                          <p className="mt-1 text-sm text-ink-2 leading-relaxed">{clean(plan.risk_map)}</p>
                        </div>
                      )}

                      {plan?.macro_context && (
                        <div>
                          <h3 className="text-sm font-medium">Macro context at entry</h3>
                          <p className="mt-1 text-sm text-ink-2 leading-relaxed">{clean(plan.macro_context)}</p>
                        </div>
                      )}

                      <p className="text-[12px] text-ink-3">
                        Opened{" "}
                        {new Date(p.opened_at).toLocaleDateString("en-US", {
                          month: "long",
                          day: "numeric",
                          timeZone: "America/New_York",
                        })}
                        . The agent re-reviews this position against this plan on every run.
                      </p>
                    </div>
                  </details>
                </li>
              );
            })}
          </ul>
        )}
      </section>

      {/* Sector exposure / concentration */}
      {positions.length > 0 && (
        <section className="ds-card mt-5 p-5 sm:p-7">
          <h2 className="text-lg font-semibold tracking-tight">Sector exposure</h2>
          <p className="mt-1 text-sm text-ink-2">
            Where the book is concentrated, as a share of total equity.
          </p>
          <div className="mt-6">
            <SectorExposure positions={positions} cash={cash} totalEquity={equity} />
          </div>
        </section>
      )}

      {/* Latest activity */}
      <section className="ds-card mt-5 p-5 sm:p-7">
        <h2 className="text-lg font-semibold tracking-tight">Latest activity</h2>

        {latest.fills.length > 0 ? (
          <ul className="mt-4 space-y-2">
            {latest.fills.map((f, i) => (
              <li key={i} className="text-sm font-[family-name:var(--font-mono)]">
                {f.action === "BUY" ? "Opened" : "Closed"} {f.ticker}: {f.quantity} shares at ${f.fill_price}
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-2 text-sm text-ink-2">No orders on the last run.</p>
        )}

        {/* Safety-layer exits: closed by deterministic code (stop breached / horizon
            expired) BEFORE the agent ran — shown with the rule that fired, so a
            forced exit is never a mystery line in the fills. */}
        {forcedExits.length > 0 && (
          <div className="mt-6 rounded-lg border border-attn/40 bg-attn-soft px-4 py-3">
            <h3 className="text-sm font-medium text-attn">Closed by the safety layer this run</h3>
            <ul className="mt-2 space-y-1.5">
              {forcedExits.map((fe, i) => (
                <li key={i} className="text-[13px] text-attn">
                  <span className="font-[family-name:var(--font-mono)]">{fe.ticker}</span>
                  {": "}
                  {fe.reason === "stop_loss_breached"
                    ? `stop loss breached (last $${fe.last_price ?? "?"} vs stop $${fe.stop_loss ?? "?"})`
                    : fe.reason === "horizon_expired"
                      ? `holding horizon expired (${fe.days_held ?? "?"}d of ${fe.horizon ?? "?"}d)`
                      : clean(fe.reason)}
                </li>
              ))}
            </ul>
          </div>
        )}

        {rejected.length > 0 && (
          <div className="mt-6">
            <h3 className="text-sm font-medium">Rejected by the validator</h3>
            <ul className="mt-2 space-y-1.5">
              {rejected.map((p, i) => (
                <li key={i} className="text-sm text-ink-2">
                  <span className="font-[family-name:var(--font-mono)]">{String(p.proposal.ticker)}</span>:{" "}
                  {p.reasons.join(", ")}
                </li>
              ))}
            </ul>
          </div>
        )}

        <Link
          href="/runs"
          className="mt-6 inline-block text-sm text-ink-2 underline decoration-line underline-offset-4 hover:text-ink"
        >
          Browse every run&apos;s full reasoning →
        </Link>
      </section>

      {/* Closed results */}
      {closedTrades.length > 0 && (
        <section className="ds-card mt-5 p-5 sm:p-7">
          <h2 className="text-lg font-semibold tracking-tight">Closed results</h2>
          <p className="mt-1 text-sm text-ink-2">
            Every finished trade, newest first, scored against its own written plan.
          </p>
          <ClosedTrades trades={closedTrades} />
        </section>
      )}

      {/* Costs & drag over time */}
      {closedTrades.length > 0 && (
        <section className="ds-card mt-5 p-5 sm:p-7">
          <h2 className="text-lg font-semibold tracking-tight">Costs and income over time</h2>
          <p className="mt-1 text-sm text-ink-2">
            Cumulative trading fees paid versus dividends received - the honest drag on the record.
          </p>
          <div className="mt-6">
            <CostsChart closedTrades={closedTrades} />
          </div>
        </section>
      )}

      {/* Universe changes log */}
      {universeLog.length > 0 && <UniverseLog log={universeLog} />}

      {/* Watchlist outcomes: did the agent's calls play out? */}
      {watchlistOutcomes.length > 0 && <WatchlistOutcomes outcomes={watchlistOutcomes} />}

      <footer className="mt-10 border-t border-line py-8">
        <p className="text-[13px] text-ink-3 leading-relaxed">
          East Equity Agent is a research experiment in agentic trading, currently running on simulated
          capital. Nothing here is financial advice.
        </p>
        <Link
          href="/"
          className="mt-4 inline-block text-sm text-ink-2 underline decoration-line underline-offset-4 hover:text-ink"
        >
          ← Back to the front page
        </Link>
      </footer>
    </main>
  );
}
