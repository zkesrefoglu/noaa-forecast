# Architecture Reference

Detailed file-by-file reference for the NOAA forecast validation pipeline. Read this when you need to understand what a specific script does, what it writes, or what triggers it.

## Schedules at a glance

| job | when | where | what it does |
|-----|------|-------|--------------|
| `noaa.yml` workflow | hourly, minute 07 UTC | GitHub Actions | pulls NOAA DWML for every zone, writes one parquet per zone per snapshot |
| `ZKE_NOAA_Vendor_Capture` task | daily 9:00 AM local (server time) | `stpwsvcritfil04` (Windows Task Scheduler, runs as `WGLCO_DOMAIN\xml0001`) | runs `capture_vendor.ps1` — copies the morning Java-generated CSV from `\\stpwsvcritfil04\WGES-Databases\Reports\DailyHourlyTemp\` into the cloned repo and pushes |
| `asos-truth.yml` workflow | daily, 08:00 UTC | GitHub Actions | pulls Iowa Mesonet ASOS observations for yesterday UTC |
| `score-daily.yml` workflow | daily, 09:00 UTC | GitHub Actions | joins NOAA + vendor + ASOS for yesterday, writes scores |

All GitHub workflows share the `noaa-forecast` concurrency group so they serialize against each other when a commit is being pushed. The vendor capture runs on a separate machine (the WGL server) and pushes independently, racing the workflows; the upsert pattern in `score_daily.py` handles overlap cleanly.

## NOAA puller: `noaa_forecast.py`

**Source:** NOAA's public Digital Weather Markup Language (DWML) XML endpoint. One HTTP call per zone per snapshot.

**What a snapshot looks like:**
- Input: zone with lat/lon
- Output: `data/<ZONE>/<YYYY-MM-DD>/<snapshot_ts_utc>.parquet`
- Schema: `snapshot_ts_utc`, `valid_ts_utc`, `location_name`, `temp_f`, plus supporting fields
- `location_name` is the zone slug (DCA, ABE, etc.) — this is the join key into ASOS

**Why multiple snapshots per day:** NOAA updates its hourly grid forecasts roughly every hour. Each snapshot contains forecasts for the next ~168 hours. By keeping every snapshot, we can bucket by leadtime after the fact. An 09:00 snapshot predicting 15:00 same-day is a 6h leadtime; a snapshot from three days ago predicting the same 15:00 is a 72h leadtime. Different quality characteristics.

**When it fails:** transient NOAA 5xx. The workflow's built-in retry is light; if one run drops a zone, the next hour's run will pick up the forecast. Not a problem in practice.

## ASOS truth puller: `asos_truth.py`

**Source:** Iowa Mesonet ASOS archive, free public service, one HTTP call per ICAO per UTC date.

**What ASOS is:** Automated Surface Observing System. Physical instruments at airports (temperature, dewpoint, wind, pressure) reporting once per minute with a routine hourly obs at ~:53. Iowa State mirrors and serves this as CSV.

**Why we use it for truth:** it's independent of both NOAA (a forecast source) and the vendor (another forecast source). It's ground truth physical measurement. Also it's free.

**Output:** one parquet per date, combining all seven zones.
- Path: `data/asos/<YYYY-MM-DD>.parquet`
- Schema: `valid_ts_utc`, `zone`, `c_region`, `icao`, `tmpf_observed`, `obs_minute_offset`, `n_obs_in_window`, `source`
- One row per (zone, hour) on the target date. Missing hours preserved as NULL rather than dropped.

**Normalization:** ASOS publishes multiple obs per hour (routine + specials). The puller picks the observation closest to top-of-hour UTC within ±30 minutes, and records `obs_minute_offset` so you can tell how close to the hour the measurement actually was.

**Rate limiting (important):**
- `INTER_REQUEST_SLEEP_S = 3.0` seconds between stations
- `MAX_RETRIES = 4` on 429/5xx
- Exponential backoff: 5s, 10s, 20s, 40s
- Honors `Retry-After` header when present

Iowa Mesonet returns 429 quickly if you hammer it. The 3-second gap plus retry loop keeps the pipeline well-behaved. If it still 429s, widen `INTER_REQUEST_SLEEP_S`.

**Runtime:** ~30 seconds when healthy, up to ~5 minutes if multiple retries fire. The workflow timeout is 5 minutes; don't lower it.

## Vendor capture: `scripts/capture_vendor.ps1`

**Source:** the WGL OPS chain on `stpwsvcritfil04` produces `ops-query-in-out_hourly_temp.csv` at `\\stpwsvcritfil04\WGES-Databases\Reports\DailyHourlyTemp\` twice a workday:

1. `Ops-Hourly-Temps_ps1` (Java, 8:30 AM) refreshes the Oracle table.
2. `Ops_SQLQueryInputOutput_Hourly_Temp` (SQL, 8:39 AM and 3:39 PM) exports the CSV and emails it to a distribution list (Ziya is on it).

The afternoon SQL run overwrites the morning file, so capture must happen between 8:39 AM and 3:39 PM. The morning file is what the scheduling team commits against.

**What the PowerShell script does:**
1. Copies the file from `\\stpwsvcritfil04\WGES-Databases\Reports\DailyHourlyTemp\` to `data/vendor/<capture_date>.csv` in the local git clone.
2. `capture_date` is the source file's `LastWriteTime.Date`, NOT "today" — protects against the task firing late or pulling stale content.
3. Short-circuits if today's file is already captured with identical SHA256 (idempotent re-runs).
4. `git pull` → `git add` → `git commit` → `git push`. Repo path is determined dynamically via `$PSScriptRoot`, so the same script works regardless of where the clone lives (laptop, server, etc.).

**Schedule (deployed):** Windows Task Scheduler on `stpwsvcritfil04`, daily 9:00 AM, task name `ZKE_NOAA_Vendor_Capture`, runs as `WGLCO_DOMAIN\xml0001`. Repo lives at `\\stpwsvcritfil04\WGES-Databases\OPSJobs\weather\noaa-forecast`. See `docs/server-capture-runbook.md` for the full setup procedure and gotchas (especially the ExecutionPolicy hurdle on first manual smoke test).

**Legacy laptop variant:** an earlier draft of this runbook targeted Ziya's work laptop with a 10:00 AM trigger. That approach was abandoned because the laptop isn't always on (weekends, days off). The server is 24/7. The deprecated runbook is preserved at `docs/vendor-capture-runbook.md` for reference and as a contingency procedure if the server ever goes away.

**Vendor CSV columns Ziya cares about:**
- `C_REGION` — integer 1..11 (see zones table)
- `C_WEATHER_SOURCE` — filter to 4 (forecast rows). 1 is historical actuals; 2 is alternate historical (assumed). The scorer uses 4 only.
- `D_TEMP` — date (local, America/New_York)
- `H_TEMP` — hour 1..24 (PJM hour-ending convention — H_TEMP=24 means 00:00 next day)
- `Q_TEMP` — forecast temperature **in Celsius** when `C_WEATHER_SOURCE=4`. **In Fahrenheit** when `C_WEATHER_SOURCE=1`. Mixed units by source — see `gotchas.md` #10. The scorer converts source=4 from C to F at load time.

## Scorer: `score_daily.py`

**What it does:** for a target UTC date, joins all three streams and computes per-zone per-bucket MAE.

**Order of operations:**
1. Load ASOS truth for the target date. Required — fail if missing.
2. Load NOAA forecasts: scan parquets in `data/<ZONE>/<YYYY-MM-DD>/` across a 9-day window ending at the target date, filter to predictions where `valid_ts_utc::DATE = target_date`. DuckDB does the read for speed.
3. Load vendor forecasts from `data/vendor/<date>.csv` for the target date and the day before. Filter `C_WEATHER_SOURCE=4`, map `C_REGION` via zones.csv, **convert `Q_TEMP` from Celsius to Fahrenheit** (mixed-units gotcha #10), convert `(D_TEMP, H_TEMP)` from America/New_York to UTC. If no vendor dir, returns empty frame and skips.
4. Concat NOAA and vendor (filtering empty frames to avoid dtype downcast — see gotchas.md).
5. Inner merge on `(valid_ts_utc, zone)` against ASOS.
6. Compute per-row error (`forecast - observed`), abs_error, leadtime bucket.
7. Aggregate: per (zone, bucket, source) — `n`, `mae`, `bias`, `rmse`, `max_abs_error`.

**Outputs:**
- `data/scores/hourly_detail_<YYYY-MM-DD>.parquet` — one row per scored (zone, hour, source, snapshot) tuple. Re-runnable: overwrites on re-score.
- `data/scores/daily_by_bucket.parquet` — accumulating across dates. Key: `(asos_date, zone, source, bucket)`. Upsert logic replaces same-key rows before appending new ones, so re-scoring a date is safe.

**Log output:** prints a pivot table summary at the end — zones x buckets x sources, MAE in F. This is the first-glance answer.

## Dashboard: `build_dashboard.py` + `dashboard_template.html`

Standalone HTML with oscilloscope aesthetic. Reads NOAA parquets via DuckDB plus `data/scores/daily_by_bucket.parquet`, renders six chart sections per zone with a zone picker chip bar at the top:

1. **Latest snapshot** — KPI tiles (current temp, precip prob, cloud cover, wind, etc.)
2. **Forecast Drift — Temperature** — every NOAA snapshot overlaid as faded traces; latest is bold (the spaghetti chart)
3. **Drift Heatmap — Temp Delta vs Latest** — how much each snapshot disagreed with the latest, per future hour
4. **Stability Curve** — forecast spread vs leadtime; the publishable "how much does NOAA's prediction actually change" view
5. **Forecast Accuracy vs Reality** *(added 2026-04-26)* — vendor & NOAA MAE per leadtime bucket. Renders as a 6-column score grid (KPI tiles per source × bucket showing weighted MAE + day count + obs count) plus a grouped bar chart. Pulls from `data/scores/daily_by_bucket.parquet`.
6. **Precip Probability + Wind Speed** — secondary spaghetti charts

Built once per workflow run (via `score-daily.yml`) and committed as `docs/index.html` for GitHub Pages serving. Hash-based zone routing (`#zone=EWR`) for shareable per-zone links.

