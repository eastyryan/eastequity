import { NextResponse } from "next/server";

// Near-live quotes for the site. Two layers:
//   1. state/live_prices.json from the 'live-data' branch (feeders push every
//      ~5 min during market hours) — symbol list + fallback prices. The repo
//      is private, so a server-side token does the read; cached 60s.
//   2. When ALPACA_API_KEY/SECRET are set in the Vercel env, those symbols
//      are re-priced through Alpaca's batched snapshots (IEX real-time), so
//      the site shows live marks even when both feeders are asleep.
// Both layers fail soft; worst case the client widget simply hides.

export const dynamic = "force-dynamic";

const CACHE = { "Cache-Control": "s-maxage=60, stale-while-revalidate=240" };
const EMPTY = { as_of: null as string | null, prices: {} as Record<string, number> };
const SNAP_CHUNK = 100;

type Feed = {
  as_of: string | null;
  prices: Record<string, number>;
  source?: string;
};

async function fromGitHub(): Promise<Feed> {
  const token = process.env.GITHUB_TOKEN;
  if (!token) return { ...EMPTY };
  try {
    const r = await fetch(
      "https://api.github.com/repos/eastyryan/eastequity/contents/state/live_prices.json?ref=live-data",
      {
        headers: {
          Authorization: `Bearer ${token}`,
          Accept: "application/vnd.github.raw+json",
          "X-GitHub-Api-Version": "2022-11-28",
        },
        next: { revalidate: 60 },
      },
    );
    if (!r.ok) return { ...EMPTY };
    const blob = await r.json();
    return {
      as_of: typeof blob?.as_of === "string" ? blob.as_of : null,
      prices:
        blob?.prices && typeof blob.prices === "object" ? blob.prices : {},
      source: typeof blob?.source === "string" ? blob.source : undefined,
    };
  } catch {
    return { ...EMPTY };
  }
}

async function alpacaOverlay(symbols: string[]): Promise<Feed | null> {
  const key = process.env.ALPACA_API_KEY;
  const secret = process.env.ALPACA_SECRET_KEY;
  if (!key || !secret || symbols.length === 0) return null;
  const headers = { "APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret };
  const prices: Record<string, number> = {};
  try {
    for (let i = 0; i < symbols.length; i += SNAP_CHUNK) {
      const chunk = symbols.slice(i, i + SNAP_CHUNK).join(",");
      const r = await fetch(
        `https://data.alpaca.markets/v2/stocks/snapshots?symbols=${encodeURIComponent(chunk)}`,
        { headers, next: { revalidate: 55 } },
      );
      if (!r.ok) continue;
      const body = (await r.json()) as Record<string, unknown>;
      const snaps =
        body?.snapshots && typeof body.snapshots === "object"
          ? (body.snapshots as Record<string, unknown>)
          : body;
      for (const [sym, snap] of Object.entries(snaps ?? {})) {
        const s = snap as {
          latestTrade?: { p?: number };
          minuteBar?: { c?: number };
          dailyBar?: { c?: number };
          prevDailyBar?: { c?: number };
        };
        const px =
          Number(s?.latestTrade?.p) ||
          Number(s?.minuteBar?.c) ||
          Number(s?.dailyBar?.c) ||
          Number(s?.prevDailyBar?.c);
        if (px > 0) prices[sym.toUpperCase()] = Math.round(px * 100) / 100;
      }
    }
  } catch {
    return null;
  }
  if (Object.keys(prices).length === 0) return null;
  return {
    as_of: new Date().toISOString(),
    prices,
    source: "alpaca_iex_direct",
  };
}

// Symbols the client asks us to price (the page's held + watched tickers).
// Sanitized hard: this endpoint proxies Alpaca, so never forward arbitrary text.
function requestedSymbols(request: Request): string[] {
  const raw = new URL(request.url).searchParams.get("symbols") ?? "";
  const out: string[] = [];
  for (const part of raw.split(",")) {
    const s = part.trim().toUpperCase();
    if (/^[A-Z][A-Z.\-]{0,9}$/.test(s) && !out.includes(s)) out.push(s);
    if (out.length >= 200) break;
  }
  return out;
}

export async function GET(request: Request) {
  const gh = await fromGitHub();
  // Price the page's own tickers plus anything the feed carried, so the live
  // strip works from Alpaca alone even when every feeder branch is stale.
  const symbols = Array.from(
    new Set([...requestedSymbols(request), ...Object.keys(gh.prices)]),
  );
  const live = await alpacaOverlay(symbols);
  if (live) {
    // Alpaca is the fresher mark; keep any GitHub-only symbols underneath.
    return NextResponse.json(
      {
        as_of: live.as_of,
        prices: { ...gh.prices, ...live.prices },
        source: live.source,
      },
      { headers: CACHE },
    );
  }
  return NextResponse.json(gh, { headers: CACHE });
}
