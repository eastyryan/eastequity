import fs from "node:fs";
import path from "node:path";
import EquityChart from "@/components/EquityChart";

type Position = {
  ticker: string;
  quantity: number;
  avg_cost: number;
  market_value_usd: number;
  last_price?: number;
  opened_at: string;
};

type Latest = {
  generated_at: string;
  run_id: string;
  mode: string;
  portfolio: { cash_usd: number; total_equity_usd: number; positions: Position[] };
  no_trade_reason?: string | null;
  proposals: { proposal: Record<string, unknown>; approved: boolean; reasons: string[] }[];
  fills: { ticker: string; action: string; fill_price: number; quantity: number }[];
};

const STARTING_CAPITAL = 10000;

function readJson<T>(file: string): T {
  return JSON.parse(fs.readFileSync(path.join(process.cwd(), "data", file), "utf-8"));
}

function clean(s: string) {
  return s.replace(/—/g, "-").replace(/–/g, "-").replace(/\*\*/g, "");
}

function usd(n: number) {
  return n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
}

export default function Home() {
  const latest = readJson<Latest>("latest.json");
  const history = readJson<{ date: string; equity: number }[]>("equity_history.json");

  const equity = latest.portfolio.total_equity_usd;
  const cash = latest.portfolio.cash_usd;
  const positions = latest.portfolio.positions ?? [];
  const returnPct = ((equity - STARTING_CAPITAL) / STARTING_CAPITAL) * 100;
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
    { label: "Cash", value: usd(cash) },
    { label: "Open positions", value: String(positions.length) },
  ];

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
          Long-only equities, multi-week holding periods, every decision published with its full
          reasoning. Research by Claude, hard rules enforced by code.
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
      </section>

      {/* Equity curve */}
      <section className="border-t border-line py-12">
        <h2 className="text-lg font-semibold tracking-tight">Equity curve</h2>
        <p className="mt-1 text-sm text-ink-2">
          {usd(STARTING_CAPITAL)} starting capital, marked daily.
        </p>
        <div className="mt-6">
          <EquityChart points={history} />
        </div>
      </section>

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

      {/* Latest decision */}
      <section className="border-t border-line py-12">
        <div className="flex items-baseline justify-between gap-4">
          <h2 className="text-lg font-semibold tracking-tight">Latest decision</h2>
          <span className="text-[13px] text-ink-3 font-[family-name:var(--font-geist-mono)]">{lastRun}</span>
        </div>

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
          <p className="mt-2 text-sm font-medium text-accent">No trade</p>
        )}

        {latest.no_trade_reason && (
          <blockquote className="mt-4 border-l-2 border-accent/40 pl-4 text-[15px] leading-relaxed text-ink-2">
            {clean(latest.no_trade_reason)}
          </blockquote>
        )}

        {latest.proposals.filter((p) => !p.approved).length > 0 && (
          <div className="mt-6">
            <h3 className="text-sm font-medium">Rejected by the validator</h3>
            <ul className="mt-2 space-y-1.5">
              {latest.proposals
                .filter((p) => !p.approved)
                .map((p, i) => (
                  <li key={i} className="text-sm text-ink-2">
                    <span className="font-[family-name:var(--font-geist-mono)]">
                      {String(p.proposal.ticker)}
                    </span>{" "}
                    · {p.reasons.join(", ")}
                  </li>
                ))}
            </ul>
          </div>
        )}
      </section>

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
              Deterministic code enforces every rule: long-only, no options or margin, 3 to 90 day
              horizons, position caps, minimum 2:1 reward-to-risk, and a kill switch.
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
