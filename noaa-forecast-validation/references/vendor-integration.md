# Vendor Integration — State of Play

Vendor capture is wired and a year of historical data has been ingested. This document is the single source of truth on the vendor feed: what it is, how units work, how data flows in (now and going forward), and what the data has told us.

## What the vendor feed is

The WGL OPS pipeline writes a single CSV to a network share twice per workday:

```
\\stpwsvcritfil04\WGES-Databases\Reports\DailyHourlyTemp\ops-query-in-out_hourly_temp.csv
```

Filename is fixed (no timestamp). The file is **overwritten in place** on each run — no history is retained on the share.

**The chain on the WGL server (`stpwsvcritfil04`):**

1. `Ops-Hourly-Temps_ps1` runs at 8:30 AM daily — Java job (`TmprHistRefresherApp`) refreshes the underlying Oracle table with the vendor's latest forecast.
2. `Ops_SQLQueryInputOutput_Hourly_Temp` runs at 8:39 AM and 3:39 PM — SQL job exports the CSV via `ops-query-in-out_hourly_temp.sql` and emails it as an attachment to a distribution list (Ziya is on the list).

**The morning run is what the scheduling team commits against.** The afternoon run is a refresh that reflects different upstream information and is NOT a substitute for the morning capture. Capture window for the morning file is approximately 8:39 AM → 3:39 PM local. After 3:39 PM, the morning version is gone.

## Verified schema (2026-04-20 inspection)

8 columns, comma-delimited, with header row:

```
D_TEMP, H_TEMP, Q_TEMP, F_WKND_HLDY, D_LST_UPD, C_LST_UPD_USER_ID, C_REGION, C_WEATHER_SOURCE
```

- `D_TEMP` — local operating date, `M/D/YYYY` or `MM/DD/YYYY` in `America/New_York`.
- `H_TEMP` — hour 1..24 (PJM hour-ending; H_TEMP=24 means 00:00 next day).
- `Q_TEMP` — temperature. **Mixed units depending on `C_WEATHER_SOURCE`** — see Gotcha #10.
- `F_WKND_HLDY` — 0/1 weekend-or-holiday flag. Not used by scorer.
- `D_LST_UPD`, `C_LST_UPD_USER_ID` — audit trail. Not used.
- `C_REGION` — 1..11. See `zones.md` for the electricity subset (1, 3, 4, 5, 8, 9, 11).
- `C_WEATHER_SOURCE` — `1 = historical actuals (F)`, `2 = alternate historical (assumed F)`, `4 = forecast (C)`. The scorer filters to source=4 and converts to Fahrenheit.

A typical file has ~10k rows: ~30 days of historical actuals plus ~14 days of forecast for all electricity regions.

## Two ingestion paths

**Path A — going-forward (planned):** scheduled task on `stpwsvcritfil04` itself, fires at 9:00 AM after the morning SQL job lands the CSV. Copies the file from local disk into the cloned repo, commits, pushes. Pending GitHub-from-server access confirmation. See `docs/vendor-capture-runbook.md`.

**Path B — historical backfill (one-time, completed 2026-04-26):** every morning + afternoon CSV is also emailed to Ziya as an attachment with subject `Hourly_Temperatures_Report (NNNN rows found)`. The Outlook macro `scripts/outlook_backfill_vendor.bas` extracted ~12 months of attachments from his Inbox in 3 minutes. Saved both AM and PM versions to disk, AM-only goes into `data/vendor/<date>.csv` (PM kept locally for future analysis but not committed).

The email path is also the safety net if Path A breaks: as long as Ziya stays on the distribution list, the data continues arriving in his Inbox regardless of what the server-side capture does.

## What's done

