# Gotchas

Every landmine this project has stepped on, with the fix. Read this BEFORE blaming the data. Most "weird" outputs trace to one of the entries below.

## Table of contents

1. Iowa Mesonet 429 "Too Many Requests"
2. Empty vendor frame poisons concat dtype → merge ValueError
3. Can't score dates before the pipeline was live
4. Cowork bash mount doesn't sync real-time after GitHub Actions writes
5. PJM hour-ending convention (H_TEMP=24 means 00:00 next day)
6. Vendor timezone is America/New_York, not UTC
7. "NOAA rows = 0" when there's definitely data
8. ASOS parquet timestamp precision is `us`, not `ns`
9. Small-sample MAE is not a metric; it's a rumor

---

## 1. Iowa Mesonet 429 "Too Many Requests"

**Symptom:** `asos_truth.py` logs `HTTPError: 429 Client Error: Too Many Requests`. Some or all zones fail.

**Cause:** Iowa Mesonet is a free public service. They rate-limit aggressive callers. Early versions of this puller fired 7 requests in under a second — instant 429 on zones 2-7.

**Fix (already in code):**
- `INTER_REQUEST_SLEEP_S = 3.0` between stations
- Retry loop on 429/5xx with exponential backoff (5s, 10s, 20s, 40s)
- Honors `Retry-After` header if Mesonet sends one

**If it still happens:** widen `INTER_REQUEST_SLEEP_S`. 5 seconds between stations takes 35 seconds for 7 zones — still well within the 5-minute workflow timeout.

**Don't:** switch to a paid service. The ASOS pull is the one part of this pipeline we get for free. Keep it that way.

---

## 2. Empty vendor frame poisons concat dtype → merge ValueError

**Symptom:** `ValueError: You are trying to merge on object and datetime64[us, UTC] columns for key 'valid_ts_utc'`

**Cause:** When `data/vendor/` doesn't exist yet, `_load_vendor` returns `pd.DataFrame(columns=[...])` — no explicit dtypes, so columns are object. `pd.concat([noaa, vendor])` downcasts NOAA's `valid_ts_utc` from `datetime64[us, UTC]` to object. The subsequent merge against ASOS (which IS `datetime64[us, UTC]`) raises.

**Fix (already in code):**
- Filter empty frames before concat: `frames = [f for f in [noaa, vendor] if not f.empty]`
- Belt-and-suspenders re-cast at top of `_score`: `pd.to_datetime(..., utc=True)` on both sides

**How to spot it if it regresses:** log lines show NOAA rows > 0, vendor rows = 0, then a traceback on `.merge(...)`.

---

## 3. Can't score dates before the pipeline was live

**Symptom:** `score-daily` runs, logs `noaa rows=0 snapshots=0`, and aborts with "no scored rows."

**Cause:** Scoring a target date requires NOAA snapshots captured BEFORE that date, predicting into it. NOAA snapshots from AFTER the target date don't predict backward in time. The pipeline with current zone names (DCA/ABE/PHL/PIT/CLE/LCK/EWR) went live on 2026-04-19 UTC. There are no valid NOAA snapshots for dates earlier than that.

**Fix:** don't try to score those dates. The earliest scorable date is the first UTC day that has at least one snapshot in the previous 9-day window. In practice: 2026-04-19 onward.

