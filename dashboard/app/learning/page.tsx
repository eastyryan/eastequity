import fs from "node:fs";
import path from "node:path";
import Link from "next/link";
import type { Metadata } from "next";

type LearningEntry = {
  id: string;
  discipline: string;
  topic: string;
  summary: string;
  key_points?: string[];
  how_to_apply?: string;
  sources?: string[];
  learned_at: string;
  evidence_status?: "validated" | "mixed" | "underperforming" | null;
  outcome?: { n?: number; wins?: number } | null;
  times_cited?: number;
};

type LearningJournal = {
  updated_at?: string;
  note?: string;
  discipline_counts?: Record<string, number>;
  entries?: LearningEntry[];
};

export const metadata: Metadata = {
  title: "Study hall - East Equity Agent",
  description:
    "The agent's daily learning journal: one researched trading lesson per weekday, injected into every future trading run.",
};

// Safe disk read: the journal is optional and may not exist yet at build time,
// so a missing/corrupt file yields null (an empty state), never a crash.
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

function formatDate(s: string): string {
  if (!s) return "";
  const d = new Date(s);
  if (isNaN(d.getTime())) return s.slice(0, 10);
  return d.toLocaleDateString("en-US", {
    month: "long",
    day: "numeric",
    year: "numeric",
    timeZone: "America/New_York",
  });
}

function disciplineLabel(d: string): string {
  return d.replace(/_/g, " ");
}

function hostnameOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url.slice(0, 60);
  }
}

