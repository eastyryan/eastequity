import React from "react";

export type BrainMemoryData = {
  markdown: string;
  updated_at: string;
} | null;

// Minimal renderer for the BRAIN_MEMORY.md subset: #/##/### headings, **bold**,
// numbered and bullet lists, paragraphs, --- rules. No dependency needed.
function renderInline(text: string, keyPrefix: string): React.ReactNode[] {
  const parts = text.split(/(\*\*[^*]+\*\*)/g);
  return parts.map((p, i) => {
    if (p.startsWith("**") && p.endsWith("**") && p.length > 4) {
      return (
        <strong key={`${keyPrefix}-${i}`} style={{ color: "var(--ee-text)" }}>
          {p.slice(2, -2)}
        </strong>
      );
    }
    return <React.Fragment key={`${keyPrefix}-${i}`}>{p}</React.Fragment>;
  });
}

function renderMarkdown(md: string): React.ReactNode[] {
  const lines = md.split("\n");
  const out: React.ReactNode[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;
  let key = 0;

  const flushList = () => {
    if (!list) return;
    const items = list.items;
    const ordered = list.ordered;
    list = null;
    out.push(
      ordered ? (
        <ol key={key++} style={{ paddingLeft: 22, margin: "6px 0 14px", lineHeight: 1.65 }}>
          {items.map((it, i) => (
            <li key={i} style={{ marginBottom: 4 }}>{renderInline(it, `li-${key}-${i}`)}</li>
          ))}
        </ol>
      ) : (
        <ul key={key++} style={{ paddingLeft: 22, margin: "6px 0 14px", lineHeight: 1.65 }}>
          {items.map((it, i) => (
            <li key={i} style={{ marginBottom: 4 }}>{renderInline(it, `li-${key}-${i}`)}</li>
          ))}
        </ul>
      )
    );
  };

  for (const raw of lines) {
    const line = raw.trimEnd();
    const t = line.trim();
    if (t === "---") {
      flushList();
      out.push(<hr key={key++} style={{ border: "none", borderTop: "1px solid var(--ee-line)", margin: "22px 0" }} />);
      continue;
    }
    const h3 = t.match(/^###\s+(.*)/);
    const h2 = t.match(/^##\s+(.*)/);
    const h1 = t.match(/^#\s+(.*)/);
    if (h1 || h2 || h3) {
      flushList();
      const text = (h1?.[1] ?? h2?.[1] ?? h3?.[1] ?? "");
      const lvl = h1 ? 1 : h2 ? 2 : 3;
      out.push(
        lvl === 1 ? (
          <h2 key={key++} className="ee-serif" style={{ fontSize: 30, margin: "26px 0 12px" }}>
            {renderInline(text, `h${key}`)}
          </h2>
        ) : (
          <h4 key={key++} style={{ fontSize: lvl === 2 ? 17 : 15, fontWeight: 700, margin: "20px 0 8px", color: "var(--ee-text)" }}>
            {renderInline(text, `h${key}`)}
          </h4>
        )
      );
      continue;
    }
    const ol = t.match(/^\d+\.\s+(.*)/);
    const ul = t.match(/^[-*]\s+(.*)/);
    if (ol || ul) {
      const ordered = !!ol;
      const item = (ol?.[1] ?? ul?.[1] ?? "");
      if (!list || list.ordered !== ordered) {
        flushList();
        list = { ordered, items: [] };
      }
      list.items.push(item);
      continue;
    }
    if (t === "") {
      flushList();
      continue;
    }
    flushList();
    out.push(
      <p key={key++} style={{ margin: "0 0 10px", lineHeight: 1.7, color: "var(--ee-muted)" }}>
        {renderInline(t, `p${key}`)}
      </p>
    );
  }
  flushList();
  return out;
}

export default function BrainMemory({ data }: { data: BrainMemoryData }) {
  if (!data || !data.markdown) {
    return (
      <p style={{ color: "var(--ee-muted)", lineHeight: 1.7 }}>
        The brain has not published its memory yet. It lands here after the next run.
      </p>
    );
  }
  const updated = (() => {
    try {
      return new Date(data.updated_at).toLocaleString("en-US", { timeZone: "America/New_York" });
    } catch {
      return data.updated_at;
    }
  })();
  return (
    <div>
      <div style={{ fontSize: 11, letterSpacing: "0.2em", color: "var(--ee-muted)", marginBottom: 18 }}>
        LAST UPDATED {updated} ET
      </div>
      <div style={{ maxWidth: 860 }}>{renderMarkdown(data.markdown)}</div>
    </div>
  );
}
