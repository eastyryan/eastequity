// Timestamp badges, market-timezone. "2026-07-16 · 10:22 PM ET".
// A bare calendar date (YYYY-MM-DD) is returned as-is: it carries no time, and
// constructing a Date from it would let timezone math shift it a day.
export function formatEtStamp(iso?: string | null): string {
  if (!iso) return "";
  if (/^\d{4}-\d{2}-\d{2}$/.test(iso)) return iso;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const date = d.toLocaleDateString("en-CA", { timeZone: "America/New_York" });
  const time = d.toLocaleTimeString("en-US", {
    hour: "numeric",
    minute: "2-digit",
    timeZone: "America/New_York",
  });
  return `${date} · ${time} ET`;
}
