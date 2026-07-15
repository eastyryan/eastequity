import { NextResponse } from "next/server";

// Near-live quotes for holdings + watchlist, read from the dedicated
// 'live-data' branch that the price feeders (GitHub Action + local launchd
// job) force-push every ~5 min during market hours. The repo is private, so
// a server-side token does the read; the upstream fetch is cached 60s and
// the response carries CDN caching, so GitHub API usage stays trivial.
// Fails soft to an empty feed so the client widget simply hides.

export const dynamic = "force-dynamic";

const CACHE = { "Cache-Control": "s-maxage=60, stale-while-revalidate=240" };
const EMPTY = { as_of: null, prices: {} };

export async function GET() {
  const token = process.env.GITHUB_TOKEN;
  if (!token) return NextResponse.json(EMPTY, { headers: CACHE });
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
    if (!r.ok) return NextResponse.json(EMPTY, { headers: CACHE });
    const blob = await r.json();
    return NextResponse.json(
      {
        as_of: typeof blob?.as_of === "string" ? blob.as_of : null,
        prices:
          blob?.prices && typeof blob.prices === "object" ? blob.prices : {},
      },
      { headers: CACHE },
    );
  } catch {
    return NextResponse.json(EMPTY, { headers: CACHE });
  }
}
