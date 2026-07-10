import { NextRequest, NextResponse } from "next/server";

// Proxy for SEC EDGAR, used by cloud trading runs whose sandbox blocks sec.gov.
// Only sec.gov hosts are allowed; responses are edge-cached briefly.
const ALLOWED_HOSTS = new Set(["www.sec.gov", "data.sec.gov", "sec.gov"]);

export async function GET(req: NextRequest) {
  const target = req.nextUrl.searchParams.get("url");
  if (!target) return NextResponse.json({ error: "missing url param" }, { status: 400 });

  let parsed: URL;
  try {
    parsed = new URL(target);
  } catch {
    return NextResponse.json({ error: "invalid url" }, { status: 400 });
  }
  if (parsed.protocol !== "https:" || !ALLOWED_HOSTS.has(parsed.hostname)) {
    return NextResponse.json({ error: "host not allowed" }, { status: 403 });
  }

  const upstream = await fetch(parsed.toString(), {
    headers: { "User-Agent": "East Equity Agent easton.ryan@hws.edu" },
  });
  const body = await upstream.arrayBuffer();
  return new NextResponse(body, {
    status: upstream.status,
    headers: {
      "Content-Type": upstream.headers.get("Content-Type") ?? "application/octet-stream",
      "Cache-Control": "s-maxage=300, stale-while-revalidate=600",
    },
  });
}
