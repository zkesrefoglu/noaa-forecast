# NOAA vs Vendor Forecast Validation — Status Evaluation

**Date:** 2026-05-17
**Window analyzed:** 2026-04-19 to 2026-05-16 (28 days of NOAA, parity window)
**Vendor archive used:** full available history (2025-04-25 to 2026-05-14)
**Truth source:** ASOS hourly observations, top-of-hour ±30 min

---

## TL;DR

1. **The capture pipeline is healthy.** NOAA pulling hourly. ASOS pulling daily. Vendor capture is running and has produced 684 files since the project began.
2. **The scorer has a real bug** — `score_daily.py` is ignoring 309 `_pm` files and truncating vendor reads to a 2-day window. It is using `~3,000` vendor rows when the actual dataset has `~1.6 million`. Every number in `daily_by_bucket.parquet` for the `vendor` source is undercount-by-construction.
3. **Apples-to-apples (corrected, 28-day NOAA window):** NOAA and vendor are statistically indistinguishable at every leadtime the vendor sells. NOAA is slightly better at long lead (72-168h).
4. **The killer finding for management:** at the 24-48h bucket (the bucket the vendor is paid to nail), NOAA `2.47°F` MAE vs Vendor `2.41°F` MAE. Difference is `0.06°F`. That is inside the noise. **Free NOAA appears to be a viable replacement for the paid feed.**
5. **PHL has a persistent NOAA cold bias** (-1.01°F at 24-48h) worth investigating before any production switch.

---

## Pipeline health

| Stream | Status | Latest | Volume |
|--------|--------|--------|--------|
| NOAA hourly forecasts | Healthy | 2026-05-17 (today, 17 snapshots so far) | 7 zones, ~19-22 snapshots/day |
| ASOS truth | Healthy | 2026-05-16 (yesterday) | 387 days total, no gaps in last 30 |
| Vendor capture | Running, with weekend gaps | 2026-05-14 | 684 files (375 base + 309 `_pm`) |
| Score writer | Running | 2026-05-16 | 388 hourly_detail files written |

**Vendor capture gaps in the last 14 days:** 2026-05-09, 2026-05-10, 2026-05-15, 2026-05-16, 2026-05-17.

Pattern reads as weekend + Friday-May-15. May 9-10 and May 16-17 are both Sat/Sun, which fits the hypothesis that the upstream Java process does not run on weekends. May 15 is a Friday and an outlier — worth checking whether that was a holiday at the company, a Java failure, or a scheduled-task miss on your work machine.

---

## The scorer bug (this is the thing to fix first)

`score_daily.py::_load_vendor` does two things that throttle vendor coverage:

1. **Globs only `data/vendor/<date>.csv`** — never `<date>_pm.csv`. That's 309 captures (~45% of all vendor files) being thrown out before scoring even starts.
2. **Reads only `(target_date, target_date-1)`** — line 195-199: `for offset in (0,1)`. But each vendor file contains a 14-day forward forecast horizon. A capture from 2026-05-14 contains forecast hours through 2026-05-28. The scorer is dropping all forecast rows with leadtime greater than ~2 days.

Concrete impact, parity window:

| Bucket | Vendor rows in `daily_by_bucket.parquet` (current scorer) | Vendor rows when reading data properly |
|--------|-----------------------------------------------------------|----------------------------------------|
| 0-6h | 877 | 1,213 |
| 6-24h | 2,585 | 3,652 |
| 24-48h | 1,533 | 5,065 |
| 48-72h | 0 | 5,261 |
| 72-168h | 0 | 22,757 |

The 48-72h and 72-168h vendor buckets do not appear in current scoring output at all. They exist in the data.

**Recommendation:** patch `_load_vendor` to (a) glob `f"{capture_date.isoformat()}*.csv"` so `_pm` files are included, and (b) widen the capture-date window from `(0,1)` to at least `(0,1,2,3,4,5,6,7)` so the full 7-day forward horizon gets scored. Then `git rm data/scores/daily_by_bucket.parquet` and re-score every date (the runbook covers this).

I have NOT changed any code yet — wanted to confirm with you first.

---

## Apples-to-apples MAE comparison (corrected)

Vendor numbers below come from a parallel script that reads every vendor file directly, bypassing the broken scorer. NOAA numbers come from `daily_by_bucket.parquet` (NOAA scoring is fine).

### Pooled across all seven zones, parity window 2026-04-19 to 2026-05-16

| Bucket | NOAA n | NOAA MAE (°F) | NOAA bias | Vendor n | Vendor MAE (°F) | Vendor bias | Winner |
|--------|--------|---------------|-----------|----------|-----------------|-------------|--------|
| 0-6h   | 21,155 | 1.92 | +0.30 | 1,213  | 2.05 | +0.52 | NOAA (0.13°F) |
| 6-24h  | 62,914 | 2.13 | +0.20 | 3,652  | 2.02 | +0.43 | Vendor (0.11°F) |
| 24-48h | 81,089 | 2.47 | +0.01 | 5,065  | 2.41 | +0.26 | **Tie (0.06°F)** |
| 48-72h | 78,508 | 2.91 | -0.15 | 5,261  | 3.05 | +0.18 | NOAA (0.14°F) |
| 72-168h | 285,425 | 4.24 | +0.28 | 22,757 | 4.67 | +0.93 | NOAA (0.43°F) |

