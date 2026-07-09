import fs from "node:fs";
import path from "node:path";
import EquityChart from "@/components/EquityChart";
import ClosedTrades from "@/components/ClosedTrades";

type Position = {
  ticker: string;
  quantity: number;
  avg_cost: number;
  market_value_usd: number;
  last_price?: number;
  opened_at: string;
};

type Indicator = { latest: number; direction: string } | null;

type Latest = {
  generated_at: string;
  run_id: string;
  mode: string;
  schedule_note?: string;
  portfolio: { cash_usd: number; total_equity_usd: number; positions: Position[] };
  no_trade_reason?: string | null;
  commentary?: string | null;
  macro_snapshot?: { cpi_yoy_pct: Indicator; ten_year_yield: Indicator; vix: Indicator } | null;
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
  }[];
  performance?: {
    closed_trades: number;
    win_rate_pct: number;
    realized_pnl_usd: number;
    avg_r_multiple: number | null;
    avg_days_held: number;
    max_drawdown_pct: number;
  } | null;
  improvements?: { date: string; note: string }[];
  watchlist?: { ticker: string; one_line: string; thoughts: string; would_buy_at?: string | null }[];
};

type HistoryPoint = { date: string; equity: number; benchmark_close?: number | null };

const STARTING_CAPITAL = 10000;

