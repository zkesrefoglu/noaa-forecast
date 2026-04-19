"""
Daily forecast scoring.

Joins NOAA hourly forecast snapshots against ASOS observed temperatures for a
target date, buckets by leadtime, and computes error metrics per zone.

A single observed value at (zone, valid_ts_utc) gets compared against every
NOAA forecast snapshot that predicted that hour. Each forecast falls into a
leadtime bucket based on (valid_ts_utc - snapshot_ts_utc). This gives us MAE
per (zone, leadtime_bucket, source).

Vendor forecasts can be joined in once `data/vendor/YYYY-MM-DD.csv` exists;
currently this script processes NOAA only. The vendor hook is in
`_load_vendor` -- returns an empty frame until the capture script ships data.

Outputs (both idempotent -- re-runnable for any date):
    data/scores/daily_by_bucket.parquet      (accumulating across dates)
    data/scores/hourly_detail_<date>.parquet (one per date; forecast/observed detail)

Usage:
    python score_daily.py                         # score yesterday UTC
    python score_daily.py --date 2026-04-18       # score a specific date
    python score_daily.py --date 2026-04-18 --zones-csv zones.csv
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import duckdb
import pandas as pd

log = logging.getLogger("score_daily")

# Leadtime buckets in hours. (low, high_exclusive, label).
# 0-6h is "nowcast" (next few hours). 6-24h is "same-op-day".
# 24-48h is what vendor covers on morning capture. 48-168h is 2-7 days out.
BUCKETS: list[tuple[int, int, str]] = [
    (0, 6, "0-6h"),
    (6, 24, "6-24h"),
    (24, 48, "24-48h"),
    (48, 72, "48-72h"),
    (72, 168, "72-168h"),
]


@dataclass
class Zone:
    zone: str
    c_region: int
    icao: str


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Score NOAA (and vendor) vs ASOS truth.")
    p.add_argument(
        "--date",
        type=str,
        default=None,
        help="Target ASOS date YYYY-MM-DD. Defaults to yesterday UTC.",
    )
    p.add_argument(
        "--zones-csv",
        type=Path,
        default=Path("zones.csv"),
        help="zones.csv path (expects columns: zone, c_region, icao).",
    )
    p.add_argument(
        "--data-root",
        type=Path,
        default=Path("data"),
        help="Root of the data tree (contains <ZONE>/, asos/, vendor/, scores/).",
    )
    p.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return p.parse_args()


def _load_zones(zones_csv: Path) -> list[Zone]:
    df = pd.read_csv(zones_csv)
    return [
        Zone(zone=r["zone"], c_region=int(r["c_region"]), icao=r["icao"])
        for _, r in df.iterrows()
    ]


def _bucket_label(leadtime_h: float) -> Optional[str]:
    for lo, hi, label in BUCKETS:
        if lo <= leadtime_h < hi:
            return label
    return None  # outside tracked range (negative or > 168h)


def _load_noaa(
    con: duckdb.DuckDBPyConnection,
    zones: list[Zone],
    target_date: date,
    data_root: Path,
) -> pd.DataFrame:
    """Load all NOAA forecast snapshots that predicted hours on target_date.

    Reads <data_root>/<ZONE>/<YYYY-MM-DD>/*.parquet across a ~8-day window
    around target_date so we capture snapshots made days in advance.
    """
    # A forecast made up to 7 days before target_date could still predict
    # hours on target_date. Read a 9-day window to be safe.
    window_start = target_date - timedelta(days=8)
    window_end = target_date + timedelta(days=1)

    frames: list[pd.DataFrame] = []
    for z in zones:
        zone_dir = data_root / z.zone
        if not zone_dir.exists():
            log.warning("zone dir missing, skipping: %s", zone_dir)
            continue
        paths: list[Path] = []
        d = window_start
        while d <= window_end:
            day_dir = zone_dir / d.isoformat()
            if day_dir.exists():
                paths.extend(sorted(day_dir.glob("*.parquet")))
            d += timedelta(days=1)
        if not paths:
            log.warning("no NOAA snapshots for zone %s in window", z.zone)
            continue
        # DuckDB is faster than pandas for parquet glob reads.
        path_list = [str(p) for p in paths]
        q = f"""
            SELECT
                snapshot_ts_utc,
                valid_ts_utc,
                location_name AS zone,
                temp_f AS forecast_tmpf
            FROM read_parquet({path_list!r})
            WHERE valid_ts_utc::DATE = DATE '{target_date.isoformat()}'
              AND temp_f IS NOT NULL
        """
        df = con.execute(q).df()
        if not df.empty:
            frames.append(df)
            log.info(
                "zone=%s noaa_rows=%d snapshots=%d",
                z.zone,
                len(df),
                df["snapshot_ts_utc"].nunique(),
            )

    if not frames:
        return pd.DataFrame(
            columns=["snapshot_ts_utc", "valid_ts_utc", "zone", "forecast_tmpf"]
        )

    out = pd.concat(frames, ignore_index=True)
    out["snapshot_ts_utc"] = pd.to_datetime(out["snapshot_ts_utc"], utc=True)
    out["valid_ts_utc"] = pd.to_datetime(out["valid_ts_utc"], utc=True)
    out["source"] = "noaa"
    return out


def _load_vendor(
    con: duckdb.DuckDBPyConnection,
    zones: list[Zone],
    target_date: date,
    data_root: Path,
) -> pd.DataFrame:
    """Load vendor forecasts for target_date.

    Expects CSVs at data/vendor/YYYY-MM-DD.csv where the filename is the
    capture date (the morning the java process generated the file). Columns
    match the ops-query dump: D_TEMP, H_TEMP, Q_TEMP, C_REGION, C_WEATHER_SOURCE.

    Stub-safe: returns empty frame if vendor dir doesn't exist yet.
    """
    vendor_dir = data_root / "vendor"
    if not vendor_dir.exists():
        log.info("vendor dir missing; skipping vendor scoring")
        return pd.DataFrame(
            columns=["snapshot_ts_utc", "valid_ts_utc", "zone", "forecast_tmpf"]
        )

    # Look for vendor captures within a 2-day window before target_date.
    # A vendor capture on day D contains forecast hours for D and D+1.
    # Scoring target_date, we care about captures from (target_date - 1) and target_date.
    paths: list[Path] = []
    for offset in (0, 1):
        capture_date = target_date - timedelta(days=offset)
        p = vendor_dir / f"{capture_date.isoformat()}.csv"
        if p.exists():
            paths.append(p)
    if not paths:
        log.info("no vendor captures for window around %s", target_date)
        return pd.DataFrame(
            columns=["snapshot_ts_utc", "valid_ts_utc", "zone", "forecast_tmpf"]
        )

    c_to_zone = {z.c_region: z.zone for z in zones}
    frames: list[pd.DataFrame] = []
    for p in paths:
        capture_date = date.fromisoformat(p.stem)
        # Convention: vendor snapshot_ts_utc = capture_date at 14:00 UTC
        # (roughly 10 AM EDT when user's scheduled task runs). This is only
        # used for leadtime calculation; exact time matters less than the day.
        snapshot_ts = datetime(
            capture_date.year,
            capture_date.month,
            capture_date.day,
            14,
            0,
            tzinfo=timezone.utc,
        )
        df = pd.read_csv(p)
        # Forecast rows only.
        df = df[df["C_WEATHER_SOURCE"] == 4].copy()
        df["zone"] = df["C_REGION"].map(c_to_zone)
        df = df.dropna(subset=["zone"])
        # Build valid_ts_utc from D_TEMP (date) + H_TEMP (hour 1..24, local time).
        # ASSUMPTION: vendor uses local operating-day hours, not UTC. Local time
        # of the scheduling shop appears to be US Eastern. We convert naive
        # (D_TEMP, H_TEMP) from America/New_York to UTC. Flag this: verify with
        # Ziya before trusting the comparison.
        df["D_TEMP_parsed"] = pd.to_datetime(df["D_TEMP"], errors="coerce")
        df["H_TEMP_int"] = pd.to_numeric(df["H_TEMP"], errors="coerce").astype("Int64")
        df = df.dropna(subset=["D_TEMP_parsed", "H_TEMP_int"])
        # H_TEMP=24 means end-of-day; treat as 00:00 next day.
        local_hour = df["H_TEMP_int"] % 24
        next_day_shift = (df["H_TEMP_int"] == 24).astype(int)
        df["valid_ts_local"] = (
            df["D_TEMP_parsed"]
            + pd.to_timedelta(next_day_shift, unit="D")
            + pd.to_timedelta(local_hour, unit="h")
        )
        df["valid_ts_local"] = df["valid_ts_local"].dt.tz_localize(
            "America/New_York", ambiguous="NaT", nonexistent="shift_forward"
        )
        df["valid_ts_utc"] = df["valid_ts_local"].dt.tz_convert("UTC")
        df["snapshot_ts_utc"] = snapshot_ts
        df["forecast_tmpf"] = pd.to_numeric(df["Q_TEMP"], errors="coerce")
        out = df[["snapshot_ts_utc", "valid_ts_utc", "zone", "forecast_tmpf"]].dropna(
            subset=["forecast_tmpf", "valid_ts_utc"]
        )
        out = out[out["valid_ts_utc"].dt.date == target_date]
        if not out.empty:
            frames.append(out)
            log.info(
                "vendor capture %s: %d forecast rows for %s",
                capture_date,
                len(out),
                target_date,
            )

    if not frames:
        return pd.DataFrame(
            columns=["snapshot_ts_utc", "valid_ts_utc", "zone", "forecast_tmpf"]
        )
    out = pd.concat(frames, ignore_index=True)
    out["source"] = "vendor"
    return out


def _load_asos(target_date: date, data_root: Path) -> pd.DataFrame:
    asos_path = data_root / "asos" / f"{target_date.isoformat()}.parquet"
    if not asos_path.exists():
        raise FileNotFoundError(f"ASOS truth missing for {target_date}: {asos_path}")
    df = pd.read_parquet(asos_path)
    df["valid_ts_utc"] = pd.to_datetime(df["valid_ts_utc"], utc=True)
    df = df[["valid_ts_utc", "zone", "tmpf_observed"]].dropna(subset=["tmpf_observed"])
    return df


def _score(
    forecasts: pd.DataFrame, asos: pd.DataFrame, target_date: date
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Join forecasts against truth, compute per-hour and per-bucket metrics."""
    if forecasts.empty:
        log.warning("no forecasts to score")
        return pd.DataFrame(), pd.DataFrame()

    # Belt-and-suspenders: ensure both sides have identical tz-aware dtype
    # before the merge. Empty-frame concat paths above can silently downcast
    # datetime64[us, UTC] back to object and break the merge.
    forecasts = forecasts.copy()
    forecasts["valid_ts_utc"] = pd.to_datetime(forecasts["valid_ts_utc"], utc=True)
    asos = asos.copy()
    asos["valid_ts_utc"] = pd.to_datetime(asos["valid_ts_utc"], utc=True)

    merged = forecasts.merge(asos, on=["valid_ts_utc", "zone"], how="inner")
    if merged.empty:
        log.warning("no overlap between forecasts and ASOS truth")
        return pd.DataFrame(), pd.DataFrame()

    merged["error"] = merged["forecast_tmpf"] - merged["tmpf_observed"]
    merged["abs_error"] = merged["error"].abs()
    merged["leadtime_h"] = (
        (merged["valid_ts_utc"] - merged["snapshot_ts_utc"]).dt.total_seconds() / 3600.0
    )
    merged["bucket"] = merged["leadtime_h"].apply(_bucket_label)
    merged["asos_date"] = target_date.isoformat()

    hourly = merged[
        [
            "asos_date",
            "valid_ts_utc",
            "zone",
            "source",
            "snapshot_ts_utc",
            "leadtime_h",
            "bucket",
            "forecast_tmpf",
            "tmpf_observed",
            "error",
            "abs_error",
        ]
    ].copy()

    # Per-bucket aggregates. Drop rows with no bucket (leadtime out of range).
    agg_input = merged.dropna(subset=["bucket"])
    if agg_input.empty:
        return hourly, pd.DataFrame()

    bucket = (
        agg_input.groupby(["asos_date", "zone", "source", "bucket"], as_index=False)
        .agg(
            n=("abs_error", "size"),
            mae=("abs_error", "mean"),
            bias=("error", "mean"),
            rmse=("error", lambda s: float((s.pow(2).mean()) ** 0.5)),
            max_abs_error=("abs_error", "max"),
        )
    )
    return hourly, bucket


def _upsert_daily_bucket(out_path: Path, new_rows: pd.DataFrame) -> None:
    """Append new_rows to the accumulating daily-bucket parquet, replacing
    any existing (asos_date, zone, source, bucket) keys."""
    if new_rows.empty:
        return
    if out_path.exists():
        existing = pd.read_parquet(out_path)
        key_cols = ["asos_date", "zone", "source", "bucket"]
        new_keys = set(map(tuple, new_rows[key_cols].values.tolist()))
        existing = existing[
            ~existing[key_cols].apply(tuple, axis=1).isin(new_keys)
        ]
        combined = pd.concat([existing, new_rows], ignore_index=True)
    else:
        combined = new_rows
    combined = combined.sort_values(["asos_date", "zone", "source", "bucket"])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    combined.to_parquet(out_path, engine="pyarrow", index=False, compression="snappy")


def main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.date:
        try:
            target_date = date.fromisoformat(args.date)
        except ValueError:
            log.error("invalid --date (expected YYYY-MM-DD): %r", args.date)
            return 2
    else:
        target_date = (datetime.now(timezone.utc) - timedelta(days=1)).date()

    zones = _load_zones(args.zones_csv)
    if not zones:
        log.error("no zones loaded")
        return 1

    con = duckdb.connect()
    try:
        asos = _load_asos(target_date, args.data_root)
    except FileNotFoundError as e:
        log.error("%s", e)
        return 1
    log.info("asos rows=%d zones=%d", len(asos), asos["zone"].nunique())

    noaa = _load_noaa(con, zones, target_date, args.data_root)
    log.info("noaa rows=%d snapshots=%d", len(noaa), noaa["snapshot_ts_utc"].nunique() if not noaa.empty else 0)

    vendor = _load_vendor(con, zones, target_date, args.data_root)
    log.info("vendor rows=%d", len(vendor))

    # Skip empty frames in the concat. An empty DataFrame with object-dtype
    # columns will downcast datetime64[UTC] to object, which breaks the merge.
    frames = [f for f in [noaa, vendor] if not f.empty]
    forecasts = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    hourly, bucket = _score(forecasts, asos, target_date)

    if hourly.empty:
        log.error("no scored rows; aborting write")
        return 1

    scores_dir = args.data_root / "scores"
    scores_dir.mkdir(parents=True, exist_ok=True)

    hourly_path = scores_dir / f"hourly_detail_{target_date.isoformat()}.parquet"
    hourly.to_parquet(hourly_path, engine="pyarrow", index=False, compression="snappy")
    log.info("wrote %d hourly rows -> %s", len(hourly), hourly_path)

    bucket_path = scores_dir / "daily_by_bucket.parquet"
    _upsert_daily_bucket(bucket_path, bucket)
    log.info("wrote %d bucket rows (upserted) -> %s", len(bucket), bucket_path)

    # Quick human-readable summary.
    print(
        f"OK score asos_date={target_date} hourly={len(hourly)} "
        f"bucket={len(bucket)} sources={sorted(forecasts['source'].unique())}"
    )
    if not bucket.empty:
        print("\nBucket summary (MAE F):")
        pivot = bucket.pivot_table(
            index=["zone", "bucket"], columns="source", values="mae"
        )
        print(pivot.to_string(float_format=lambda x: f"{x:.2f}"))

    return 0


if __name__ == "__main__":
    sys.exit(main())