## `query.py`

Thin DuckDB helper for ad-hoc analysis. Exposes a connection with parquets registered as views. Use when Ziya wants a quick SQL answer against the live data.

## `zones.csv`

The seven-zone config. Schema: `zone, c_region, icao, wban, lat, lon`. See `zones.md` for the full table and history.

## `requirements.txt`

Minimal: `pandas`, `pyarrow`, `requests`, `duckdb`, `plotly` (for dashboard). No heavyweight framework.

## One-time / occasional scripts (not on a schedule)

These don't run automatically — they're invoked by hand for setup or backfill operations.

### `scripts/historical_backfill.py`
Orchestrator that walks a date range, pulls ASOS truth from Iowa Mesonet (idempotent — skips dates whose parquet already exists), then runs `score_daily.py` for each date. Resumable: if interrupted, re-running picks up where it stopped. Used 2026-04-26 to backfill 365 days of vendor scores against ASOS truth.

Usage:
```
python scripts/historical_backfill.py --start 2025-04-25 --end 2026-04-24
```

Run-time: ~2.5h on a fresh range (ASOS rate-limited at 3s/station/day). ~30 min if ASOS is already populated and only scoring needs to re-run (e.g. after a scoring logic change).

### `scripts/outlook_backfill_vendor.bas`
VBA macro for Outlook desktop. Iterates Inbox, finds emails matching subject prefix `Hourly_Temperatures_Report`, extracts the CSV attachment, saves to `C:\Users\<username>\Desktop\vendor_backfill\` named by received date (`<date>.csv` for AM, `<date>_pm.csv` for PM). Used 2026-04-26 to recover 12 months of historical vendor captures from Ziya's email — 669 unique CSVs in 3 minutes.

Run via Outlook VBA editor (Alt+F11 → File → Import File → select the `.bas` → F5). Re-runnable; existing files are overwritten.

### `scripts/capture_vendor.ps1`
The daily capture script itself. Runs on `stpwsvcritfil04` via the `ZKE_NOAA_Vendor_Capture` scheduled task. Documented above.
