import fs from "node:fs";
import path from "node:path";
import Link from "next/link";
import { notFound } from "next/navigation";
import ClosedTrades from "@/components/ClosedTrades";

type Proposal = {
  proposal: Record<string, unknown>;
  approved: boolean;
  reasons: string[];
};

type Fill = { ticker: string; action: string; fill_price: number; quantity: number };

// Structurally compatible with the ClosedTrades component's own trade type.
type ClosedTrade = {
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
};

type RunFile = {
  generated_at: string;
  run_id: string;
  mode: string;
  commentary?: string | null;
  no_trade_reason?: string | null;
  latest_reasoning?: string | null;
  proposals?: Proposal[];
  fills?: Fill[];
  closed_trades?: ClosedTrade[];
};

type RunIndexEntry = { run_id: string };

// Safe disk read: run files are optional and may not exist yet, so a missing/corrupt
// file yields null and the page renders a graceful "run not found" instead of crashing.
function readJsonSafe<T>(file: string): T | null {
  try {
    const p = path.join(process.cwd(), "data", file);
    if (!fs.existsSync(p)) return null;
    return JSON.parse(fs.readFileSync(p, "utf-8")) as T;
  } catch {
    return null;
  }
}

// Mirror of page.tsx's markdown-stripping helper for any agent prose.
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

function num(v: unknown): number | null {
  return typeof v === "number" && !Number.isNaN(v) ? v : null;
}

function str(v: unknown): string | null {
  return typeof v === "string" && v.length > 0 ? v : null;
}

// A run id maps to run_<id>.json. Guard against any path funny-business in the slug.
function safeRunId(id: string): string | null {
  return /^[A-Za-z0-9_-]+$/.test(id) ? id : null;
}

// Pre-render every run that is in the index; unknown ids still render on demand
// (dynamicParams defaults to true) and fall back to notFound() if the file is absent.
export async function generateStaticParams() {
  const index = readJsonSafe<RunIndexEntry[]>("runs_index.json");
  if (!index) return [];
  return index.map((r) => ({ id: r.run_id }));
}

