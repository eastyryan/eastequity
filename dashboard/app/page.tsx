import fs from "node:fs";
import path from "node:path";
import Link from "next/link";
import EquityChart from "@/components/EquityChart";
import ClosedTrades from "@/components/ClosedTrades";
import Calibration from "@/components/Calibration";
import CalibrationDiagram from "@/components/CalibrationDiagram";
import PositionChart from "@/components/PositionChart";
import CostsChart from "@/components/CostsChart";
import SectorExposure from "@/components/SectorExposure";
import UniverseLog from "@/components/UniverseLog";
import WatchlistOutcomes from "@/components/WatchlistOutcomes";

type Position = {
  ticker: string;
  quantity: number;
  avg_cost: number;
  market_value_usd: number;
  last_price?: number;
  opened_at: string;
  days_held?: number;
  sector?: string | null;
  original_plan?: {
    thesis?: string;
    stop_loss?: number;
    target_price?: number;
    entry_price_max?: number;
    holding_horizon_days?: number;
    confidence?: number;
    risk_reward_ratio?: number;
    catalysts?: string[];
    risk_map?: string;
    macro_context?: string;
    proposed_at?: string;
  } | null;
};

type Indicator = { latest: number; direction: string } | null;

type CalibrationBucket = {
  trades: number;
  win_rate_pct: number;
  avg_stated_confidence_pct: number;
  calibration_gap_pct: number;
};

type Calibration = {
  note: string;
  by_confidence: Record<string, CalibrationBucket>;
  high_conf_0_70_plus: { trades: number; win_rate_pct: number | null; inflated: boolean };
};

type PositionRisk = {
  last_price: number;
  recorded_stop: number;
  cushion_to_stop_pct: number | null;
  atr_pct: number | null;
  expected_move_pct: number | null;
  cushion_in_atr?: number;
  inside_noise_band?: boolean;
  stop_distance_from_entry_pct?: number;
};

type Latest = {
  generated_at: string;
  run_id: string;
  mode: string;
  schedule_note?: string;
  portfolio: { cash_usd: number; total_equity_usd: number; positions: Position[] };
  no_trade_reason?: string | null;
  commentary?: string | null;
  macro_snapshot?: {
    cpi_yoy_pct: Indicator;
    ten_year_yield: Indicator;
    vix: Indicator;
    yield_curve_10y2y?: Indicator;
    hy_credit_spread?: Indicator;
  } | null;
  proposals: { proposal: Record<string, unknown>; approved: boolean; reasons: string[] }[];
  fills: { ticker: string; action: string; fill_price: number; quantity: number }[];
  closed_trades?: {
    ticker: string;
    entry_price: number;
    exit_price: number;
    opened_at: string;
    closed_at: string;
    days_held: number;
    pnl_usd: number | null;
    r_multiple: number | null;
    verdict: string;
    thesis?: string | null;
    dividends_usd?: number;
    total_pnl_usd?: number;
    fees_usd?: { commission?: number; sec_fee?: number; taf?: number } | null;
    gap_modeled?: boolean;
  }[];
  performance?: {
    closed_trades: number;
    win_rate_pct: number;
    realized_pnl_usd: number;
    avg_r_multiple: number | null;
    avg_days_held: number;
    max_drawdown_pct: number;
    realized_pnl_incl_dividends_usd?: number;
    total_fees_paid_usd?: number;
  } | null;
  improvements?: { date: string; note: string }[];
  watchlist?: { ticker: string; one_line: string; thoughts: string; would_buy_at?: string | null }[];
  total_dividends_usd?: number;
  calibration?: Calibration | null;
  position_risk?: Record<string, PositionRisk>;
  data_quality?: { source: string; age_hours?: number; stale?: boolean; note?: string } | null;
  stale_data_notice?: string | null;
  universe_size?: number;
  as_of_et?: string;
  trade_events?: TradeEvent[];
  watchlist_outcomes?: WatchlistOutcome[];
};

