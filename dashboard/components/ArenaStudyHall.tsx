"use client";

import { useState } from "react";

/**
 * Study hall, rendered in the Arena's visual language.
 *
 * Replaces the standalone /learning page. That page was built on the legacy card
 * system — `ds-card` panels, pill badges, Tailwind utilities, --ink/--line tokens —
 * which is a different design from the Arena's editorial grid of hairline rules,
 * serif display type and --ee-* tokens. Rather than transplant the cards, the
 * content is rebuilt on the Arena's own primitives so the section reads as part of
 * the page instead of an embedded sub-site.
 *
 * Lessons are COLLAPSED to their title by default. A study entry runs several
 * hundred words of dense research prose; stacked open they bury the rest of the
 * page and make the list impossible to scan. The title is the index — open the one
 * you want. Expansion reuses the same disclosure pattern as the watchlist rows
 * above (button + chevron, .ee-wrow hover) so the interaction is already familiar.
 */

export type LearningEntry = {
  id: string;
  discipline?: string;
  topic?: string;
  summary?: string;
  key_points?: string[];
  how_to_apply?: string;
  sources?: string[];
  learned_at?: string;
  evidence_status?: "validated" | "mixed" | "underperforming" | null;
  outcome?: { n?: number; wins?: number } | null;
  times_cited?: number;
};

export type LearningJournal = {
  updated_at?: string;
  note?: string;
  discipline_counts?: Record<string, number>;
  entries?: LearningEntry[];
} | null;