function ProposalCard({ item }: { item: Proposal }) {
  const p = item.proposal ?? {};
  const ticker = str(p.ticker) ?? "—";
  const action = str(p.action);
  const thesis = str(p.thesis);
  const size = num(p.position_size_usd);
  const entry = num(p.entry_price_max);
  const stop = num(p.stop_loss);
  const target = num(p.target_price);
  const conf = num(p.confidence);
  const horizon = num(p.holding_horizon_days);
  const rr = num(p.risk_reward_ratio);

  const stats: { label: string; value: string }[] = [];
  if (size != null) stats.push({ label: "Size", value: usd(size) });
  if (entry != null) stats.push({ label: "Entry ≤", value: `$${entry}` });
  if (stop != null) stats.push({ label: "Stop", value: `$${stop}` });
  if (target != null) stats.push({ label: "Target", value: `$${target}` });
  if (conf != null) stats.push({ label: "Confidence", value: `${Math.round(conf * 100)}%` });
  if (horizon != null) stats.push({ label: "Horizon", value: `${horizon}d` });
  if (rr != null) stats.push({ label: "Reward / risk", value: `${rr}` });

  const reasons = (item.reasons ?? []).filter((r) => r && r.length > 0);

  return (
    <div className="ds-card p-5">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <div className="flex items-baseline gap-3">
          <span className="font-medium font-[family-name:var(--font-mono)]">{ticker}</span>
          {action && <span className="text-[13px] text-ink-3">{action}</span>}
        </div>
        <span
          className={`rounded-full border px-2 py-0.5 text-[10px] font-medium uppercase font-[family-name:var(--font-mono)] ${
            item.approved
              ? "border-pos/30 bg-pos-soft text-accent"
              : "border-neg/30 bg-neg-soft text-neg"
          }`}
        >
          {item.approved ? "Approved" : "Rejected"}
        </span>
      </div>

      {stats.length > 0 && (
        <div className="mt-4 grid grid-cols-2 gap-y-3 sm:grid-cols-4 text-sm">
          {stats.map((s) => (
            <div key={s.label} className="border-l border-line pl-3">
              <div className="text-[12px] text-ink-3">{s.label}</div>
              <div className="mt-0.5 font-[family-name:var(--font-mono)]">{s.value}</div>
            </div>
          ))}
        </div>
      )}

      {thesis && <p className="mt-4 text-sm text-ink-2 leading-relaxed">{clean(thesis)}</p>}

      {reasons.length > 0 && (
        <div className="mt-4">
          <h4 className="text-[12px] font-medium text-ink-3">
            {item.approved ? "Validator notes" : "Why the validator rejected it"}
          </h4>
          <ul className="mt-1 list-disc space-y-1 pl-5 text-sm text-ink-2 leading-relaxed">
            {reasons.map((r, i) => (
              <li key={i}>{clean(r)}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

export default async function RunPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const runId = safeRunId(id);
  const run = runId ? readJsonSafe<RunFile>(`run_${runId}.json`) : null;

  if (!run) {
    return (
      <main className="mx-auto max-w-5xl px-5 pb-10 sm:px-8">
        <header className="flex h-[68px] items-center justify-between border-b border-line">
          <Link href="/" className="flex items-center gap-2.5">
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src="/avatar.jpg" alt="" className="h-8 w-8 rounded-full border border-line" />
            <span className="text-[15px] font-semibold tracking-tight">East Equity Agent</span>
          </Link>
        </header>
        <section className="py-24 text-center">
          <h1 className="text-2xl font-semibold tracking-tight">Run not found</h1>
          <p className="mx-auto mt-2 max-w-md text-sm text-ink-2 leading-relaxed">
            There is no archived run with that id. It may not have been published yet.
          </p>
          <Link
            href="/runs"
            className="mt-6 inline-block rounded-md border border-line px-3.5 py-1.5 text-sm text-ink-2 hover:border-line-2 hover:text-ink"
          >
            ← All runs
          </Link>
        </section>
      </main>
    );
  }

  const when = new Date(run.generated_at);
  const validWhen = !Number.isNaN(when.getTime());
  const dateLabel = validWhen
    ? when.toLocaleDateString("en-US", {
        month: "long",
        day: "numeric",
        year: "numeric",
        timeZone: "America/New_York",
      })
    : "";
  const timeLabel = validWhen
    ? `${when.toLocaleTimeString("en-US", {
        hour: "numeric",
        minute: "2-digit",
        timeZone: "America/New_York",
      })} ET`
    : "";

  const commentary = run.commentary ?? run.no_trade_reason;
  const proposals = run.proposals ?? [];
  const approved = proposals.filter((p) => p.approved);
  const rejected = proposals.filter((p) => !p.approved);
  const fills = run.fills ?? [];
  const closed = run.closed_trades ?? [];
  const reasoning = run.latest_reasoning;

  return (
    <main className="mx-auto max-w-5xl px-5 pb-10 sm:px-8">
      {/* Header */}
      <header className="flex h-[68px] items-center justify-between border-b border-line">
        <Link href="/" className="flex items-center gap-2.5">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src="/avatar.jpg" alt="" className="h-8 w-8 rounded-full border border-line" />
          <span className="text-[15px] font-semibold tracking-tight">East Equity Agent</span>
        </Link>
        <Link
          href="/runs"
          className="text-[13px] text-ink-3 hover:text-ink font-[family-name:var(--font-mono)]"
        >
          All runs
        </Link>
      </header>

      {/* Title */}
      <section className="pt-14 pb-12">
        <Link
          href="/runs"
          className="text-[13px] text-ink-3 hover:text-ink font-[family-name:var(--font-mono)]"
        >
          ← Run archive
        </Link>
        <h1 className="mt-4 text-3xl sm:text-4xl font-semibold tracking-tight leading-tight">
          {dateLabel || "Run"}
        </h1>
        <p className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-[13px] text-ink-3 font-[family-name:var(--font-mono)]">
          {timeLabel && <span>{timeLabel}</span>}
          {run.mode && (
            <>
              <span aria-hidden>·</span>
              <span className="uppercase">{run.mode}</span>
            </>
          )}
          <span aria-hidden>·</span>
          <span>{run.run_id}</span>
        </p>
      </section>

      {/* Commentary */}
      {commentary && (
        <section className="ds-card mt-5 p-5 sm:p-7">
          <h2 className="text-lg font-semibold tracking-tight">What the agent said</h2>
          <p className="mt-4 text-[15px] leading-relaxed text-ink-2">{clean(commentary)}</p>
        </section>
      )}

      {/* Full reasoning / chain-of-thought */}
      {reasoning && (
        <section className="ds-card mt-5 p-5 sm:p-7">
          <h2 className="text-lg font-semibold tracking-tight">Full reasoning</h2>
          <p className="mt-1 text-sm text-ink-2">
            The agent&apos;s complete chain-of-thought for this run, unedited.
          </p>
          <div className="mt-6 overflow-x-auto ds-card p-5">
            <pre className="whitespace-pre-wrap break-words text-[13px] leading-relaxed text-ink-2 font-[family-name:var(--font-mono)]">
              {clean(reasoning)}
            </pre>
          </div>
        </section>
      )}

      {/* Proposals */}
      {proposals.length > 0 && (
        <section className="ds-card mt-5 p-5 sm:p-7">
          <h2 className="text-lg font-semibold tracking-tight">Proposals</h2>
          <p className="mt-1 text-sm text-ink-2">
            Every idea the agent put to the validator this run, approved and rejected.
          </p>

          {approved.length > 0 && (
            <div className="mt-6">
              <h3 className="text-sm font-medium">Approved</h3>
              <div className="mt-3 space-y-4">
                {approved.map((p, i) => (
                  <ProposalCard key={`a-${i}`} item={p} />
                ))}
              </div>
            </div>
          )}

          {rejected.length > 0 && (
            <div className="mt-8">
              <h3 className="text-sm font-medium">Rejected by the validator</h3>
              <div className="mt-3 space-y-4">
                {rejected.map((p, i) => (
                  <ProposalCard key={`r-${i}`} item={p} />
                ))}
              </div>
            </div>
          )}
        </section>
      )}

      {/* Fills */}
      <section className="ds-card mt-5 p-5 sm:p-7">
        <h2 className="text-lg font-semibold tracking-tight">Orders filled</h2>
        {fills.length > 0 ? (
          <ul className="mt-4 space-y-2">
            {fills.map((f, i) => (
              <li key={i} className="text-sm font-[family-name:var(--font-mono)]">
                {f.action === "BUY" ? "Opened" : "Closed"} {f.ticker}: {f.quantity} shares at $
                {f.fill_price}
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-2 text-sm text-ink-2">No orders on this run.</p>
        )}
      </section>

      {/* Closed trades */}
      {closed.length > 0 && (
        <section className="ds-card mt-5 p-5 sm:p-7">
          <h2 className="text-lg font-semibold tracking-tight">Closed on this run</h2>
          <p className="mt-1 text-sm text-ink-2">
            Positions the agent exited this session, scored against their own plans.
          </p>
          <ClosedTrades trades={closed} />
        </section>
      )}

      <footer className="border-t border-line py-8">
        <p className="text-[13px] text-ink-3 leading-relaxed">
          East Equity Agent is a research experiment in agentic trading, currently running on
          simulated capital. Nothing here is financial advice.
        </p>
      </footer>
    </main>
  );
}
