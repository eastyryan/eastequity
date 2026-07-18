"use client";

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
 * Content is one researched lesson per weekday. Lessons are graded by the trades
 * that cite them, so the evidence status shown here is earned, not asserted — and
 * under three linked trades it deliberately shows nothing rather than a verdict.
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

  return (
    <div>
      <p
        style={{
          fontSize: 12.5,
          color: "var(--ee-bodytx)",
          lineHeight: 1.7,
          maxWidth: "62ch",
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
          {entries.map((e) => (
            <article
              key={e.id}
              style={{ borderTop: "1px solid var(--ee-hair08)", padding: "24px 0 28px" }}
            >
              <div
                style={{
                  display: "flex",
                  gap: 16,
                  flexWrap: "wrap",
                  alignItems: "baseline",
                  marginBottom: 10,
                }}
              >
                <span style={{ fontSize: 12, color: "var(--ee-accent)", flexShrink: 0 }}>
                  {formatDate(e.learned_at)}
                </span>
                <span style={{ fontSize: 11, letterSpacing: "0.16em", color: "var(--ee-muted)" }}>
                  {label(e.discipline).toUpperCase()}
                </span>
                {e.evidence_status && (
                  <span
                    style={{ fontSize: 11, letterSpacing: "0.14em", color: statusColor(e.evidence_status) }}
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
              </div>

              <h4 className="ee-serif" style={{ fontSize: 22, lineHeight: 1.25, margin: "0 0 10px" }}>
                {clean(e.topic)}
              </h4>

              <p
                style={{
                  fontSize: 12.5,
                  color: "var(--ee-bodytx)",
                  lineHeight: 1.7,
                  margin: 0,
                  maxWidth: "68ch",
                  textWrap: "pretty",
                }}
              >
                {clean(e.summary)}
              </p>

              {(e.key_points?.length ?? 0) > 0 && (
                <ul style={{ listStyle: "none", padding: 0, margin: "14px 0 0" }}>
                  {e.key_points!.map((k, i) => (
                    <li
                      key={i}
                      style={{
                        display: "flex",
                        gap: 10,
                        fontSize: 12.5,
                        color: "var(--ee-bodytx)",
                        lineHeight: 1.65,
                        padding: "3px 0",
                        maxWidth: "68ch",
                      }}
                    >
                      <span style={{ color: "var(--ee-accent)", flexShrink: 0 }} aria-hidden>
                        —
                      </span>
                      <span>{clean(k)}</span>
                    </li>
                  ))}
                </ul>
              )}

              {e.how_to_apply && (
                <div style={{ marginTop: 16, paddingLeft: 16, borderLeft: "2px solid var(--ee-hair14)" }}>
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
                      maxWidth: "64ch",
                    }}
                  >
                    {clean(e.how_to_apply)}
                  </p>
                </div>
              )}

              {(e.sources?.length ?? 0) > 0 && (
                <div style={{ display: "flex", gap: 14, flexWrap: "wrap", marginTop: 14 }}>
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
            </article>
          ))}
        </div>
      )}
    </div>
  );
}