type TradeEvent = { date: string; ticker: string; action: "BUY" | "SELL_TO_CLOSE"; verdict?: string | null };

type WatchlistOutcome = {
  ticker: string;
  first_watched: string;
  price_when_added?: number | null;
  would_buy_at?: string | null;
  one_line?: string;
  currently_watched?: boolean;
  hit_buy_level?: boolean;
  hit_date?: string;
  acted?: boolean;
  latest_price?: number | null;
  move_pct_since_watched?: number | null;
  dropped_date?: string;
};

type PositionChartData = {
  bars: { date: string; open: number; high: number; low: number; close: number }[];
  avg_cost?: number | null;
  last_price?: number | null;
  entry?: number | null;
  stop?: number | null;
  target?: number | null;
  opened_at?: string | null;
};

type UniverseLogEntry = {
  date: string;
  added: string[];
  removed: string[];
  dropped_unpriceable: string[];
  size: number;
  rationale: string;
};

type HistoryPoint = { date: string; equity: number; benchmark_close?: number | null };

const STARTING_CAPITAL = 10000;

function readJson<T>(file: string): T {
  return JSON.parse(fs.readFileSync(path.join(process.cwd(), "data", file), "utf-8"));
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

function arrow(direction?: string) {
  return direction === "rising" ? "↑" : direction === "falling" ? "↓" : "";
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

export default function Home() {
  const latest = readJson<Latest>("latest.json");
  const history = readJson<HistoryPoint[]>("equity_history.json");
  const positionCharts = readJsonSafe<Record<string, PositionChartData>>("position_charts.json", {});
  const universeLog = readJsonSafe<UniverseLogEntry[]>("universe_log.json", []);
  const watchlistOutcomes =
    latest.watchlist_outcomes ?? readJsonSafe<WatchlistOutcome[]>("watchlist_outcomes.json", []);

  const equity = latest.portfolio.total_equity_usd;
  const cash = latest.portfolio.cash_usd;
  const positions = latest.portfolio.positions ?? [];
  const returnPct = ((equity - STARTING_CAPITAL) / STARTING_CAPITAL) * 100;

  // Excess return vs holding SPY with the same capital since day one (Hermes-style honesty).
  const benchPoints = history.filter((h) => h.benchmark_close);
  let excessPts: number | null = null;
  if (benchPoints.length >= 1) {
    const spyReturnPct =
      (benchPoints[benchPoints.length - 1].benchmark_close! / benchPoints[0].benchmark_close! - 1) * 100;
    excessPts = returnPct - spyReturnPct;
  }

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

  const stats = [
    { label: "Portfolio value", value: usd(equity), signed: false, positive: false },
    {
      label: "Total return",
      value: `${returnPct >= 0 ? "+" : ""}${returnPct.toFixed(2)}%`,
      signed: true,
      positive: returnPct >= 0,
    },
    {
      label: "vs S&P 500",
      value:
        excessPts === null ? "n/a" : `${excessPts >= 0 ? "+" : ""}${excessPts.toFixed(2)}%`,
      signed: excessPts !== null,
      positive: excessPts !== null && excessPts >= 0,
    },
    { label: "Cash", value: usd(cash), signed: false, positive: false },
  ];

  const perf = latest.performance;
  const thinking = latest.commentary ?? latest.no_trade_reason;
  const macro = latest.macro_snapshot;
  const closedTrades = latest.closed_trades ?? [];
  const improvements = latest.improvements ?? [];
  const rejected = latest.proposals.filter((p) => !p.approved);

  const fresh = freshness(latest);
  const freshClass =
    fresh?.tone === "degraded"
      ? "border-red-300 bg-red-50 text-red-700"
      : fresh?.tone === "stale"
        ? "border-amber-300 bg-amber-50 text-amber-800"
        : "border-emerald-300 bg-emerald-50 text-emerald-700";

  const universeLabel = latest.universe_size ? `${latest.universe_size} names` : "dozens of names";

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
    <main className="mx-auto max-w-4xl px-5 sm:px-8">
      {/* Header */}
      <header className="flex h-16 items-center justify-between border-b border-line">
        <span className="flex items-center gap-2.5">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src="/avatar.jpg" alt="" className="h-8 w-8 rounded-full border border-line" />
          <span className="text-[15px] font-semibold tracking-tight">East Equity Agent</span>
        </span>
        <span className="flex items-center gap-2">
          {fresh && (
            <span
              title={fresh.detail}
              className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[11px] font-medium font-[family-name:var(--font-geist-mono)] ${freshClass}`}
            >
              <span className="inline-block h-1.5 w-1.5 rounded-full bg-current" aria-hidden />
              {fresh.label}
            </span>
          )}
          <span className="rounded-full border border-amber-300 bg-amber-50 px-2.5 py-0.5 text-[11px] font-medium text-amber-800 font-[family-name:var(--font-geist-mono)]">
            PAPER TRADING
          </span>
        </span>
      </header>

      {/* Intro + stats */}
      <section className="pt-14 pb-12">
        <h1 className="max-w-xl text-3xl sm:text-4xl font-semibold tracking-tight leading-tight">
          An AI agent that swing-trades the AI supply chain, in public.
        </h1>
        <p className="mt-4 max-w-xl text-ink-2 leading-relaxed">
          Long-only equities, compounding 10 to 15 percent swing gains over days to weeks, every
          decision published with its full reasoning. Research by Claude, hard rules enforced by
          code.
        </p>

        <dl className="mt-10 grid grid-cols-2 gap-3 sm:grid-cols-4 sm:gap-4">
          {stats.map((s) => (
            <div key={s.label} className="rounded-xl border border-line bg-white p-5">
              <dt className="text-[13px] text-ink-3">{s.label}</dt>
              <dd
                className={`mt-2 whitespace-nowrap text-2xl lg:text-3xl font-medium tracking-tight font-[family-name:var(--font-geist-mono)] ${
                  s.signed ? (s.positive ? "text-accent" : "text-red-700") : ""
                }`}
              >
                {s.value}
              </dd>
            </div>
          ))}
        </dl>

        <p className="mt-8 text-[13px] text-ink-3">
          Last run {lastRun}. {latest.schedule_note ?? "Runs on weekdays"}.
        </p>
      </section>

      {/* Equity curve */}
      <section className="border-t border-line py-12">
        <h2 className="text-lg font-semibold tracking-tight">Equity curve</h2>
        <p className="mt-1 text-sm text-ink-2">
          {usd(STARTING_CAPITAL)} starting capital, marked each session against buying and holding
          the S&amp;P 500.
        </p>
        <div className="mt-6">
          <EquityChart points={history} startingCapital={STARTING_CAPITAL} events={latest.trade_events} />
        </div>
      </section>

      {/* Performance record, appears once trades have closed */}
      {perf && (
        <section className="border-t border-line py-12">
          <h2 className="text-lg font-semibold tracking-tight">Performance record</h2>
          <dl className="mt-6 grid grid-cols-2 gap-y-8 sm:grid-cols-4">
            {perfTiles.map((s) => (
              <div key={s.label} className="border-l border-line pl-4">
                <dt className="text-[13px] text-ink-3">{s.label}</dt>
                <dd
                  className={`mt-1 text-xl font-medium tracking-tight font-[family-name:var(--font-geist-mono)] ${
                    s.accent ? "text-accent" : ""
                  }`}
                >
                  {s.value}
                </dd>
                {s.sub && (
                  <dd className="mt-0.5 text-[11px] text-ink-3 font-[family-name:var(--font-geist-mono)]">
                    {s.sub}
                  </dd>
                )}
              </div>
            ))}
          </dl>
        </section>
      )}

      {/* Confidence calibration, honesty feature: stated confidence vs realized win rate */}
      {latest.calibration && <Calibration data={latest.calibration} />}
      {latest.calibration && Object.keys(latest.calibration.by_confidence ?? {}).length > 0 && (
        <section className="border-t border-line py-12">
          <h2 className="text-lg font-semibold tracking-tight">Calibration curve</h2>
          <p className="mt-1 text-sm text-ink-2">
            Each confidence bucket plotted against perfect calibration - below the line is overconfidence.
          </p>
          <div className="mt-6 mx-auto max-w-sm">
            <CalibrationDiagram calibration={latest.calibration} />
          </div>
        </section>
      )}

      {/* Positions */}
      <section className="border-t border-line py-12">
        <h2 className="text-lg font-semibold tracking-tight">Open positions</h2>
        {positions.length === 0 ? (
          <div className="mt-6 rounded-lg border border-dashed border-line px-6 py-10 text-center">
            <p className="font-medium">Holding 100% cash</p>
            <p className="mx-auto mt-1.5 max-w-md text-sm text-ink-2 leading-relaxed">
              No current setup met the agent's bar for conviction and risk-reward. Passing is a
              position too. The reasoning is below.
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
                    <summary className="flex cursor-pointer list-none flex-wrap items-baseline gap-x-4 gap-y-1 [&::-webkit-details-marker]:hidden">
                      <span className="w-14 shrink-0 font-medium font-[family-name:var(--font-geist-mono)]">
                        {p.ticker}
                      </span>
                      <span className="text-[13px] text-ink-3 font-[family-name:var(--font-geist-mono)]">
                        {p.quantity} sh @ ${p.avg_cost.toFixed(2)}
                      </span>
                      <span className="ml-auto flex items-baseline gap-4">
                        <span className="text-sm font-[family-name:var(--font-geist-mono)]">
                          {usd(p.market_value_usd)}
                        </span>
                        <span
                          className={`text-sm font-[family-name:var(--font-geist-mono)] ${
                            pnl >= 0 ? "text-accent" : "text-red-700"
                          }`}
                        >
                          {pnl >= 0 ? "+" : ""}
                          {usd(pnl)} ({pnl >= 0 ? "+" : ""}
                          {pnlPct.toFixed(1)}%)
                        </span>
                        <span
                          className="shrink-0 text-ink-3 transition-transform group-open:rotate-90"
                          aria-hidden
                        >
                          ›
                        </span>
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
                              <div className="mt-0.5 font-[family-name:var(--font-geist-mono)]">{s.value}</div>
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
                                className={`mt-0.5 text-sm font-[family-name:var(--font-geist-mono)] ${
                                  risk.inside_noise_band ? "text-amber-700" : ""
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
                                <span className="font-[family-name:var(--font-geist-mono)]">
                                  {p.days_held ?? 0}d / {horizon}d
                                </span>
                              </div>
                              <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-line">
                                <div
                                  className={`h-full rounded-full ${
                                    horizonPct >= 100 ? "bg-red-700" : "bg-accent"
                                  }`}
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
                        Opened {new Date(p.opened_at).toLocaleDateString("en-US", {
                          month: "long",
                          day: "numeric",
                          timeZone: "America/New_York",
                        })}. The agent re-reviews this position against this plan on every run.
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
        <section className="border-t border-line py-12">
          <h2 className="text-lg font-semibold tracking-tight">Sector exposure</h2>
          <p className="mt-1 text-sm text-ink-2">Where the book is concentrated, as a share of total equity.</p>
          <div className="mt-6">
            <SectorExposure positions={positions} cash={cash} totalEquity={equity} />
          </div>
        </section>
      )}

      {/* What the agent is thinking */}
      {thinking && (
        <section className="border-t border-line py-12">
          <div className="flex items-baseline justify-between gap-4">
            <h2 className="text-lg font-semibold tracking-tight">What the agent is thinking</h2>
            <span className="text-[13px] text-ink-3 font-[family-name:var(--font-geist-mono)]">{lastRun}</span>
          </div>
          <p className="mt-4 text-[15px] leading-relaxed text-ink-2">{clean(thinking)}</p>

          {macro && (
            <div className="mt-6 flex flex-wrap gap-x-8 gap-y-2 text-[13px] text-ink-3 font-[family-name:var(--font-geist-mono)]">
              {macro.cpi_yoy_pct && (
                <span>CPI {macro.cpi_yoy_pct.latest}% {arrow(macro.cpi_yoy_pct.direction)}</span>
              )}
              {macro.ten_year_yield && (
                <span>10Y {macro.ten_year_yield.latest}% {arrow(macro.ten_year_yield.direction)}</span>
              )}
              {macro.vix && <span>VIX {macro.vix.latest} {arrow(macro.vix.direction)}</span>}
              {macro.yield_curve_10y2y && (
                <span>10Y-2Y {macro.yield_curve_10y2y.latest}% {arrow(macro.yield_curve_10y2y.direction)}</span>
              )}
              {macro.hy_credit_spread && (
                <span>HY spread {macro.hy_credit_spread.latest}% {arrow(macro.hy_credit_spread.direction)}</span>
              )}
            </div>
          )}
        </section>
      )}

      {/* Watchlist */}
      {(latest.watchlist ?? []).length > 0 && (
        <section className="border-t border-line py-12">
          <h2 className="text-lg font-semibold tracking-tight">Watchlist</h2>
          <p className="mt-1 text-sm text-ink-2">
            The most compelling potential positions right now, ranked. Expand a name to read the
            agent's current thinking.
          </p>
          <ul className="mt-6 divide-y divide-line/60">
            {latest.watchlist!.map((w) => (
              <li key={w.ticker}>
                <details className="group py-4">
                  <summary className="flex cursor-pointer list-none items-baseline gap-4 [&::-webkit-details-marker]:hidden">
                    <span className="w-14 shrink-0 font-medium font-[family-name:var(--font-geist-mono)]">
                      {w.ticker}
                    </span>
                    <span className="flex-1 text-sm text-ink-2 leading-relaxed">{clean(w.one_line)}</span>
                    <span className="shrink-0 text-ink-3 transition-transform group-open:rotate-90" aria-hidden>
                      ›
                    </span>
                  </summary>
                  <div className="pb-2 pl-[4.5rem] pr-6">
                    <p className="text-sm text-ink-2 leading-relaxed">{clean(w.thoughts)}</p>
                    {w.would_buy_at && (
                      <p className="mt-2 text-[13px] text-ink-3 font-[family-name:var(--font-geist-mono)]">
                        Would buy: {clean(w.would_buy_at)}
                      </p>
                    )}
                  </div>
                </details>
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* Watchlist outcomes: did the agent's calls play out? */}
      {watchlistOutcomes.length > 0 && <WatchlistOutcomes outcomes={watchlistOutcomes} />}

      {/* Latest activity */}
      <section className="border-t border-line py-12">
        <h2 className="text-lg font-semibold tracking-tight">Latest activity</h2>

        {latest.fills.length > 0 ? (
          <ul className="mt-4 space-y-2">
            {latest.fills.map((f, i) => (
              <li key={i} className="text-sm font-[family-name:var(--font-geist-mono)]">
                {f.action === "BUY" ? "Opened" : "Closed"} {f.ticker}: {f.quantity} shares at $
                {f.fill_price}
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-2 text-sm text-ink-2">
            No orders on the last run.
          </p>
        )}

        {rejected.length > 0 && (
          <div className="mt-6">
            <h3 className="text-sm font-medium">Rejected by the validator</h3>
            <ul className="mt-2 space-y-1.5">
              {rejected.map((p, i) => (
                <li key={i} className="text-sm text-ink-2">
                  <span className="font-[family-name:var(--font-geist-mono)]">
                    {String(p.proposal.ticker)}
                  </span>
                  : {p.reasons.join(", ")}
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
        <section className="border-t border-line py-12">
          <h2 className="text-lg font-semibold tracking-tight">Closed results</h2>
          <p className="mt-1 text-sm text-ink-2">
            Every finished trade, newest first, scored against its own written plan.
          </p>
          <ClosedTrades trades={closedTrades} />
        </section>
      )}

      {/* Costs & drag over time */}
      {closedTrades.length > 0 && (
        <section className="border-t border-line py-12">
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

      {/* What improved */}
      {improvements.length > 0 && (
        <section className="border-t border-line py-12">
          <h2 className="text-lg font-semibold tracking-tight">What improved</h2>
          <p className="mt-1 max-w-2xl text-sm text-ink-2">
            After every run the agent writes down one concrete thing that would have made it sharper -
            a data gap, a tooling gripe, a lesson learned. These are its own unedited notes, not a
            changelog of shipped fixes.
          </p>
          <ul className="mt-6 space-y-5">
            {improvements.slice(0, 3).map((im, i) => (
              <li key={i} className="flex gap-5">
                <span className="shrink-0 text-[13px] text-ink-3 font-[family-name:var(--font-geist-mono)] pt-0.5">
                  {im.date}
                </span>
                <p className="text-sm text-ink-2 leading-relaxed">{clean(im.note)}</p>
              </li>
            ))}
          </ul>
          {improvements.length > 3 && (
            <details className="group mt-5">
              <summary className="cursor-pointer list-none text-sm text-ink-2 hover:text-ink [&::-webkit-details-marker]:hidden">
                <span className="group-open:hidden">Show {improvements.length - 3} earlier updates</span>
                <span className="hidden group-open:inline">Hide earlier updates</span>
              </summary>
              <ul className="mt-5 space-y-5">
                {improvements.slice(3).map((im, i) => (
                  <li key={i} className="flex gap-5">
                    <span className="shrink-0 text-[13px] text-ink-3 font-[family-name:var(--font-geist-mono)] pt-0.5">
                      {im.date}
                    </span>
                    <p className="text-sm text-ink-2 leading-relaxed">{clean(im.note)}</p>
                  </li>
                ))}
              </ul>
            </details>
          )}
        </section>
      )}

      {/* How it works */}
      <section className="border-t border-line py-12">
        <h2 className="text-lg font-semibold tracking-tight">How it works</h2>
        <div className="mt-6 grid gap-8 sm:grid-cols-3">
          <div>
            <h3 className="font-medium">Research</h3>
            <p className="mt-1.5 text-sm text-ink-2 leading-relaxed">
              Claude reads macro data, SEC filings, 13F flows, and price structure across{" "}
              {universeLabel} in semiconductors, networking, power, and data center infrastructure.
            </p>
          </div>
          <div>
            <h3 className="font-medium">Guardrails</h3>
            <p className="mt-1.5 text-sm text-ink-2 leading-relaxed">
              Deterministic code enforces every rule: long-only, no options or margin, 3 to 90
              day swing horizons, position caps, a 10 percent minimum upside target, never
              risking more than the expected gain, and a kill switch.
            </p>
          </div>
          <div>
            <h3 className="font-medium">Transparency</h3>
            <p className="mt-1.5 text-sm text-ink-2 leading-relaxed">
              Every proposal, rejection, and fill is journaled and published here, including the
              trades the validator refused to allow.
            </p>
          </div>
        </div>
      </section>

      <footer className="border-t border-line py-8">
        <p className="text-[13px] text-ink-3 leading-relaxed">
          East Equity Agent is a research experiment in agentic trading, currently running on
          simulated capital. Nothing here is financial advice.
        </p>
      </footer>
    </main>
  );
}
