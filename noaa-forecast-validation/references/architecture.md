# Architecture Reference

Detailed file-by-file reference for the NOAA forecast validation pipeline. Read this when you need to understand what a specific script does, what it writes, or what triggers it.

## Schedules at a glance

| job | when (UTC) | where | what it does |
|-----|------------|-------|--------------|
| `noaa.yml` workflow | hourly, minute 07 | GitHub Actions | pulls NOAA DWML for every zone, writes one parquet per zone per snapshot |
| `capture_vendor.ps1` | 10:00 local (14:00 UTC EDT) | Ziya's work machine | copies Java-generated CSV from network share, commits to repo |
| `asos-truth.yml` workflow | daily, 08:00 | GitHub Actions | pulls Iowa Mesonet ASOS observations for yesterday UTC |
| `score-daily.yml` workflow | daily, 09:00 | GitHub Actions | joins NOAA + vendor + ASOS for yesterday, writes scores |

All workflows share the `noaa-forecast` concurrency group so they serialize against each other when a commit is being pushed.

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

**Source:** Java process on the work machine that writes `ops-query-in-out_hourly_temp.csv` to a network share at 8:30 AM local every day. The Java process does not retain history — today's file overwrites yesterday's.

**What the PowerShell script does:**
1. Copies the file from the UNC path to `data/vendor/<capture_date>.csv` in the local git clone.
2. `capture_date` is the file's `LastWriteTime.Date`, NOT "today" — protects against late-running scheduled tasks tagging the wrong day.
3. Short-circuits if today's file is already captured with identical SHA256.
4. `git pull` → `git add` → `git commit` → `git push`.

**Schedule:** Windows Task Scheduler, 10:00 AM local, runs as the logged-in user. Chosen after 8:30 AM java dump, before any ad-hoc scheduler changes, and safely inside the morning work window.

**Vendor CSV columns Ziya cares about:**
- `C_REGION` — integer 1..11 (see zones table)
- `C_WEATHER_SOURCE` — filter to 4 (forecast rows only; other values are actuals or placeholders)
- `D_TEMP` — date (local, America/New_York)
- `H_TEMP` — hour 1..24 (PJM hour-ending convention — H_TEMP=24 means 00:00 next day)
- `Q_TEMP` — forecast temperature in Fahrenheit

## Scorer: `score_daily.py`

**What it does:** for a target UTC date, joins all three streams and computes per-zone per-bucket MAE.

**Order of operations:**
1. Load ASOS truth for the target date. Required — fail if missing.
2. Load NOAA forecasts: scan parquets in `data/<ZONE>/<YYYY-MM-DD>/` across a 9-day window ending at the target date, filter to predictions where `valid_ts_utc::DATE = target_date`. DuckDB does the read for speed.
3. Load vendor forecasts from `data/vendor/<date>.csv` for the target date and the day before. Filter `C_WEATHER_SOURCE=4`, map `C_REGION` via zones.csv, convert `(D_TEMP, H_TEMP)` from America/New_York to UTC. If no vendor dir, returns empty frame and skips.
4. Concat NOAA and vendor (filtering empty frames to avoid dtype downcast — see gotchas.md).
5. Inner merge on `(valid_ts_utc, zone)` against ASOS.
6. Compute per-row error (`forecast - observed`), abs_error, leadtime bucket.
7. Aggregate: per (zone, bucket, source) — `n`, `mae`, `bias`, `rmse`, `max_abs_error`.

**Outputs:**
- `data/scores/hourly_detail_<YYYY-MM-DD>.parquet` — one row per scored (zone, hour, source, snapshot) tuple. Re-runnable: overwrites on re-score.
- `data/scores/daily_by_bucket.parquet` — accumulating across dates. Key: `(asos_date, zone, source, bucket)`. Upsert logic replaces same-key rows before appending new ones, so re-scoring a date is safe.

**Log output:** prints a pivot table summary at the end — zones x buckets x sources, MAE in F. This is the first-glance answer.

## Dashboard: `build_dashboard.py`

Standalone HTML with oscilloscope aesthetic. Reads NOAA parquets via DuckDB, plots recent forecasts as overlaid traces. Useful for eyeballing whether forecasts look plausible or if something went sideways. Not part of the automated scoring loop.

## `query.py`

Thin DuckDB helper for ad-hoc analysis. Exposes a connection with parquets registered as views. Use when Ziya wants a quick SQL answer against the live data.

## `zones.csv`

The seven-zone config. Schema: `zone, c_region, icao, wban, lat, lon`. See `zones.md` for the full table and history.

## `requirements.txt`

Minimal: `pandas`, `pyarrow`, `requests`, `duckdb`, `plotly` (for dashboard). No heavyweight framework.