function readJson<T>(file: string): T {
  return JSON.parse(fs.readFileSync(path.join(process.cwd(), "data", file), "utf-8"));
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

function arrow(direction?: string) {
  return direction === "rising" ? "↑" : direction === "falling" ? "↓" : "";
}

export default function Home() {
  const latest = readJson<Latest>("latest.json");
  const history = readJson<HistoryPoint[]>("equity_history.json");

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

  const lastRun = new Date(latest.generated_at).toLocaleDateString("en-US", {
    month: "long",
    day: "numeric",
    year: "numeric",
  });

  const stats = [
    { label: "Portfolio value", value: usd(equity) },
    {
      label: "Total return",
      value: `${returnPct >= 0 ? "+" : ""}${returnPct.toFixed(2)}%`,
      accent: returnPct > 0,
    },
    {
      label: "vs S&P 500",
      value:
        excessPts === null ? "n/a" : `${excessPts >= 0 ? "+" : ""}${excessPts.toFixed(2)} pts`,
      accent: excessPts !== null && excessPts > 0,
    },
    { label: "Cash", value: usd(cash) },
  ];

  const perf = latest.performance;
  const thinking = latest.commentary ?? latest.no_trade_reason;
  const macro = latest.macro_snapshot;
  const closedTrades = latest.closed_trades ?? [];
  const improvements = latest.improvements ?? [];
  const rejected = latest.proposals.filter((p) => !p.approved);

  return (
    <main className="mx-auto max-w-4xl px-5 sm:px-8">
      {/* Header */}
      <header className="flex h-16 items-center justify-between border-b border-line">
        <span className="text-[15px] font-semibold tracking-tight">East Equity Agent</span>
        <span className="rounded-full border border-amber-300 bg-amber-50 px-2.5 py-0.5 text-[11px] font-medium text-amber-800 font-[family-name:var(--font-geist-mono)]">
          PAPER TRADING
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

        <dl className="mt-10 grid grid-cols-2 gap-y-8 sm:grid-cols-4">
          {stats.map((s) => (
            <div key={s.label} className="border-l border-line pl-4">
              <dt className="text-[13px] text-ink-3">{s.label}</dt>
              <dd
                className={`mt-1 text-2xl font-medium tracking-tight font-[family-name:var(--font-geist-mono)] ${
                  s.accent ? "text-accent" : ""
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
          <EquityChart points={history} startingCapital={STARTING_CAPITAL} />
        </div>
      </section>

      {/* Performance record, appears once trades have closed */}
      {perf && (
        <section className="border-t border-line py-12">
          <h2 className="text-lg font-semibold tracking-tight">Performance record</h2>
          <dl className="mt-6 grid grid-cols-2 gap-y-8 sm:grid-cols-3 lg:grid-cols-6">
            {[
              { label: "Closed trades", value: String(perf.closed_trades) },
              { label: "Win rate", value: `${perf.win_rate_pct}%` },
              {
                label: "Realized P&L",
                value: `${perf.realized_pnl_usd >= 0 ? "+" : ""}${usd(perf.realized_pnl_usd)}`,
                accent: perf.realized_pnl_usd > 0,
              },
              {
                label: "Avg R multiple",
                value: perf.avg_r_multiple === null ? "n/a" : `${perf.avg_r_multiple}R`,
              },
              { label: "Avg days held", value: String(perf.avg_days_held) },
              { label: "Max drawdown", value: `${perf.max_drawdown_pct}%` },
            ].map((s) => (
              <div key={s.label} className="border-l border-line pl-4">
                <dt className="text-[13px] text-ink-3">{s.label}</dt>
                <dd
                  className={`mt-1 text-xl font-medium tracking-tight font-[family-name:var(--font-geist-mono)] ${
                    s.accent ? "text-accent" : ""
                  }`}
                >
                  {s.value}
                </dd>
              </div>
            ))}
          </dl>
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
          <div className="mt-6 overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-line text-left text-[13px] text-ink-3">
                  <th className="py-2 pr-4 font-normal">Ticker</th>
                  <th className="py-2 pr-4 font-normal">Shares</th>
                  <th className="py-2 pr-4 font-normal">Avg cost</th>
                  <th className="py-2 pr-4 font-normal">Value</th>
                  <th className="py-2 font-normal">P&amp;L</th>
                </tr>
              </thead>
              <tbody className="font-[family-name:var(--font-geist-mono)]">
                {positions.map((p) => {
                  const pnl = p.market_value_usd - p.quantity * p.avg_cost;
                  return (
                    <tr key={p.ticker} className="border-b border-line/60">
                      <td className="py-3 pr-4 font-medium">{p.ticker}</td>
                      <td className="py-3 pr-4">{p.quantity}</td>
                      <td className="py-3 pr-4">${p.avg_cost.toFixed(2)}</td>
                      <td className="py-3 pr-4">{usd(p.market_value_usd)}</td>
                      <td className={`py-3 ${pnl >= 0 ? "text-accent" : "text-red-700"}`}>
                        {pnl >= 0 ? "+" : ""}
                        {usd(pnl)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* What the agent is thinking */}
      {thinking && (
        <section className="border-t border-line py-12">
          <div className="flex items-baseline justify-between gap-4">
            <h2 className="text-lg font-semibold tracking-tight">What the agent is thinking</h2>
            <span className="text-[13px] text-ink-3 font-[family-name:var(--font-geist-mono)]">{lastRun}</span>
          </div>
          <p className="mt-4 max-w-2xl text-[15px] leading-relaxed text-ink-2">{clean(thinking)}</p>

          {macro && (
            <div className="mt-6 flex flex-wrap gap-x-8 gap-y-2 text-[13px] text-ink-3 font-[family-name:var(--font-geist-mono)]">
              {macro.cpi_yoy_pct && (
                <span>CPI {macro.cpi_yoy_pct.latest}% {arrow(macro.cpi_yoy_pct.direction)}</span>
              )}
              {macro.ten_year_yield && (
                <span>10Y {macro.ten_year_yield.latest}% {arrow(macro.ten_year_yield.direction)}</span>
              )}
              {macro.vix && <span>VIX {macro.vix.latest} {arrow(macro.vix.direction)}</span>}
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

      {/* What improved */}
      {improvements.length > 0 && (
        <section className="border-t border-line py-12">
          <h2 className="text-lg font-semibold tracking-tight">What improved</h2>
          <p className="mt-1 text-sm text-ink-2">
            The agent critiques its own process after every run. Changes that ship land here.
          </p>
          <ul className="mt-6 space-y-5">
            {improvements.map((im, i) => (
              <li key={i} className="flex gap-5">
                <span className="shrink-0 text-[13px] text-ink-3 font-[family-name:var(--font-geist-mono)] pt-0.5">
                  {im.date}
                </span>
                <p className="text-sm text-ink-2 leading-relaxed">{clean(im.note)}</p>
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* How it works */}
      <section className="border-t border-line py-12">
        <h2 className="text-lg font-semibold tracking-tight">How it works</h2>
        <div className="mt-6 grid gap-8 sm:grid-cols-3">
          <div>
            <h3 className="font-medium">Research</h3>
            <p className="mt-1.5 text-sm text-ink-2 leading-relaxed">
              Claude reads macro data, SEC filings, 13F flows, and price structure across 57 names
              in semiconductors, networking, power, and data center infrastructure.
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