**If the zone names change again in the future** (don't do this without good reason), the same constraint applies to the new names. Dates before the rename can't be scored under the new names.

---

## 4. Cowork bash mount doesn't sync real-time after GitHub Actions writes

**Symptom:** Claude runs `ls /sessions/.../mnt/noaa-forecast/data/scores/` in the sandbox and the file isn't there, but GitHub Actions just wrote it successfully and the GitHub web UI shows it.

**Cause:** The Cowork bash mount is a snapshot of the filesystem at session start (or some cached view). It doesn't reflect commits pushed from outside the session (like a GitHub Actions runner) in real-time.

**Fix:** use the Read tool with the Windows path directly — that reads live state. Or ask Ziya to verify from his browser. Don't trust bash `ls` for "did the latest workflow commit land?" — trust the GitHub web UI or a fresh `git pull` on Ziya's machine.

---

## 5. PJM hour-ending convention (H_TEMP=24 means 00:00 next day)

**Symptom:** Vendor forecast for "hour 24 of 2026-04-18" seems to have a temperature that matches "hour 0 of 2026-04-19" in NOAA — off by one day.

**Cause:** PJM (and most operator scheduling shops) use hour-ending notation. `H_TEMP=1` means the 60 minutes ending at 01:00. `H_TEMP=24` means 00:00 NEXT DAY, not 24:00 same day.

**Fix (already in code):** in `score_daily.py._load_vendor`:

```python
local_hour = df["H_TEMP_int"] % 24
next_day_shift = (df["H_TEMP_int"] == 24).astype(int)
df["valid_ts_local"] = (
    df["D_TEMP_parsed"]
    + pd.to_timedelta(next_day_shift, unit="D")
    + pd.to_timedelta(local_hour, unit="h")
)
```

**Don't "simplify"** this. It looks weird but it's correct.

---

## 6. Vendor timezone is America/New_York, not UTC

**Symptom:** Vendor forecasts are off by ~4-5 hours from ASOS truth in a systematic way. Bias on 24-48h bucket is suspiciously clean at ~0 but MAE is high.

**Cause:** Vendor uses local operating day in America/New_York. NOAA and ASOS use UTC. If the conversion is skipped, everything shifts by the America/New_York offset (-4 hours in EDT, -5 in EST).

**Fix (already in code):**

```python
df["valid_ts_local"] = df["valid_ts_local"].dt.tz_localize(
    "America/New_York", ambiguous="NaT", nonexistent="shift_forward"
)
df["valid_ts_utc"] = df["valid_ts_local"].dt.tz_convert("UTC")
```

`ambiguous="NaT"` handles fall-back DST; `nonexistent="shift_forward"` handles spring-forward. Don't remove these args — they suppress runtime errors on two hours per year.

---

## 7. "NOAA rows = 0" when there's definitely data

**Symptom:** `score-daily` reports `noaa rows=0 snapshots=0` but `data/<ZONE>/<YYYY-MM-DD>/` has parquets.

**Candidate causes in order:**

1. **Target date outside the 9-day scan window.** `_load_noaa` scans `target_date - 8 days` through `target_date + 1 day`. If the parquets are OUTSIDE that window, they're skipped. Rare in normal operation.
2. **Parquets have the OLD zone name.** If zones.csv was updated but stale parquets weren't removed (step 2 of the rename runbook skipped), DuckDB reads them but filters by `location_name IN zones_csv_zones`, yielding 0.
3. **DuckDB read error silently returned empty.** Check workflow logs for any DuckDB warning near the `_load_noaa` log line.

**Debug:**

```python
import duckdb
con = duckdb.connect()
con.execute("""
SELECT DISTINCT location_name, COUNT(*) AS rows
FROM read_parquet('data/*/*/*.parquet')
GROUP BY 1 ORDER BY 2 DESC
""").df()
```

If `location_name` values don't match `zones.csv` → stale data, clean up.

---

## 8. ASOS parquet timestamp precision is `us`, not `ns`

**Symptom:** Merging ASOS with another frame produces `ValueError: You are trying to merge on datetime64[ns, UTC] and datetime64[us, UTC]`.

**Cause:** pyarrow writes timestamps at microsecond precision by default. pandas reads them back as `datetime64[us, UTC]`. If the other side of the merge is `datetime64[ns, UTC]` (pandas' default), the merge complains.

**Fix:** `pd.to_datetime(x, utc=True)` on both sides before merge normalizes precision. Already done in `_score` as belt-and-suspenders. If you see this error elsewhere, apply the same fix.

---

## 9. Small-sample MAE is not a metric; it's a rumor

**Symptom:** Ziya is looking at a number like `EWR 0-6h MAE = 5.00` with `n = 5` and asking "is NOAA broken for Newark?"

**Cause:** Five observations can't tell you anything. One bad hour (forecast 60, observed 45) on its own gives `MAE = 3`. Combine with typical 1-2°F errors on the other four and you easily hit 5.

**Fix:** report `n` alongside every MAE. For the weekly management report, hide any row where `n < 30` as "insufficient data" or gray-out. The pipeline accumulates naturally; wait a week before making claims.

**Rule of thumb:** trust `mae` when `n >= 30`. Take it seriously when `n >= 100`. Declare a winner only after `n >= 200` per (zone, bucket, source).
