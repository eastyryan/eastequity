import { NextRequest, NextResponse } from "next/server";

// Proxy for FRED series observations, used by cloud trading runs whose sandbox
// blocks stlouisfed.org. The API key lives server-side in Vercel env config.
export async function GET(req: NextRequest) {
  const seriesId = req.nextUrl.searchParams.get("series_id");
  const start = req.nextUrl.searchParams.get("observation_start") ?? "";
  if (!seriesId || !/^[A-Z0-9]{1,30}$/.test(seriesId)) {
    return NextResponse.json({ error: "missing or invalid series_id" }, { status: 400 });
  }
  const key = process.env.FRED_API_KEY;
  if (!key) return NextResponse.json({ error: "FRED_API_KEY not configured" }, { status: 500 });

  const url = new URL("https://api.stlouisfed.org/fred/series/observations");
  url.searchParams.set("series_id", seriesId);
  url.searchParams.set("api_key", key);
  url.searchParams.set("file_type", "json");
  if (start) url.searchParams.set("observation_start", start);

  const upstream = await fetch(url.toString());
  const body = await upstream.text();
  return new NextResponse(body, {
    status: upstream.status,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": "s-maxage=900, stale-while-revalidate=1800",
    },
  });
}
