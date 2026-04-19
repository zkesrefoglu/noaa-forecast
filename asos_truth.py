"""
ASOS truth puller.

Fetches observed hourly temperatures from the Iowa Mesonet ASOS archive
for every ICAO station in zones.csv, normalizes multi-per-hour observations
down to one value per top-of-hour UTC (closest observation within a window),
and writes one Parquet file per target date.

Output layout:
    data/asos/YYYY-MM-DD.parquet

Columns:
    valid_ts_utc          : UTC top-of-hour (00:00, 01:00, ..., 23:00)
    zone                  : airport code slug (e.g. "DCA")
    c_region              : vendor-file region number (from zones.csv)
    icao                  : ICAO id actually queried (e.g. "KDCA")
    tmpf_observed         : closest-to-hour observed temperature (Fahrenheit)
    obs_minute_offset     : minutes from top-of-hour to the chosen observation
    n_obs_in_window       : number of observations seen within the window
    source                : fixed string "iowa_mesonet_asos"

Missing values (ASOS 'M' or no obs within window) are preserved as NULL.
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

MESONET_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
USER_AGENT = "zke-noaa-forecast/1.0 (github.com/zkesrefoglu/noaa-forecast)"
REQUEST_TIMEOUT_S = 60
# Max minutes from top-of-hour we accept for the "hourly" value.
# ASOS routine obs are ~:53. Specials can land anywhere. 30 min keeps it honest.
HOURLY_WINDOW_MIN = 30
# Seconds to sleep between consecutive station requests. Iowa Mesonet is a
# free public service; hammering it with 7 requests/sec earns a 429.
INTER_REQUEST_SLEEP_S = 3.0
# Retry configuration for transient failures (429, 5xx).
MAX_RETRIES = 4
BACKOFF_BASE_S = 5.0  # exponential: 5, 10, 20, 40

log = logging.getLogger("asos_truth")


@dataclass
class Zone:
    zone: str
    c_region: int
    icao: str
    wban: Optional[str]
    lat: float
    lon: float


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Pull ASOS observed hourly temps.")
    p.add_argument(
        "--zones-csv",
        type=Path,
        default=Path("zones.csv"),
        help="Path to zones.csv (expects columns: zone, c_region, icao, lat, lon).",
    )
    p.add_argument(
        "--date",
        type=str,
        default=None,
        help="Target date YYYY-MM-DD in UTC. Defaults to yesterday (UTC).",
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        default=Path("data/asos"),
        help="Root directory for parquet output.",
    )
    p.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return p.parse_args()


def _load_zones(zones_csv: Path) -> list[Zone]:
    zones: list[Zone] = []
    with zones_csv.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for i, row in enumerate(reader, start=1):
            try:
                zones.append(
                    Zone(
                        zone=(row["zone"] or "").strip(),
                        c_region=int(row["c_region"]),
                        icao=(row["icao"] or "").strip(),
                        wban=(row.get("wban") or "").strip() or None,
                        lat=float(row["lat"]),
                        lon=float(row["lon"]),
                    )
                )
            except (KeyError, ValueError, TypeError) as e:
                log.error("zones.csv row %d malformed: %s (row=%r)", i, e, row)
    return zones


def _fetch_mesonet_csv(icao: str, target_date: date) -> str:
    """Fetch one UTC-day of ASOS temperature observations for a station.

    Iowa Mesonet's CSV endpoint. Returns obs whose 'valid' timestamp falls
    in [day1, day2) UTC. Retries on 429 and 5xx with exponential backoff.
    """
    next_day = target_date + timedelta(days=1)
    params = {
        "station": icao,
        "data": "tmpf",
        "year1": target_date.year,
        "month1": target_date.month,
        "day1": target_date.day,
        "year2": next_day.year,
        "month2": next_day.month,
        "day2": next_day.day,
        "tz": "Etc/UTC",
        "format": "onlycomma",
        "latlon": "no",
        "elev": "no",
        "missing": "M",
        "trace": "T",
        "direct": "no",
    }

    last_exc: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        log.info(
            "GET station=%s %s (attempt %d/%d)",
            icao,
            target_date.isoformat(),
            attempt,
            MAX_RETRIES,
        )
        try:
            resp = requests.get(
                MESONET_URL,
                params=params,
                headers={"User-Agent": USER_AGENT},
                timeout=REQUEST_TIMEOUT_S,
            )
        except requests.RequestException as e:
            last_exc = e
            log.warning("request error for %s: %s", icao, e)
            if attempt < MAX_RETRIES:
                sleep_s = BACKOFF_BASE_S * (2 ** (attempt - 1))
                time.sleep(sleep_s)
            continue

        if resp.status_code in (429, 500, 502, 503, 504):
            retry_after = resp.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                sleep_s = float(retry_after)
            else:
                sleep_s = BACKOFF_BASE_S * (2 ** (attempt - 1))
            log.warning(
                "station=%s HTTP %d; sleeping %.1fs before retry",
                icao,
                resp.status_code,
                sleep_s,
            )
            last_exc = requests.HTTPError(
                f"{resp.status_code} {resp.reason}", response=resp
            )
            if attempt < MAX_RETRIES:
                time.sleep(sleep_s)
            continue

        resp.raise_for_status()
        return resp.text

    assert last_exc is not None
    raise last_exc


def _parse_mesonet_csv(text: str) -> pd.DataFrame:
    """Parse the Iowa Mesonet onlycomma CSV into a clean DataFrame."""
    df = pd.read_csv(io.StringIO(text))
    if df.empty:
        return df
    df["tmpf"] = pd.to_numeric(df["tmpf"], errors="coerce")
    df["valid_ts_utc"] = pd.to_datetime(df["valid"], utc=True, errors="coerce")
    df = df.dropna(subset=["valid_ts_utc"])
    return df[["station", "valid_ts_utc", "tmpf"]]


def _hourly_from_obs(
    obs: pd.DataFrame,
    target_date: date,
    window_min: int = HOURLY_WINDOW_MIN,
) -> pd.DataFrame:
    """Collapse raw ASOS obs to one row per top-of-hour UTC on target_date.

    For each hour H (00..23), pick the observation whose timestamp is closest
    to H:00 UTC, provided it lies within +/- window_min.
    """
    rows = []
    start = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc)
    for h in range(24):
        top = start + timedelta(hours=h)
        lo = top - timedelta(minutes=window_min)
        hi = top + timedelta(minutes=window_min)
        if obs.empty:
            rows.append(
                {
                    "valid_ts_utc": top,
                    "tmpf_observed": None,
                    "obs_minute_offset": None,
                    "n_obs_in_window": 0,
                }
            )
            continue
        window = obs[(obs["valid_ts_utc"] >= lo) & (obs["valid_ts_utc"] <= hi)]
        n_obs = len(window)
        if n_obs == 0:
            rows.append(
                {
                    "valid_ts_utc": top,
                    "tmpf_observed": None,
                    "obs_minute_offset": None,
                    "n_obs_in_window": 0,
                }
            )
            continue
        window = window.copy()
        window["delta"] = (window["valid_ts_utc"] - top).abs()
        non_null = window.dropna(subset=["tmpf"])
        if non_null.empty:
            rows.append(
                {
                    "valid_ts_utc": top,
                    "tmpf_observed": None,
                    "obs_minute_offset": None,
                    "n_obs_in_window": n_obs,
                }
            )
            continue
        chosen = non_null.sort_values("delta").iloc[0]
        offset_min = int(round(chosen["delta"].total_seconds() / 60.0))
        if chosen["valid_ts_utc"] < top:
            offset_min = -offset_min
        rows.append(
            {
                "valid_ts_utc": top,
                "tmpf_observed": float(chosen["tmpf"]),
                "obs_minute_offset": offset_min,
                "n_obs_in_window": n_obs,
            }
        )
    return pd.DataFrame(rows)


def run(
    zones: list[Zone],
    target_date: date,
    out_dir: Path,
) -> tuple[int, int]:
    """Pull ASOS for every zone, write one combined parquet for the date.

    Returns (n_zones_ok, n_zones_failed).
    """
    frames: list[pd.DataFrame] = []
    successes = 0
    failures = 0

    for idx, z in enumerate(zones):
        if idx > 0:
            time.sleep(INTER_REQUEST_SLEEP_S)
        try:
            text = _fetch_mesonet_csv(z.icao, target_date)
            raw = _parse_mesonet_csv(text)
            hourly = _hourly_from_obs(raw, target_date)
            hourly["zone"] = z.zone
            hourly["c_region"] = z.c_region
            hourly["icao"] = z.icao
            hourly["source"] = "iowa_mesonet_asos"
            n_good = int(hourly["tmpf_observed"].notna().sum())
            log.info(
                "zone=%s icao=%s raw_obs=%d hours_filled=%d/24",
                z.zone,
                z.icao,
                len(raw),
                n_good,
            )
            frames.append(hourly)
            successes += 1
        except Exception:
            log.exception("zone %s (icao=%s) failed", z.zone, z.icao)
            failures += 1

    if not frames:
        log.error("no zones succeeded; nothing to write")
        return (successes, failures)

    combined = pd.concat(frames, ignore_index=True)
    combined = combined[
        [
            "valid_ts_utc",
            "zone",
            "c_region",
            "icao",
            "tmpf_observed",
            "obs_minute_offset",
            "n_obs_in_window",
            "source",
        ]
    ]
    combined["valid_ts_utc"] = pd.to_datetime(combined["valid_ts_utc"], utc=True)

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{target_date.isoformat()}.parquet"
    combined.to_parquet(out_path, engine="pyarrow", index=False, compression="snappy")
    log.info("wrote %d rows -> %s", len(combined), out_path)
    print(
        f"OK asos rows={len(combined)} zones_ok={successes} "
        f"zones_failed={failures} path={out_path}"
    )
    return (successes, failures)


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
        log.error("no zones loaded from %s", args.zones_csv)
        return 1

    successes, failures = run(zones, target_date, args.out_dir)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
