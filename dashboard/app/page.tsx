import fs from "node:fs";
import path from "node:path";
import Arena from "@/components/Arena";
import type { LearningJournal } from "@/components/ArenaStudyHall";
import type { HistoryPoint, Latest } from "@/lib/types";

// Read at build time from the JSON the agent commits. Trading runs commit
// latest.json without a [vercel skip] marker, so every run redeploys this page
// with fresh data — that commit-triggered rebuild IS the update mechanism.
function readJson<T>(file: string): T {
  return JSON.parse(fs.readFileSync(path.join(process.cwd(), "data", file), "utf-8")) as T;
}

// The study journal is optional — it does not exist until the first study session
// runs — so a missing or corrupt file must yield an empty state, never a build error.
function readJsonSafe<T>(file: string): T | null {
  try {
    const p = path.join(process.cwd(), "data", file);
    if (!fs.existsSync(p)) return null;
    return JSON.parse(fs.readFileSync(p, "utf-8")) as T;
  } catch {
    return null;
  }
}

export default function Home() {
  const latest = readJson<Latest>("latest.json");
  const history = readJson<HistoryPoint[]>("equity_history.json");
  const journal = readJsonSafe<NonNullable<LearningJournal>>("learning_journal.json");

  return <Arena latest={latest} history={history} journal={journal} />;
}
