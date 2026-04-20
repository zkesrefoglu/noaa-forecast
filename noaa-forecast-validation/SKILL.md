---
name: noaa-forecast-validation
description: Project context and operating runbook for Ziya's NOAA forecast validation pipeline at ZKE Solutions. Use this skill whenever the user mentions NOAA forecast, vendor forecast, ASOS, Iowa Mesonet, zones (DCA/ABE/PHL/PIT/CLE/LCK/EWR), score_daily, noaa_forecast.py, capture_vendor, C_REGION, ops-query-in-out_hourly_temp, the noaa-forecast repo, or anything about comparing NOAA vs vendor weather forecasts for electricity load scheduling. Also trigger on shorthand like "the scoring pipeline," "the forecast repo," "the MAE report," or when the user references files under C:\Users\Ziya\Documents\GitHub\noaa-forecast. This skill is the single source of truth for architecture, zone mapping, runbooks, and known gotchas; consult it before touching any script, triggering any workflow, or drafting any management-facing write-up about forecast accuracy.
---

# NOAA Forecast Validation

## What this project exists to answer

**Can NOAA's free public forecasts replace the paid vendor feed that currently drives the Excel load-scheduling model at Ziya's employer?**

The answer is measured as mean absolute error (MAE) in degrees Fahrenheit, per electricity zone, per forecast leadtime bucket, over enough days to be statistically meaningful. The deliverable is a weekly management-facing summary showing NOAA vs vendor per zone.

The repo: `C:\Users\Ziya\Documents\GitHub\noaa-forecast` (GitHub: `zkesrefoglu/noaa-forecast`).

## How the data flow works

Three independent streams land in the repo, then get joined at scoring time.

```
+-----------------------+     +------------------+     +------------------+
| NOAA DWML hourly XML  |     | vendor CSV daily |     | ASOS hourly obs  |
| (forecasts)           |     | (forecasts)      |     | (truth)          |
+----------+------------+     +--------+---------+     +--------+---------+
           |                           |                        |
           | hourly :07 UTC            | 10 AM ET scheduled     | daily 08:00 UTC
           | GitHub Actions            | task on work machine   | GitHub Actions
           v                           v                        v
  data/<ZONE>/<DATE>/             data/vendor/            data/asos/
   <snapshot>.parquet              <DATE>.csv              <DATE>.parquet
                      \           |           /
                       \          |          /
                        v         v         v
                       score_daily.py (daily 09:00 UTC)
                                  |
                                  v
                         data/scores/
                          hourly_detail_<DATE>.parquet
                          daily_by_bucket.parquet   (accumulating)
```

**Key invariant:** the scorer joins on `(valid_ts_utc, zone)`. Everything upstream must agree on that join key. NOAA is parsed to UTC from the DWML file's time-layout blocks. ASOS is UTC from the start. Vendor is local `(D_TEMP, H_TEMP)` in America/New_York and gets converted at scoring time.

## The seven zones

Only seven zones matter for electricity scheduling — others in the vendor file are natural gas and ignored.

| zone | c_region | ICAO | role |
|------|----------|------|------|
| DCA  | 1 | KDCA | Washington National |
| ABE  | 3 | KABE | Allentown/Lehigh Valley (PPL territory) |
| PHL  | 4 | KPHL | Philadelphia (PECO territory) |
| PIT  | 5 | KPIT | Pittsburgh |
| CLE  | 8 | KCLE | Cleveland (FirstEnergy Ohio) |
| LCK  | 9 | KLCK | Columbus Rickenbacker |
| EWR  | 11 | KEWR | Newark (PSE&G / NJ) |

**Excluded on purpose:** `c_region` 2 (BWI, not used), 6 (TOL, natural gas), 7 (CAK, natural gas), 10 (ERIE, natural gas). Don't "add them back" without talking to Ziya — they exist in the vendor file but aren't part of the electricity question.

Coordinates are airport lat/lon (FAA). See `references/zones.md` for full details including why airport coordinates and not centroids.

## Repository layout

```
noaa-forecast/
  noaa_forecast.py           # NOAA DWML puller (hourly)
  asos_truth.py              # Iowa Mesonet ASOS puller (daily)
  score_daily.py             # Join + MAE scorer (daily)
  query.py                   # Ad-hoc DuckDB helper
  build_dashboard.py         # HTML dashboard generator
  zones.csv                  # Zone config (the seven above)
  requirements.txt
  scripts/
    capture_vendor.ps1       # Work-machine PowerShell script (scheduled task)
  .github/workflows/
    noaa.yml                 # hourly :07 NOAA pull
    asos-truth.yml           # daily 08:00 UTC ASOS pull
    score-daily.yml          # daily 09:00 UTC scoring
  data/
    <ZONE>/<YYYY-MM-DD>/<snapshot_ts>.parquet   # NOAA snapshots
    asos/<YYYY-MM-DD>.parquet                   # ASOS truth
    vendor/<YYYY-MM-DD>.csv                     # vendor capture (once wired)
    scores/
      hourly_detail_<YYYY-MM-DD>.parquet        # per-date forecast+truth detail
      daily_by_bucket.parquet                   # accumulating summary
```