const clean = (s?: string | null) =>
  (s || "")
    .replace(/—/g, "-")
    .replace(/–/g, "-")
    .replace(/\*\*/g, "")
    .replace(/[`*]/g, "");

const label = (d?: string) => (d || "").replace(/_/g, " ");

function formatDate(s?: string): string {
  if (!s) return "";
  const d = new Date(s);
  if (isNaN(d.getTime())) return s.slice(0, 10);
  return d.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    timeZone: "America/New_York",
  });
}

function hostnameOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url.slice(0, 48);
  }
}

const statusColor = (s?: string | null) =>
  s === "validated" ? "var(--ee-up)" : s === "underperforming" ? "var(--ee-down)" : "var(--ee-muted)";

export default function ArenaStudyHall({ journal }: { journal: LearningJournal }) {
  const entries = journal?.entries ?? [];
  const counts = Object.entries(journal?.discipline_counts ?? {}).filter(([, n]) => n > 0);
  const [open, setOpen] = useState<Record<string, boolean>>({});

  return (
    <div>
      <p
        style={{
          fontSize: 12.5,
          color: "var(--ee-bodytx)",
          lineHeight: 1.7,
          margin: "0 0 26px",
          textWrap: "pretty",
        }}
      >
        Every weekday after the close, the agent researches one topic from its own curriculum and
        writes a durable lesson. Those lessons are injected into every future trading run, and each
        one is graded by whether the trades citing it actually worked — so the craft compounds, and
        the record of what it learned stays auditable.
      </p>

      {counts.length > 0 && (
        <div style={{ display: "flex", gap: 18, flexWrap: "wrap", marginBottom: 30 }}>
          {counts.map(([d, n]) => (
            <span key={d} style={{ fontSize: 11, letterSpacing: "0.14em", color: "var(--ee-muted)" }}>
              {label(d).toUpperCase()} · {n}
            </span>
          ))}
        </div>
      )}

      {entries.length === 0 ? (
        <div
          style={{
            borderTop: "1px solid var(--ee-hair08)",
            padding: "26px 2px",
            fontSize: 12.5,
            color: "var(--ee-bodytx)",
            lineHeight: 1.65,
          }}
        >
          No lessons yet. The first study session runs after the next market close.
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column" }}>
          {entries.map((e) => {
            const isOpen = !!open[e.id];
            return (
              <div key={e.id} style={{ borderTop: "1px solid var(--ee-hair08)" }}>
                <button
                  className="ee-wrow"
                  onClick={() => setOpen((s) => ({ ...s, [e.id]: !s[e.id] }))}
                  aria-expanded={isOpen}
                  style={{
                    display: "flex",
                    alignItems: "flex-start",
                    gap: 18,
                    padding: "18px 2px",
                    cursor: "pointer",
                    width: "100%",
                    background: "none",
                    border: "none",
                    color: "inherit",
                    font: "inherit",
                    textAlign: "left",
                  }}
                >
                  <span style={{ flex: 1, minWidth: 0 }}>
                    <span
                      style={{
                        display: "flex",
                        gap: 16,
                        flexWrap: "wrap",
                        alignItems: "baseline",
                        marginBottom: 8,
                      }}
                    >
                      <span style={{ fontSize: 12, color: "var(--ee-accent)" }}>
                        {formatDate(e.learned_at)}
                      </span>
                      <span
                        style={{ fontSize: 11, letterSpacing: "0.16em", color: "var(--ee-muted)" }}
                      >
                        {label(e.discipline).toUpperCase()}
                      </span>
                      {e.evidence_status && (
                        <span
                          style={{
                            fontSize: 11,
                            letterSpacing: "0.14em",
                            color: statusColor(e.evidence_status),
                          }}
                          title={
                            e.outcome?.n
                              ? `${e.outcome.wins ?? 0} of ${e.outcome.n} trades citing this lesson won`
                              : undefined
                          }
                        >
                          {e.evidence_status.toUpperCase()}
                          {e.outcome?.n ? ` ${e.outcome.wins ?? 0}/${e.outcome.n}` : ""}
                        </span>
                      )}
                      {(e.times_cited ?? 0) > 0 && (
                        <span style={{ fontSize: 11, color: "var(--ee-faint)" }}>
                          cited {e.times_cited}×
                        </span>
                      )}
                      <span style={{ fontSize: 11, color: "var(--ee-faint)" }}>{e.id}</span>
                    </span>
                    <span
                      className="ee-serif"
                      style={{ display: "block", fontSize: 22, lineHeight: 1.3, textWrap: "pretty" }}
                    >
                      {clean(e.topic)}
                    </span>
                  </span>
                  <span
                    style={{ color: "var(--ee-muted)", fontSize: 12, flexShrink: 0, marginTop: 4 }}
                    aria-hidden
                  >
                    {isOpen ? "▲" : "▼"}
                  </span>
                </button>

                {isOpen && (
                  <div style={{ padding: "2px 2px 28px" }}>
                    <p
                      style={{
                        fontSize: 12.5,
                        color: "var(--ee-bodytx)",
                        lineHeight: 1.7,
                        margin: 0,
                        textWrap: "pretty",
                      }}
                    >
                      {clean(e.summary)}
                    </p>

                    {(e.key_points?.length ?? 0) > 0 && (
                      <ul style={{ listStyle: "none", padding: 0, margin: "16px 0 0" }}>
                        {e.key_points!.map((k, i) => (
                          <li
                            key={i}
                            style={{
                              display: "flex",
                              gap: 10,
                              fontSize: 12.5,
                              color: "var(--ee-bodytx)",
                              lineHeight: 1.7,
                              padding: "4px 0",
                            }}
                          >
                            <span style={{ color: "var(--ee-accent)", flexShrink: 0 }} aria-hidden>
                              —
                            </span>
                            <span style={{ textWrap: "pretty" }}>{clean(k)}</span>
                          </li>
                        ))}
                      </ul>
                    )}

                    {e.how_to_apply && (
                      <div
                        style={{
                          marginTop: 18,
                          paddingLeft: 16,
                          borderLeft: "2px solid var(--ee-hair14)",
                        }}
                      >
                        <div
                          style={{
                            fontSize: 10.5,
                            letterSpacing: "0.18em",
                            color: "var(--ee-muted)",
                            marginBottom: 6,
                          }}
                        >
                          HOW THE AGENT APPLIES THIS
                        </div>
                        <p
                          style={{
                            fontSize: 12.5,
                            color: "var(--ee-thought)",
                            lineHeight: 1.7,
                            margin: 0,
                            textWrap: "pretty",
                          }}
                        >
                          {clean(e.how_to_apply)}
                        </p>
                      </div>
                    )}

                    {(e.sources?.length ?? 0) > 0 && (
                      <div style={{ display: "flex", gap: 14, flexWrap: "wrap", marginTop: 16 }}>
                        {e.sources!.slice(0, 6).map((s, i) => (
                          <a
                            key={i}
                            href={s}
                            target="_blank"
                            rel="noreferrer"
                            style={{ fontSize: 11.5, color: "var(--ee-accent)" }}
                          >
                            {hostnameOf(s)} ↗
                          </a>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
