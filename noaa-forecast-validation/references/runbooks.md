# Runbooks

Copy-paste procedures for the common operational tasks. Read the procedure matching the user's request, then execute.

## Table of contents

1. Backfill a date (re-score or pull for a historical day)
2. Manually trigger a workflow
3. Add or remove a zone
4. Investigate a suspicious MAE
5. Check data freshness / is the pipeline alive?
6. Re-score everything after a scoring logic change
7. Inspect scoring output ad hoc (DuckDB)
8. Bulk historical backfill (date range, ASOS + scoring)
9. Recover a missed vendor day from email

---

## 1. Backfill a date

**Context:** pipeline went down for a day, or Ziya wants a specific historical date scored.

**What to check first:** does NOAA data exist for the date? NOAA snapshots predicting hours on target_date must have been captured in the 9 days BEFORE target_date. If the pipeline wasn't running, those snapshots don't exist and the backfill is a lost cause. See `gotchas.md` → "Can't score dates before pipeline was live."

**Procedure:**

```
# 1. Pull ASOS for the date (idempotent — safe to re-run)
# GitHub Actions → asos-truth → Run workflow → date = YYYY-MM-DD

# 2. If you have vendor captures for that date and the day before, they should
# already be in data/vendor/. If not, and Ziya has archived copies, drop them
# into data/vendor/<YYYY-MM-DD>.csv and commit.

# 3. Score the date
# GitHub Actions → score-daily → Run workflow → date = YYYY-MM-DD
```

The scorer upserts `daily_by_bucket.parquet` by `(asos_date, zone, source, bucket)`, so re-running overwrites the same rows cleanly. `hourly_detail_<date>.parquet` gets rewritten.

---

## 2. Manually trigger a workflow

All three workflows support `workflow_dispatch`. Ziya triggers manually via GitHub Actions UI.

- **noaa.yml** — no date input; always pulls "now." Manual trigger is mainly for testing or filling a gap right after a deploy.
- **asos-truth.yml** — optional `date` input (blank = yesterday UTC). Provide `YYYY-MM-DD` to pull a specific day.
- **score-daily.yml** — optional `date` input (blank = yesterday UTC). Provide `YYYY-MM-DD` to score a specific day.

**Quick sanity after a manual trigger:** check the workflow's last log for the `OK ...` line at the end. Each script emits a one-line success summary followed by useful numbers. If it errored, the traceback is inline — search the log for `ERROR` or `Traceback`.

---

## 3. Add or remove a zone

Rare but procedure matters. See `zones.md` → "Adding or removing a zone" for the detailed steps. Short version:

```
# Add
# 1. Edit zones.csv — append new row (zone, c_region, icao, wban, lat, lon)
# 2. Commit + push
# 3. Wait for next hourly noaa.yml run, or manually trigger it
# 4. Verify data/<NEW_ZONE>/<today>/ has a parquet
# 5. Trigger asos-truth.yml manually with date=yesterday to verify the new ICAO works
# 6. Next morning's score-daily run picks up the new zone automatically

# Remove
# 1. Edit zones.csv — drop the row
# 2. git rm -r data/<OLD_ZONE>/
# 3. Commit + push
# 4. (Optional) purge old zone from daily_by_bucket.parquet — usually fine to leave as historical context
```

---

## 4. Investigate a suspicious MAE

Ziya sees a number he doesn't trust. Example: "EWR shows 5 degrees MAE, that can't be right."

**Triage order:**

```
# a. Check sample size
# A 5°F MAE from 5 observations is noise. A 5°F MAE from 500 observations is a real problem.
# data/scores/daily_by_bucket.parquet has an 'n' column. Grep or query it.

# b. Pull the hourly detail for that zone + date
# data/scores/hourly_detail_<date>.parquet has every scored row with forecast,
# observed, error, leadtime, snapshot. Filter to zone=EWR and eyeball.
```

Quick DuckDB:

```sql
-- what's the per-hour story for EWR on a date?
SELECT valid_ts_utc, source, leadtime_h, forecast_tmpf, tmpf_observed, error
FROM read_parquet('data/scores/hourly_detail_2026-04-19.parquet')
WHERE zone = 'EWR'
ORDER BY valid_ts_utc, source, leadtime_h;
```

**Then ask:**

- Is there one bad hour dominating? (One forecast of 70 against observed 55 inflates MAE fast with small n.)
- Does the error pattern look like a timezone bug? (Systematic offset aligned to hour-of-day suggests UTC vs EPT confusion.)
- Is `tmpf_observed` reasonable? Cross-check against the ASOS parquet — if ASOS itself is weird, `valid_ts_utc` in the ASOS file might have a precision mismatch. See `gotchas.md`.
- Is the zone's ICAO in `zones.csv` the right airport? (Typos here produce totally plausible-looking but wrong data.)

**Don't jump to "NOAA is bad" until you've ruled out the data plumbing.**

---

## 5. Check data freshness / is the pipeline alive?

Fastest checks, in order:

```
# a. GitHub Actions UI — look at the three workflows' last run times and statuses.
# If noaa.yml hasn't run in the last 90 min, something's wrong.

# b. Check latest snapshot for one zone via GitHub web UI:
#    github.com/zkesrefoglu/noaa-forecast/tree/main/data/DCA
# Most recent date subdirectory should be today (UTC).

# c. If running locally, git pull and look at data/<ZONE>/<today>/ file count.
```

