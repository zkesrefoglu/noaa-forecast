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
10. Vendor `Q_TEMP` is mixed units: source=1 is Fahrenheit, source=4 is Celsius
11. PowerShell ExecutionPolicy blocks unsigned scripts from network shares

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

---

## 10. Vendor `Q_TEMP` is mixed units: source=1 is Fahrenheit, source=4 is Celsius

**Symptom:** Vendor MAE comes out at ~45°F across every zone and every bucket. Looks insane (no commercial vendor is that bad). Bias is also large, in the same direction as the MAE magnitude.

**Cause:** The WGL SQL export `ops-query-in-out_hourly_temp.csv` mixes units in the `Q_TEMP` column based on `C_WEATHER_SOURCE`:

- `C_WEATHER_SOURCE = 1` (historical actuals): **Fahrenheit**
- `C_WEATHER_SOURCE = 2` (alternate historical): **Fahrenheit** (assumed; not verified)
- `C_WEATHER_SOURCE = 4` (forecast): **Celsius**

The scorer filters to source=4 (the forecast rows), so it sees Celsius values. If treated as Fahrenheit, comparison against ASOS Fahrenheit truth produces a ~25-32°F constant offset that registers as ~45°F MAE on typical US daily temperatures. The math: `30°C - 86°F = -56` if both treated as F; `|30 - 86| = 56`; pooled across daily highs and lows, mean abs error ~45°F.

**Fix (already in code):** in `score_daily.py._load_vendor`:

```python
# CRITICAL: vendor file uses MIXED UNITS by source code.
# Already filtered to source=4 above, so convert C -> F.
q_celsius = pd.to_numeric(df["Q_TEMP"], errors="coerce")
df["forecast_tmpf"] = q_celsius * 9.0 / 5.0 + 32.0
```

**How this was caught:** initial historical backfill produced vendor MAE of 45°F across all 7 zones at every bucket. The magnitude was dead-giveaway — too clean, too uniform, exactly the C-to-F offset for mid-latitude temperatures. Discovered 2026-04-26.

**Don't:**
- Don't assume the WGL data dictionary is authoritative. The vendor-integration.md doc said "Q_TEMP — temperature, °F" — it was wrong. Verify by spot-checking a hot summer afternoon in DC: if you see values like 30, that's Celsius; 86 is Fahrenheit.
- Don't fix this by converting `Q_TEMP` upstream of the source filter. Source=1 (historical) is genuinely in Fahrenheit and shouldn't be converted. The conversion lives AFTER the source=4 filter for that reason.
- Don't trust this if WGL changes the SQL query that produces the file. If the SQL ever joins different upstream tables or changes units, this assumption breaks silently. Add a sanity check: if vendor MAE jumps to 40+°F overnight, suspect a unit change before suspecting the model.

---

## 11. PowerShell ExecutionPolicy blocks unsigned scripts from network shares

**Symptom:** Running `.\scripts\capture_vendor.ps1` directly on the server produces:

```
.\scripts\capture_vendor.ps1 cannot be loaded. The file ... is not digitally signed.
You cannot run this script on the current system. SecurityError: PSSecurityException
UnauthorizedAccess
```

**Cause:** Windows treats files from network paths (UNC shares like `\\stpwsvcritfil04\...`) as untrusted by default, even when the path resolves to the same machine. The default `ExecutionPolicy` (typically `RemoteSigned` on servers) refuses to run unsigned scripts from these locations.

**Fix for manual smoke tests:**

```powershell
# One-shot bypass for a single script invocation:
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\capture_vendor.ps1

# Or set bypass for the current shell only (process scope):
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\scripts\capture_vendor.ps1
```

**Fix for the scheduled task:** the runbook already passes `-ExecutionPolicy Bypass` in the task's `-Argument` string. The task runs without policy interference, so this only bites manual testing — but it bites the FIRST manual test, which is exactly when you don't want surprises.

**Don't:** flip the system-wide ExecutionPolicy to Bypass or Unrestricted. That weakens server security globally for one script. Process-scope bypass during testing is enough.

**How this was caught:** discovered 2026-04-27 during the first server-side smoke test of `capture_vendor.ps1`. Three attempts before the bypass was applied — easy hour to lose if you don't recognize the message. Now you have.
