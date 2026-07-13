import fs from "node:fs";
import path from "node:path";
import Link from "next/link";
import type { Metadata } from "next";

type RunIndexEntry = {
  run_id: string;
  date: string;
  mode: string;
  n_fills: number;
  tickers_traded: string[];
  headline: string;
};

export const metadata: Metadata = {
  title: "Run archive - East Equity Agent",
  description:
    "Every reasoning run the agent has published, newest first. Open any run to read its full chain-of-thought, proposals, and fills.",
};

// Safe disk read: these archive files are optional and may not exist yet at build
// time, so a missing/corrupt file yields null (an empty state), never a crash.
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

// A bare calendar date (YYYY-MM-DD) is rendered as-is so timezone math can't shift
// it a day; a full timestamp is displayed in America/New_York like the rest of the site.
function formatDate(s: string): string {
  if (!s) return "";
  const bare = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
  if (bare) {
    const d = new Date(Number(bare[1]), Number(bare[2]) - 1, Number(bare[3]));
    return d.toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric" });
  }
  const d = new Date(s);
  if (isNaN(d.getTime())) return s;
  return d.toLocaleDateString("en-US", {
    month: "long",
    day: "numeric",
    year: "numeric",
    timeZone: "America/New_York",
  });
}

export default function RunsIndex() {
  const index = readJsonSafe<RunIndexEntry[]>("runs_index.json") ?? [];
  // Index is stored in append order; the archive reads newest first.
  const runs = [...index].reverse();

  return (
    <main className="mx-auto max-w-4xl px-5 sm:px-8">
      {/* Header */}
      <header className="flex h-16 items-center justify-between border-b border-line">
        <Link href="/" className="flex items-center gap-2.5">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src="/avatar.jpg" alt="" className="h-8 w-8 rounded-full border border-line" />
          <span className="text-[15px] font-semibold tracking-tight">East Equity Agent</span>
        </Link>
        <span className="rounded-full border border-amber-300 bg-amber-50 px-2.5 py-0.5 text-[11px] font-medium text-amber-800 font-[family-name:var(--font-geist-mono)]">
          PAPER TRADING
        </span>
      </header>

      {/* Intro */}
      <section className="pt-14 pb-12">
        <Link
          href="/"
          className="text-[13px] text-ink-3 hover:text-ink font-[family-name:var(--font-geist-mono)]"
        >
          ← Back to dashboard
        </Link>
        <h1 className="mt-4 text-3xl sm:text-4xl font-semibold tracking-tight leading-tight">
          Run archive
        </h1>
        <p className="mt-4 max-w-xl text-ink-2 leading-relaxed">
          Every reasoning run the agent has published, newest first. Open a run to read its full
          chain-of-thought, the proposals it made, and what the validator approved.
        </p>
      </section>

      {/* Runs list */}
      <section className="border-t border-line py-12">
        {runs.length === 0 ? (
          <div className="rounded-lg border border-dashed border-line px-6 py-10 text-center">
            <p className="font-medium">No runs archived yet</p>
            <p className="mx-auto mt-1.5 max-w-md text-sm text-ink-2 leading-relaxed">
              Published runs will appear here as the agent works. Check back after its next session.
            </p>
          </div>
        ) : (
          <ul className="divide-y divide-line/60">
            {runs.map((r) => {
              const tickers = r.tickers_traded ?? [];
              const isPaper = (r.mode ?? "").toLowerCase() === "paper";
              return (
                <li key={r.run_id}>
                  <Link
                    href={`/runs/${r.run_id}`}
                    className="group flex flex-col gap-2 py-5 sm:flex-row sm:items-baseline sm:gap-5"
                  >
                    <div className="flex shrink-0 items-center gap-3 sm:w-56">
                      <span className="text-[13px] text-ink-3 font-[family-name:var(--font-geist-mono)]">
                        {formatDate(r.date)}
                      </span>
                      {r.mode && (
                        <span
                          className={`rounded-full border px-2 py-0.5 text-[10px] font-medium uppercase font-[family-name:var(--font-geist-mono)] ${
                            isPaper
                              ? "border-amber-300 bg-amber-50 text-amber-800"
                              : "border-line bg-white text-ink-3"
                          }`}
                        >
                          {r.mode}
                        </span>
                      )}
                    </div>
                    <div className="min-w-0 flex-1">
                      <p className="text-sm leading-relaxed text-ink group-hover:text-accent">
                        {clean(r.headline ?? "")}
                      </p>
                      <p className="mt-1 text-[13px] text-ink-3 font-[family-name:var(--font-geist-mono)]">
                        {tickers.length > 0 ? `Traded ${tickers.join(", ")}` : "No trade"}
                      </p>
                    </div>
                    <span
                      className="hidden shrink-0 text-ink-3 transition-transform group-hover:translate-x-0.5 sm:block"
                      aria-hidden
                    >
                      ›
                    </span>
                  </Link>
                </li>
              );
            })}
          </ul>
        )}
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
