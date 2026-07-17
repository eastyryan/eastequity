import { NextResponse } from "next/server";

// Real-time VIX + 10-year Treasury yield for the dashboard macro tape.
// FRED (the trading engine's source of record) publishes these daily series
// ~1-2 business days late, so on a weekday morning the tape reads Wednesday's
// values. This overlay shows today's intraday marks on the DASHBOARD ONLY —
// the brain still reasons on FRED so decisions stay reproducible from an
// official source. Fails soft: null fields simply hide the overlay and the
// tape falls back to the FRED value.

export const dynamic = "force-dynamic";

const CACHE = { "Cache-Control": "s-maxage=60, stale-while-revalidate=240" };
const UA = "Mozilla/5.0 (compatible; EastEquityDashboard/1.0)";

async function yahoo(symbol: string): Promise<number | null> {
  try {
    const r = await fetch(
      `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(
        symbol,
      )}?interval=1d&range=1d`,
      { headers: { "User-Agent": UA, Accept: "application/json" }, next: { revalidate: 55 } },
    );
    if (!r.ok) return null;
    const j = (await r.json()) as {
      chart?: { result?: Array<{ meta?: { regularMarketPrice?: number } }> };
    };
    const px = j?.chart?.result?.[0]?.meta?.regularMarketPrice;
    return typeof px === "number" && px > 0 ? px : null;
  } catch {
    return null;
  }
}

async function stooq(symbol: string): Promise<number | null> {
  try {
    const r = await fetch(
      `https://stooq.com/q/l/?s=${encodeURIComponent(symbol)}&f=sd2t2ohlcv&e=csv`,
      { headers: { "User-Agent": UA }, next: { revalidate: 55 } },
    );
    if (!r.ok) return null;
    const line = (await r.text()).trim().split("\n").pop() ?? "";
    // Header: Symbol,Date,Time,Open,High,Low,Close,Volume
    const close = Number(line.split(",")[6]);
    return Number.isFinite(close) && close > 0 ? close : null;
  } catch {
    return null;
  }
}

// ^TNX is quoted as the yield (4.55) on some feeds and yield x10 (45.5) on
// others; a 10Y yield is never ~20%, so anything above that is the x10 form.
function normYield(v: number | null): number | null {
  if (v == null) return null;
  const y = v > 20 ? v / 10 : v;
  return Math.round(y * 100) / 100;
}

export async function GET() {
  const [vixY, tnxY] = await Promise.all([yahoo("^VIX"), yahoo("^TNX")]);
  let vix = vixY;
  let ten = normYield(tnxY);
  if (vix == null) vix = await stooq("^vix");
  if (ten == null) ten = normYield(await stooq("10usy.b"));
  if (vix != null) vix = Math.round(vix * 100) / 100;
  const as_of = vix != null || ten != null ? new Date().toISOString() : null;
  return NextResponse.json(
    { as_of, vix, ten_year_yield: ten, source: "yahoo_stooq" },
    { headers: CACHE },
  );
}