## Leadtime buckets

Every forecast row has a leadtime (`valid_ts_utc - snapshot_ts_utc`) bucketed as:

- `0-6h` — nowcast, easiest
- `6-24h` — same operating day
- `24-48h` — next operating day (what the vendor is primarily sold for)
- `48-72h` — 2-3 days out
- `72-168h` — 3-7 days out

The scorer computes MAE, bias, RMSE, and max_abs_error per `(zone, bucket, source)`. Source is `noaa` or `vendor`.

## Before you touch anything, read the right reference

This skill keeps SKILL.md tight. Details live in `references/`:

- **`references/architecture.md`** — File-by-file reference, exact schedules, schema of every parquet and CSV.
- **`references/zones.md`** — Full zone table with context, exclusion reasoning, coordinate source, history of the rename from PPL/PCO/FEO/NJ to airport codes.
- **`references/runbooks.md`** — Copy-paste procedures for common tasks: backfill a date, manually trigger a workflow, add or remove a zone, investigate a suspicious MAE, check data freshness.
- **`references/gotchas.md`** — Known failure modes and fixes: Iowa Mesonet 429s, empty-frame dtype downcast, can't-score-old-dates, Cowork mount sync, PJM hour-ending convention, America/New_York to UTC for vendor data.
- **`references/vendor-integration.md`** — State of play on vendor capture: PowerShell script written, UNC path pending, how to finish wiring once the path arrives.

**Read the reference that matches the user's request before acting.** If you don't know which one applies, skim the table of contents above and pick the closest match. When in doubt, read `runbooks.md` — most operational questions are covered there.

## Working style with Ziya

Ziya runs ZKE Solutions. He wants direct, concise, brutally honest output. Preferences that apply here:

- **No emojis.** Ever.
- **Confirm plans before big executions** (writes that touch the repo, workflow triggers, schema changes). Small diagnostic fixes are fine to propose and patch in one turn.
- **Never assume.** If a value, path, or convention isn't verified, ask. This skill encodes what has been verified; anything outside it is open.
- **Respect his intelligence.** Explain the why, not just the how. Don't pad.
- **CSVs and markdown for deliverables.** Not JSON dumps. Not fluffy prose.

## The current state of the project (as of the skill creation)

**Working:**
- NOAA puller is live, hourly at :07 UTC, writes seven zones.
- ASOS truth puller is live, daily at 08:00 UTC, with rate-limit retries.
- Scorer is live, daily at 09:00 UTC, produces per-zone per-bucket MAE.
- Dashboard (oscilloscope aesthetic) reads live data via DuckDB.

**Pending:**
- **Vendor capture:** PowerShell script is written (`scripts/capture_vendor.ps1`). Line 23 has a placeholder UNC path. Once Ziya provides the real path from his work machine, the script installs as a Windows Task Scheduler job at 10 AM local. See `references/vendor-integration.md`.
- **Weekly management report:** not started. Needs to read `data/scores/daily_by_bucket.parquet` and produce a clean NOAA-vs-vendor pivot per zone per bucket. Only meaningful once a few days of vendor captures exist.

**Don't do until asked:**
- Don't score dates before the zone rename went live (anything before 2026-04-19 UTC). There are no NOAA snapshots with the current zone names for those dates.
- Don't add zones without explicit direction — the seven above are what the business cares about.
- Don't "improve" the vendor capture strategy without asking. The Java process writing to a network share at 8:30 AM is a given, not a constraint to route around.

## When things break, the fix is usually one of these

1. **Iowa Mesonet 429** — retry loop in `asos_truth.py` handles this; if it's still failing, the sleep between stations (`INTER_REQUEST_SLEEP_S`) may need raising. See `references/gotchas.md`.
2. **Scorer merge ValueError on dtype mismatch** — empty vendor frame is downcasting the concat. Fixed in the current code by filtering empties and re-casting before merge. If it reappears, check `references/gotchas.md`.
3. **noaa rows=0 for some date** — either no snapshots exist for that date (pipeline wasn't alive yet), or the date is outside the 9-day window `_load_noaa` scans. Pick a date the pipeline was actually running.
4. **Cowork bash mount doesn't show new commits** — mount doesn't sync real-time after GitHub Actions writes. Use the Read tool with Windows paths, or have the user verify via GitHub web UI.
