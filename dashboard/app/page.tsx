import fs from "node:fs";
import path from "node:path";
import Arena from "@/components/Arena";
import type { HistoryPoint, Latest } from "@/lib/types";

// Read at build time from the JSON the agent commits. Trading runs commit
// latest.json without a [vercel skip] marker, so every run redeploys this page
// with fresh data — that commit-triggered rebuild IS the update mechanism.
function readJson<T>(file: string): T {
  return JSON.parse(fs.readFileSync(path.join(process.cwd(), "data", file), "utf-8")) as T;
}

export default function Home() {
  const latest = readJson<Latest>("latest.json");
  const history = readJson<HistoryPoint[]>("equity_history.json");

  return <Arena latest={latest} history={history} />;
}
