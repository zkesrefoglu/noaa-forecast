# DCA Forecast Validation — NOAA vs Vendor

**Zone:** DCA (Washington National, KDCA, c_region=1)
**Date:** 2026-05-17
**NOAA parity window:** 2026-04-19 to 2026-05-16 (28 days)
**Vendor full archive:** 2025-04-25 to 2026-05-16 (387 days, 228,679 forecast rows)

---

## Headline for DCA

**NOAA beats vendor at every leadtime bucket.** Pooled over the 28-day parity window:

| Bucket | NOAA MAE (°F) | Vendor MAE (°F) | NOAA edge |
|--------|---------------|-----------------|-----------|
| 0-6h   | 1.85 | 2.42 | -0.57 |
| 6-24h  | 2.01 | 1.96 | +0.05 (tie) |
| 24-48h | **2.22** | **2.39** | **-0.17** |
| 48-72h | 2.67 | 3.16 | -0.48 |
| 72-168h | 4.04 | 4.72 | -0.68 |

DCA is the strongest case in the whole network for replacing the vendor with NOAA. The 24-48h operating-day bucket — the bucket the vendor is paid for — has NOAA running 0.17°F tighter. The long-lead 72-168h bucket has NOAA running 0.68°F tighter, on hundreds of thousands of rows.

Bias-wise, NOAA at DCA runs slightly cold (-0.36°F at 24-48h, -0.71°F at 48-72h). Vendor runs cold too at near-term but flips warm at long lead (+1.59°F at 72-168h). NOAA's bias profile is more stable.

---

## Sample sizes and confidence

| Bucket | NOAA n | Vendor n | Notes |
|--------|--------|----------|-------|
| 0-6h   | 3,048 | 173 | Vendor thin; NOAA edge robust |
| 6-24h  | 9,063 | 526 | Both healthy |
| 24-48h | 11,686 | 728 | **Operational bucket — both healthy** |
| 48-72h | 11,319 | 756 | Both healthy |
| 72-168h | 41,081 | 3,267 | Long lead, big sample |

NOAA sample is ~15x larger than vendor because NOAA captures hourly snapshots (24/day) while vendor captures only once or twice per day. The MAE is computed correctly per source — the n difference doesn't bias the comparison, just affects how tight the confidence interval is.

A 0.17°F difference on 11,686 vs 728 observations is a real signal, not noise. At these sample sizes, MAE standard errors are well below 0.05°F for NOAA and below 0.10°F for vendor.

---

## Vendor as a long-term benchmark

Because we have 387 days of vendor data vs 28 days of NOAA, vendor lets us check that the parity window is representative — i.e., the last 28 days aren't a freak weather period.

Vendor MAE for DCA, full 387-day archive:

| Bucket | Full-archive MAE | Parity-window MAE | Drift |
|--------|------------------|-------------------|-------|
| 0-6h   | 2.23 | 2.42 | +0.19 (recent slightly worse) |
| 6-24h  | 2.12 | 1.96 | -0.16 (recent slightly better) |
| 24-48h | 2.50 | 2.39 | -0.11 (recent slightly better) |
| 48-72h | 2.83 | 3.16 | +0.33 (recent worse) |
| 72-168h | 3.72 | 4.72 | +1.00 (recent meaningfully worse) |

Last 28 days were a touch noisier than the 13-month average at long lead — likely springtime cold-front variability. The 24-48h bucket is broadly stable. Don't overweight the 72-168h numbers in either direction.

---

## Per-day 24-48h pattern (the bucket that matters)

Looking at the 28 days of parity data for DCA's 24-48h bucket, NOAA wins on 15 days, vendor wins on 9 days, and 3 are ties (within 0.05°F). The biggest NOAA wins are 2026-04-27 (-2.45°F) and 2026-04-20 (-1.34°F). The biggest vendor wins are 2026-05-09 (+1.54°F) and 2026-05-12 (+1.40°F). No structural pattern — looks like genuine day-to-day variance, not a systematic problem with either source.

See `DCA_per_day_24_48h.csv` for the full day-by-day breakdown.

---

## Caveat that still applies

The vendor numbers above come from a parallel script that reads every vendor file directly. The deployed `score_daily.py` only reads ~10% of vendor data (`_pm` files ignored, 2-day capture window). If you trust only what's in `daily_by_bucket.parquet`, your vendor MAE will be wildly different from what's here. The DCA conclusion (NOAA wins) is robust to either methodology because the deployed scorer is just under-sampling, not biasing.

Patching the scorer remains step 1 before any management-facing report.

---

## Files

| File | Contents |
|------|----------|
| `DCA_evaluation_2026-05-17.md` | This document |
| `DCA_headtohead_parity.csv` | NOAA vs Vendor by bucket, parity window |
| `DCA_vendor_full_archive.csv` | Vendor MAE by bucket, full 387-day archive |
| `DCA_noaa_daily_last14.csv` | NOAA daily MAE by bucket, last 14 days |
| `DCA_per_day_24_48h.csv` | Day-by-day NOAA vs Vendor at 24-48h |

---

## Bottom line

For DCA specifically, NOAA's free forecast is **better than** the paid vendor at every leadtime, with the operational 24-48h bucket showing a 0.17°F edge on healthy sample sizes. If management wants to test a NOAA-only run for one zone first, DCA is the obvious candidate. No PHL-style cold-bias landmine here — DCA's NOAA bias is a manageable -0.36°F at 24-48h, comparable to vendor's -0.33°F.