- **Capture script (`scripts/capture_vendor.ps1`):** UNC path filled in, path-agnostic via `$PSScriptRoot`. Originally intended for the work laptop; superseded by the planned server task but kept in the repo as a reference / fallback.
- **Outlook backfill macro (`scripts/outlook_backfill_vendor.bas`):** extracted 669 unique CSVs (360 AM, 309 PM) covering 2025-04-25 → 2026-04-25.
- **Historical backfill orchestrator (`scripts/historical_backfill.py`):** loops over a date range, pulls ASOS truth from Iowa Mesonet, runs `score_daily.py` per date. Resumable, idempotent, ~21 sec per ASOS pull. Took ~2.6h to backfill 365 days.
- **Scorer hook (`score_daily.py._load_vendor`):**
  - Reads `data/vendor/<date>.csv` for `target_date` and `target_date - 1` (vendor file covers ~14 forward days; we only use the first 48h).
  - Filters to `C_WEATHER_SOURCE=4`.
  - Converts `Q_TEMP` from Celsius to Fahrenheit (Gotcha #10).
  - Converts `(D_TEMP, H_TEMP)` from `America/New_York` to UTC.
  - Maps `C_REGION` to zone name via `zones.csv`.
  - Handles missing vendor dir gracefully (returns empty frame).
- **Dashboard (`build_dashboard.py` + `dashboard_template.html`):** `FORECAST ACCURACY VS REALITY` section renders MAE per source × bucket per zone, sourced from `data/scores/daily_by_bucket.parquet`.

## What the data says (initial findings, 2026-04-26)

Coverage as of first scoring run:
- Vendor: 360 days of morning captures, ~7,500 scored bucket-rows
- NOAA: 7 days of native pipeline captures, ~165 scored bucket-rows

**24-48h leadtime (the operationally relevant one for next-day load scheduling):**

| Zone | Vendor MAE | NOAA MAE | Δ | Winner |
|---|---|---|---|---|
| DCA | 2.78 | 2.69 | -0.09 | NOAA |
| ABE | 2.63 | 3.10 | +0.47 | Vendor |
| CLE | 2.66 | 3.52 | +0.86 | Vendor |
| EWR | 2.68 | 3.99 | +1.31 | Vendor |
| LCK | 2.55 | 2.05 | -0.50 | NOAA |
| PHL | 2.57 | 2.23 | -0.34 | NOAA |
| PIT | 2.52 | 3.56 | +1.04 | Vendor |

**Headline:** vendor wins 4 of 7 zones, NOAA wins 3 of 7. **But the magnitude differential is the real story** — vendor's average winning margin is 0.92°F; NOAA's average winning margin is 0.31°F. When vendor wins, it wins decisively; when NOAA wins, it wins narrowly.

**Caveats:**
- NOAA has 7 days of data; vendor has 360. The NOAA numbers are based on a small sample and will shift as data accumulates. Per Gotcha #9, declare nothing on `n < 200`.
- Vendor 24-48h coverage is 10 obs/day (afternoon hours only) due to the morning capture's 14:00 UTC snapshot timing. Could bias the metric.
- DCA is vendor's worst zone — and it's WGL's home market. Suspicious. Worth investigating whether the airport ASOS station (KDCA) has known biases or whether vendor's model is under-tuned for the urban DC microclimate.

## Don't do this

- **Don't re-architect around the WGL Java/SQL chain.** It works, it's not ours to change, and it generates the file reliably.
- **Don't store sensitive UNC paths or credentials in the repo.** The current path is an internal share, not a secret. If that ever changes, switch to env vars.
- **Don't claim "missed days are gone" — they're not.** The CSV is emailed to Ziya as an attachment on every run, and the email backfill macro can recover any historical morning we have in his Inbox. Outlook retention sets the actual horizon.
- **Don't run capture from Ziya's work laptop going forward.** The laptop dependency is what we're killing. The server task on `stpwsvcritfil04` removes the need for the laptop to be on, awake, or even the user's responsibility.
- **Don't trust `Q_TEMP` as Fahrenheit without checking source.** Source=4 is Celsius. See Gotcha #10. If WGL changes the SQL, sanity-check the units before believing any new MAE numbers.

## Open questions (ask Ziya when relevant)

- Does the vendor file format ever change mid-year? If columns shift, `_load_vendor` needs a version flag.
- Are there days the Java/SQL chain fails or doesn't run (holidays, server maintenance)? Capture script currently fails loudly on missing source file; may need to soften.
- Does Ziya want a Slack/email alert when a server-side capture is missed?
- DCA underperformance: is KDCA's ASOS station biased, or is vendor's DC microclimate model weak? Worth a focused investigation.
- Should vendor PM captures (currently kept locally, not in repo) be ingested? They'd give us a second snapshot per day to compare AM vs PM forecast skill.

## File / script index

- `scripts/capture_vendor.ps1` — laptop-side capture (legacy / fallback)
- `scripts/outlook_backfill_vendor.bas` — VBA macro for one-time historical email extraction
- `scripts/historical_backfill.py` — orchestrator for ASOS pull + scoring loop over a date range
- `docs/vendor-capture-runbook.md` — laptop-machine runbook (legacy)
- `score_daily.py._load_vendor` — scorer's vendor ingestion logic with C-to-F conversion and timezone handling
- `data/vendor/<YYYY-MM-DD>.csv` — committed AM captures (one per day)
- `data/scores/daily_by_bucket.parquet` — accumulating per-(date, zone, source, bucket) MAE/bias/RMSE
