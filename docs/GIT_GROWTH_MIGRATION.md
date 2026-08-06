# Moving the relay bundle + chart PNGs off main: verified plan, NOT implemented

**Status: investigation only (2026-08-05 audit).** The migration below is safe on
paper and blocked by exactly one thing: the claude.ai cloud trading routine
clones `main`, its prompt/config cannot be inspected from this repository, and
the chart PNGs exist specifically so that routine can Read them
(tools/price_chart.py:5-7: "charts are committed by the data relay so cloud runs
can Read them too"; CLAUDE.md:493 instructs the brain to Read
`data/charts/<TICKER>.png`). Migrating the files out from under an unauditable
consumer would be the price-staleness incident again, but self-inflicted. Do not
implement until the confirmation checklist below is done.

## The problem, measured (2026-08-05)

| What | Measurement |
|---|---|
| `.git` pack, ~1 month of operation | **210 MB** |
| `data/charts/*.png` history | **2,547 blobs, 153.6 MB compressed** (`git rev-list --objects` + `cat-file --batch-check='%(objectsize:disk)'`) |
| `data/cloud_context.json` history | **307 blobs, 12.4 MB compressed** |
| Together | **166 MB ≈ 79% of the pack** |
| Current working-tree size of the same files | charts 1.9 MB (26 PNGs) + bundle 1.6 MB ≈ **3.5 MB** |
| Re-commit rate | bundle refreshed up to ~14x/weekday; each chart PNG re-committed ~50–110x (2,547 blobs / 26 live charts ≈ 98 avg) |

PNGs never delta-compress, so every refresh adds ~full file size to the pack
forever. Growth rate ≈ **5–6 MB/day**, almost all of it charts.

## Verified writer map

| Writer | Evidence | Notes |
|---|---|---|
| gather-data.yml gather job | `cp "$CTX" data/cloud_context.json` (gather-data.yml:212); `git add data/cloud_context.json data/charts dashboard/data/position_charts.json` (gather-data.yml:228) | The dominant writer: every scheduled/push-triggered gather |
| tools/price_chart.py `render_charts()` | writes `data/charts/<TICKER>.png` | produces the files; the gather commits them |
| runlib/publish.py:189-198 | writes `dashboard/data/position_charts.json` (built by runlib/analytics.py:1070 `build_position_charts`) | small JSON, rides in the same commit |
| scripts/relay_data.sh:60-62 | `cp ... data/cloud_context.json`; `git add data/cloud_context.json data/charts ...` | the local Mac relay — dead since ~Jul 21 (x-post.yml header documents the launchd stand-down) but still a writer if revived |

## Verified reader map

### data/cloud_context.json

| Reader | Evidence | What breaks if the file moves |
|---|---|---|
| **claude.ai cloud routine** | clones main; orchestrator's `--gather-only` fallback (below) plus whatever its uninspectable prompt does | **UNKNOWN — the blocker** |
| orchestrator.py:446 | relay fallback when live feeds are degraded | brain falls back to nothing → `data_quality_empty`, no trading |
| scripts/execute_order_intents.py:67 | reads `portfolio_risk` for book revalidation of intents | fail-soft (`try/except`) but loses a risk check |
| scripts/stop_watch.py:165 | first ATR source for the chandelier trail | falls back to newest local `state/context_2*.json`, absent on a fresh runner → trail cannot ratchet |
| runlib/analytics.py:324 | `bundle_age_hours` for build_health | heartbeat loses the staleness alarm (reads as unknown, not unhealthy) |
| runlib/capabilities.py:90 | freshness-probe candidate | degraded probe |
| gather-data.yml:104 (decide job) | bundle-age gate for push-triggered gathers | age reads 9999 → every trade-path push escalates to a full gather |
| tests/test_capabilities_freshness.py | fixture paths | test update needed |

### data/charts/*.png

| Reader | Evidence | What breaks |
|---|---|---|
| **claude.ai cloud brain** | CLAUDE.md:493 — "Candlestick charts at data/charts/<TICKER>.png … USE THE READ TOOL"; tools/price_chart.py:5-7 says committing them is FOR cloud runs | **the brain goes chart-blind — this is why the file exists** |
| tools/price_chart.py | overwrite-only-on-success logic depends on last-good committed charts being present in the checkout (docstring: the 2026-07-14 empty-folder bug) | cloud fetch failures would wipe instead of keep |

