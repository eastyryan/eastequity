import fs from "node:fs";
import path from "node:path";
import Arena from "@/components/Arena";
import type { LearningJournal } from "@/components/ArenaStudyHall";
import type { HistoryPoint, Latest } from "@/lib/types";

// Read at build time from the JSON the agent commits. Since 2026-09-21 every
// trading-run and executor commit carries [vercel skip], so this page is NOT
// rebuilt per run: the .github/workflows/dashboard-refresh.yml cron pushes one
// empty [vercel build] commit a day and that single build publishes whatever
// data has accumulated in the repo. The page can therefore be up to a day
// behind the ledger by design — that is the deployment-storage tradeoff, not a bug.
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