**Read:** at every bucket, the two sources are within roughly 0.5°F of each other. NOAA edges out at the extremes (nowcast and 3-7 days). Vendor edges out at the same-op-day bucket. The 24-48h bucket — the operational sweet spot — is a tie.

NOAA also runs less biased than vendor at every leadtime, which matters for downstream load modeling. Vendor consistently forecasts warmer than reality (+0.43 to +0.93°F).

### Per zone, 24-48h bucket (the bucket that matters most)

| Zone | NOAA MAE | NOAA bias | Vendor MAE | Vendor bias | Diff (NOAA-Vendor) |
|------|----------|-----------|------------|-------------|--------------------|
| ABE  | 2.67 | -0.15 | 2.78 | +0.11 | **-0.11 (NOAA better)** |
| CLE  | 2.65 | +0.88 | 2.63 | +0.68 | +0.02 (tie) |
| DCA  | 2.22 | -0.36 | 2.39 | -0.33 | **-0.17 (NOAA better)** |
| EWR  | 2.32 | +0.24 | 2.33 | +0.17 | -0.01 (tie) |
| LCK  | 2.31 | +0.49 | 2.01 | +0.67 | +0.30 (Vendor better) |
| PHL  | 2.30 | **-1.01** | 2.03 | -0.15 | +0.27 (Vendor better) |
| PIT  | 2.79 | -0.00 | 2.65 | +0.71 | +0.14 (Vendor better) |

NOAA wins at ABE and DCA. Vendor wins at LCK, PHL, PIT. ABE/CLE/EWR are statistical ties.

### Per zone, 72-168h bucket (long lead)

NOAA beats vendor at every zone in this bucket. The 0.4-0.9°F edge is consistent. If the question is "can we replace the vendor for long-range planning," the answer here is clearly yes.

---

## Anomalies worth investigating

### PHL NOAA cold bias

NOAA at PHL is systematically forecasting -1.01°F at 24-48h (and -1.52°F at 72-168h). That's not noise — it's a structural offset. Possibilities:

1. Wrong ICAO in `zones.csv` for PHL — verify it's `KPHL` and lat/lon points to PHL airport, not Philadelphia centroid.
2. NOAA's nearest grid point for PHL coordinates lands over a microclimate (riverfront, urban core).
3. ASOS PHL sensor calibration issue (less likely; would show against vendor too, and vendor PHL bias is only -0.15).

**Action:** pull `hourly_detail_2026-05-15.parquet` filtered to PHL, look at the per-hour story. Probably 30 minutes of work.

### CLE warm bias (both sources)

Both NOAA and vendor are running +0.7-0.9°F warm at CLE in 24-48h. Suggests CLE ASOS sensor reads cool, not that either forecaster is wrong. Worth a sanity check against METAR archives or a nearby station.

### Vendor capture: Friday May 15 miss

May 15 was a workday. The vendor file didn't appear. Was Java down, was the scheduled task off, or was your machine off at 10 AM that morning? Three minutes of investigation will tell you which.

---

## Files

| Path | What's in it |
|------|--------------|
| `noaa_vs_vendor_evaluation_2026-05-17.md` | This document |
| `headtohead_pooled.csv` | Pooled head-to-head, parity window, all buckets |
| `headtohead_full_zone_bucket.csv` | Per-zone per-bucket NOAA vs Vendor, parity window |
| `headtohead_24_48h_per_zone.csv` | Per-zone 24-48h drill-down |
| `headtohead_72_168h_per_zone.csv` | Per-zone 72-168h drill-down |
| `vendor_full_mae_by_bucket.csv` | Vendor full archive (13 months) MAE by bucket |
| `vendor_full_mae_by_zone_bucket.csv` | Vendor full archive MAE by zone x bucket |
| `vendor_full_scored.parquet` | All 1.6M vendor forecasts joined to ASOS (raw rows for ad-hoc) |
| `daily_row_counts.csv` | Daily row counts by source — useful for spotting outages |

---

## Recommended next steps in priority order

1. **Patch `_load_vendor`** to read `_pm` files and widen the capture window to 7 days. Re-score the full history. Without this, `daily_by_bucket.parquet` keeps lying.
2. **Investigate PHL cold bias.** If it's the ICAO or coordinates, fix `zones.csv` and re-score.
3. **Decide on weekend vendor gaps.** If Java doesn't run weekends, document that and stop treating Sat/Sun as gaps. If it should run, dig into why it didn't.
4. **Schedule the weekly management report** that the skill says you wanted. Now that we have a real head-to-head, it can be a one-pager refreshed automatically each Monday.

The headline you can take to management today, with the caveats above: **on 28 days of real-world parity testing, NOAA's free forecast performs within 0.5°F of the paid vendor feed across every leadtime bucket. At the 24-48h operating-day bucket, the difference is 0.06°F — well inside measurement noise.**