export default function LearningJournalPage() {
  const journal = readJsonSafe<LearningJournal>("learning_journal.json");
  const entries = journal?.entries ?? [];
  const counts = journal?.discipline_counts ?? {};
  const studied = Object.entries(counts).filter(([, n]) => n > 0);

  return (
    <main className="mx-auto max-w-5xl px-5 pb-10 sm:px-8">
      {/* Header */}
      <header className="flex h-[68px] items-center justify-between border-b border-line">
        <Link href="/" className="flex items-center gap-2.5">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src="/avatar.jpg" alt="" className="h-8 w-8 rounded-full border border-line" />
          <span className="text-[15px] font-semibold tracking-tight">East Equity Agent</span>
        </Link>
        <span className="rounded-full border border-attn/40 bg-attn-soft px-2.5 py-0.5 text-[11px] font-medium text-attn font-[family-name:var(--font-mono)]">
          PAPER TRADING
        </span>
      </header>

      {/* Intro */}
      <section className="pt-14 pb-12">
        <Link
          href="/"
          className="text-[13px] text-ink-3 hover:text-ink font-[family-name:var(--font-mono)]"
        >
          ← Back to dashboard
        </Link>
        <h1 className="mt-4 text-3xl sm:text-4xl font-semibold tracking-tight leading-tight">
          Study hall
        </h1>
        <p className="mt-4 max-w-xl text-ink-2 leading-relaxed">
          Every weekday after the close, the agent studies one trading topic properly - researched
          from primary sources, written up as a durable lesson, and injected into every future
          trading run. The curriculum is weighted toward whatever its own trade feedback says is
          weakest. This is that journal, newest first.
        </p>
        {studied.length > 0 && (
          <div className="mt-6 flex flex-wrap gap-2">
            {studied.map(([d, n]) => (
              <span
                key={d}
                className="rounded-full border border-line bg-card px-2.5 py-0.5 text-[11px] text-ink-3 font-[family-name:var(--font-mono)]"
              >
                {disciplineLabel(d)} · {n}
              </span>
            ))}
          </div>
        )}
      </section>

      {/* Lessons */}
      {entries.length === 0 ? (
        <section className="ds-card mt-5 p-5 sm:p-7">
          <div className="rounded-lg border border-dashed border-line px-6 py-10 text-center">
            <p className="font-medium">No lessons yet</p>
            <p className="mx-auto mt-1.5 max-w-md text-sm text-ink-2 leading-relaxed">
              The first study session runs after the next market close. Lessons will appear here
              as the agent works through its curriculum.
            </p>
          </div>
        </section>
      ) : (
        <section className="flex flex-col gap-5">
          {entries.map((e) => (
            <article key={e.id} className="ds-card p-5 sm:p-7">
              <div className="flex flex-wrap items-center gap-3">
                <span className="text-[13px] text-ink-3 font-[family-name:var(--font-mono)]">
                  {formatDate(e.learned_at)}
                </span>
                <span className="rounded-full border border-accent/40 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-accent font-[family-name:var(--font-mono)]">
                  {disciplineLabel(e.discipline ?? "")}
                </span>
                <span className="text-[11px] text-ink-3 font-[family-name:var(--font-mono)]">
                  {e.id}
                </span>
                {e.evidence_status && (
                  <span
                    className={`rounded-full border px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide font-[family-name:var(--font-mono)] ${
                      e.evidence_status === "validated"
                        ? "border-pos/40 bg-pos-soft text-pos"
                        : e.evidence_status === "underperforming"
                          ? "border-attn/40 bg-attn-soft text-attn"
                          : "border-line bg-card text-ink-3"
                    }`}
                    title={
                      e.outcome?.n
                        ? `${e.outcome.wins ?? 0}/${e.outcome.n} trades citing this lesson won`
                        : undefined
                    }
                  >
                    {e.evidence_status}
                    {e.outcome?.n ? ` ${e.outcome.wins ?? 0}/${e.outcome.n}` : ""}
                  </span>
                )}
                {(e.times_cited ?? 0) > 0 && (
                  <span className="text-[11px] text-ink-3 font-[family-name:var(--font-mono)]">
                    cited {e.times_cited}×
                  </span>
                )}
              </div>
              <h2 className="mt-3 text-xl font-semibold tracking-tight leading-snug">
                {clean(e.topic ?? "")}
              </h2>
              <p className="mt-3 text-sm leading-relaxed text-ink-2">{clean(e.summary ?? "")}</p>
              {(e.key_points?.length ?? 0) > 0 && (
                <ul className="mt-4 flex flex-col gap-1.5">
                  {e.key_points!.map((k, i) => (
                    <li key={i} className="flex gap-2 text-sm leading-relaxed text-ink-2">
                      <span className="text-accent" aria-hidden>
                        •
                      </span>
                      <span>{clean(k)}</span>
                    </li>
                  ))}
                </ul>
              )}
              {e.how_to_apply && (
                <div className="mt-4 rounded-lg border border-line bg-card px-4 py-3">
                  <p className="text-[11px] font-medium uppercase tracking-wide text-ink-3 font-[family-name:var(--font-mono)]">
                    How the agent applies this
                  </p>
                  <p className="mt-1.5 text-sm leading-relaxed text-ink-2">
                    {clean(e.how_to_apply)}
                  </p>
                </div>
              )}
              {(e.sources?.length ?? 0) > 0 && (
                <p className="mt-4 text-[12px] leading-relaxed text-ink-3 break-words">
                  Sources:{" "}
                  {e.sources!.map((s, i) => {
                    const isUrl = /^https?:\/\//.test(s);
                    return (
                      <span key={i}>
                        {i > 0 && " · "}
                        {isUrl ? (
                          <a
                            href={s}
                            target="_blank"
                            rel="noreferrer"
                            className="underline decoration-line hover:text-ink"
                          >
                            {hostnameOf(s)}
                          </a>
                        ) : (
                          clean(s)
                        )}
                      </span>
                    );
                  })}
                </p>
              )}
            </article>
          ))}
        </section>
      )}

      <footer className="mt-10 border-t border-line py-8">
        <p className="text-[13px] text-ink-3 leading-relaxed">
          East Equity Agent is a research experiment in agentic trading, currently running on
          simulated capital. Nothing here is financial advice.
        </p>
      </footer>
    </main>
  );
}