Expected daily volume:
- NOAA: ~24 parquets per zone per day (hourly)
- ASOS: 1 parquet per day, 7 zones x ~24 rows = ~168 rows
- Scores: 1 `hourly_detail_<date>.parquet` per day, plus the accumulating `daily_by_bucket.parquet`

---

## 6. Re-score everything after a scoring logic change

If Ziya changes `score_daily.py` in a way that affects historical outputs (new bucket definition, bug fix, different aggregation):

```
# 1. Commit and push the code change.
# 2. Delete the accumulating file so it rebuilds cleanly from scratch:
git rm data/scores/daily_by_bucket.parquet
git commit -m "reset daily_by_bucket for rescore"
git push

# 3. Re-trigger score-daily for each date that has ASOS truth available:
# GitHub Actions → score-daily → Run workflow → date = YYYY-MM-DD
# One manual run per date. No bulk-backfill workflow exists yet.
```

If there are more than ~10 dates to re-score, ask Ziya if a bulk backfill workflow is worth building — it's 20 lines of YAML but adds surface area.

---

## 7. Inspect scoring output ad hoc (DuckDB)

Connect via `query.py` or just:

```python
import duckdb
con = duckdb.connect()

# Latest NOAA vs vendor per zone per bucket
con.execute("""
SELECT zone, bucket, source, n, mae, bias, rmse
FROM read_parquet('data/scores/daily_by_bucket.parquet')
WHERE asos_date = (SELECT MAX(asos_date) FROM read_parquet('data/scores/daily_by_bucket.parquet'))
ORDER BY zone, bucket, source
""").df()

# 7-day rolling NOAA MAE by zone for 24-48h bucket
con.execute("""
SELECT zone, AVG(mae) AS mae_7d
FROM read_parquet('data/scores/daily_by_bucket.parquet')
WHERE source = 'noaa' AND bucket = '24-48h'
  AND asos_date >= CURRENT_DATE - INTERVAL 7 DAY
GROUP BY zone
ORDER BY mae_7d
""").df()
```

For Ziya's weekly deliverable, the 24-48h bucket is the most important — that's the leadtime the vendor is paid to nail.

---

## 8. Bulk historical backfill (date range, ASOS + scoring)

**Context:** new vendor data arrived (e.g. via email recovery), or the scoring logic changed enough that you want a full re-score, or you're spinning up scoring against historical vendor captures for the first time.

**Procedure:**

```powershell
cd C:\Users\Ziya\Documents\GitHub\noaa-forecast
python scripts/historical_backfill.py --start 2025-04-25 --end 2026-04-24
```

Per-date behavior:
- ASOS: pulls Iowa Mesonet truth ONLY if `data/asos/<date>.parquet` doesn't already exist. Idempotent.
- Scoring: always runs (the scorer upserts by `(asos_date, zone, source, bucket)`, so re-running refreshes cleanly).

Runtime: ~2.5h on a fully-fresh range (ASOS rate-limited at 3s/station/day). ~30 min if ASOS already exists and only scoring runs (e.g. after a `score_daily.py` patch).

The orchestrator catches per-date failures and continues — one bad day doesn't kill the batch. End-of-run summary lists ASOS pulls / skips / failures and scoring successes / failures with the failing dates listed inline.

**After it finishes:**
```powershell
git add data/asos/ data/scores/
git commit -m "backfill: <range> (ASOS + scores)"
git push
```

**Don't:** run this inside a GitHub Actions workflow. It calls `asos_truth.py` 365+ times and would run afoul of Iowa Mesonet rate limits in a CI context. Local execution only.

**Don't:** start the range earlier than 2026-04-19 expecting NOAA scores. NOAA snapshots only exist from that date forward (when the renamed zones went live). Vendor scoring still works for earlier dates — produces vendor-only rows, no NOAA-vs-vendor comparison.

---

## 9. Recover a missed vendor day from email

**Context:** the server scheduled task `ZKE_NOAA_Vendor_Capture` failed to run on a particular day (server reboot, credential expired, network hiccup). The day's CSV is missing from `data/vendor/`.

**Why this is recoverable:** the WGL SQL job emails the CSV as an attachment to a distribution list including Ziya on every run (8:39 AM and 3:39 PM). Outlook keeps it.

**Procedure for a single missed day (manual):**

1. Open Outlook desktop.
2. Search Inbox for `subject:Hourly_Temperatures_Report` filtered to the missed date.
3. The morning email arrived ~8:39 AM. Open that one (not the 3:39 PM one — afternoon reflects different upstream info).
4. Save the attached CSV: right-click attachment → Save As → `<missed-date>.csv` (e.g. `2026-05-12.csv`).
5. Drop into `C:\Users\Ziya\Documents\GitHub\noaa-forecast\data\vendor\`.
6. `git add data/vendor/<missed-date>.csv && git commit -m "vendor: manual recovery for <date>" && git push`.

**Procedure for multiple missed days (bulk):**

Use the macro at `scripts/outlook_backfill_vendor.bas`. Same as the original 2026-04-26 backfill — saves AM and PM CSVs to `Desktop\vendor_backfill\` named by date. Filter to the dates you need, copy AM versions only into `data/vendor/`, commit + push.

**If the server task is silently failing repeatedly:** inspect on `stpwsvcritfil04`:

```powershell
Get-ScheduledTaskInfo -TaskName "ZKE_NOAA_Vendor_Capture" |
    Select-Object LastRunTime, LastTaskResult, NumberOfMissedRuns
```

`LastTaskResult` non-zero or stale `LastRunTime` → open Task Scheduler GUI → History tab for full diagnostics. See `docs/server-capture-runbook.md` troubleshooting section.