`dashboard/data/position_charts.json` is read by the dashboard build — it is
small and NOT part of this migration.

## How the live-data orphan pattern works today (the model to copy)

- **Writer:** live-prices.yml — `git checkout --orphan live-data`, stage one
  file + a `vercel.json` with `deploymentEnabled:false` (so Vercel never builds
  the branch), commit, `git push -f`. One commit, no history, constant size.
- **Readers:** tools/live_prices.py:200-202 (`git fetch --depth=1 origin
  live-data` + `git show origin/live-data:state/live_prices.json`);
  stop-watch.yml:144-145 fallback; dashboard/app/api/live-prices/route.ts:29
  (contents API with `?ref=live-data`); tools/trigger_watch.py per the
  live-prices.yml header.

This is exactly the shape the bundle + charts need: refreshed ~14x/day, only the
newest copy ever matters, no reader wants history.

## Migration steps (in order — do not start before the confirmation section)

1. **Dual-write first.** In gather-data.yml, after the existing main commit,
   also force-push `data/cloud_context.json` + `data/charts/` (+ the vercel.json
   build guard) to a new orphan branch (e.g. `relay-data`), copying the
   live-prices.yml recipe. Main behavior unchanged; the branch proves itself.
2. **Teach the in-repo readers the branch with main as fallback:** a small
   helper (fetch `--depth=1`, `git show origin/relay-data:<path>`, fall back to
   the working-tree file) used by stop_watch, execute_order_intents,
   capabilities, analytics. NOTE: scripts/stop_watch.py and
   scripts/execute_order_intents.py are owned by other agents — coordinate,
   don't edit unilaterally.
3. **Fix the decide-job age gate** (gather-data.yml:104) to read the branch
   (`git fetch --depth=1` + `git show`), or every push would escalate to a
   gather once the main copy goes stale.
4. **Update the cloud routine's prompt** (outside this repo) to fetch the
   branch into its checkout before the run: roughly `git fetch --depth=1 origin
   relay-data && git checkout origin/relay-data -- data/cloud_context.json
   data/charts`. The sandbox reaches only GitHub, and this is a GitHub fetch,
   so it *should* be feasible — confirm, don't assume.
5. **Watch one full week of runs dual-written** (all 7 slots + Sunday), then
   drop `data/cloud_context.json data/charts` from the main-side `git add`
   (gather-data.yml:228) and from scripts/relay_data.sh:62.
6. **Never rewrite history.** The 166 MB already in the pack stays; the win is
   that growth stops. Shrinking the existing pack means a history rewrite +
   force-push of main — a separate, deliberate decision that would break every
   clone, the cloud routine's checkout, and the fetch-depth reasoning in four
   workflows. Not part of this plan.

## What MUST be confirmed about the cloud routine first

The routine is a claude.ai scheduled agent; nothing in this repo can show its
prompt. Before step 4:

1. Can its sandbox run `git fetch` for a non-default branch of this repo?
   (It clones main — is the clone single-branch? shallow? read-only token?)
2. Does its prompt reference `data/cloud_context.json` or `data/charts/...` as
   literal paths it expects present after clone (CLAUDE.md:493 suggests yes for
   charts)? If so the checkout in step 4 must run before the brain reads.
3. Does it ever COMMIT either path back to main (it pushes run-markers, lease,
   dashboard updates on every run)? A cloud-side writer would reintroduce the
   growth from the other side.
4. Does the watchdog routine (which re-runs missed slots) share the same clone
   recipe, or does it need the same prompt change separately?

Test cheaply: one manual `workflow_dispatch` of the dual-write, then a single
supervised cloud run whose journal output confirms it read the branch copy
(bundle `run_date` visible in the run summary must match the branch, not main).

## Expected savings

- Main's pack growth drops from ~5–6 MB/day to ~KB/day (journal + state deltas).
- The orphan branch stays a constant ~3.5 MB (single force-pushed commit).
- Every workflow checkout and every future clone stops paying for dead
  bundle/chart revisions; the fetch-depth: 300 fix already caps today's cost,
  this removes the growth underneath it.
