"""
NOAA hourly forecast snapshot fetcher.

Pulls the digital DWML from forecast.weather.gov for a given lat/lon,
parses hourly temperature, precipitation probability, cloud cover, and
wind speed/direction, and writes one Parquet snapshot per run.

Output layout:
    data/<name>/<YYYY-MM-DD>/snapshot_<YYYYMMDD>T<HHMMSS>Z.parquet

Each row represents one forecast hour. Columns:
    snapshot_ts_utc       : when this forecast was fetched (UTC)
    location_name         : slug for the location (e.g. "washington_dc")
    lat, lon              : coordinates used
    valid_ts_local        : forecast timestamp as given by NOAA (location-local)
    valid_ts_utc          : forecast timestamp converted to UTC
    hour_offset           : hours from snapshot_ts_utc to valid_ts_utc
    temp_f                : hourly temperature (F)
    precip_prob_pct       : probability of precipitation (%)
    cloud_cover_pct       : total cloud amount (%)
    wind_speed_mph        : sustained wind speed (mph; converted from knots if needed)
    wind_dir_deg          : wind direction (degrees true, 0=N, 90=E)

Missing values from NOAA are preserved as NULL (not zero).
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
from lxml import etree

NOAA_URL = "https://forecast.weather.gov/MapClick.php"
USER_AGENT = "zke-noaa-forecast/1.0 (github.com/zkesrefoglu/noaa-forecast)"
KNOTS_TO_MPH = 1.15077945
REQUEST_TIMEOUT_S = 30

log = logging.getLogger("noaa_forecast")


@dataclass
class FetchResult:
    rows: int
    out_path: Path


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Fetch one NOAA hourly-forecast snapshot.")
    p.add_argument("--lat", type=float, default=38.9087, help="Latitude (default: DC)")
    p.add_argument("--lon", type=float, default=-77.0189, help="Longitude (default: DC)")
    p.add_argument(
        "--name",
        type=str,
        default="washington_dc",
        help="Location slug used for the output path.",
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        default=Path("data"),
        help="Root directory for parquet output.",
    )
    p.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return p.parse_args()


def _fetch_xml(lat: float, lon: float) -> bytes:
    params = {"lat": lat, "lon": lon, "FcstType": "digitalDWML"}
    log.info("GET %s %s", NOAA_URL, params)
    resp = requests.get(
        NOAA_URL,
        params=params,
        headers={"User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.content


def _parse_time_layouts(root) -> dict[str, list[datetime]]:
    """Return { layout-key: [datetime, ...] }. Timestamps are timezone-aware."""
    layouts: dict[str, list[datetime]] = {}
    for tl in root.iter("time-layout"):
        key_el = tl.find("layout-key")
        if key_el is None or not key_el.text:
            continue
        key = key_el.text.strip()
        times: list[datetime] = []
        for svt in tl.iter("start-valid-time"):
            if svt.text:
                # e.g. "2026-04-18T14:00:00-04:00"
                try:
                    times.append(datetime.fromisoformat(svt.text.strip()))
                except ValueError:
                    log.warning("Unparseable start-valid-time: %r", svt.text)
        layouts[key] = times
    log.debug("parsed %d time-layouts", len(layouts))
    return layouts


def _read_param_values(
    params_root,
    tag: str,
    type_filter: Optional[str] = None,
) -> Optional[tuple[str, list[Optional[float]], Optional[str]]]:
    """
    Find the first <tag> under <parameters> (optionally matching type=...).
    Returns (time_layout_key, values, units) or None if not found.
    """
    for el in params_root.iter(tag):
        if type_filter is not None and el.get("type") != type_filter:
            continue
        layout_key = el.get("time-layout")
        units = el.get("units")
        values: list[Optional[float]] = []
        for v in el.findall("value"):
            text = (v.text or "").strip()
            if not text:
                values.append(None)
                continue
            try:
                values.append(float(text))
            except ValueError:
                values.append(None)
        return layout_key, values, units
    return None


def _align(
    layouts: dict[str, list[datetime]],
    layout_key: Optional[str],
    values: list[Optional[float]],
) -> dict[datetime, Optional[float]]:
    """Pair timestamps from a layout with a series of values."""
    if not layout_key:
        return {}
    times = layouts.get(layout_key, [])
    if len(times) != len(values):
        log.warning(
            "length mismatch for layout %s: %d times vs %d values (truncating to min)",
            layout_key,
            len(times),
            len(values),
        )
    n = min(len(times), len(values))
    return {times[i]: values[i] for i in range(n)}


def parse_dwml(xml_bytes: bytes) -> pd.DataFrame:
    """Return a DataFrame keyed on forecast timestamp with all tracked params."""
    root = etree.fromstring(xml_bytes)
    layouts = _parse_time_layouts(root)

    params_root = root.find(".//parameters")
    if params_root is None:
        raise ValueError("No <parameters> block found in DWML response")

    # Pull each parameter we care about.
    specs = [
        ("temp_f", "temperature", "hourly", None),
        ("precip_prob_pct", "probability-of-precipitation", "floating", None),
        ("cloud_cover_pct", "cloud-amount", "total", None),
        ("wind_speed_raw", "wind-speed", "sustained", "wind_speed_units"),
        ("wind_dir_deg", "direction", "wind", None),
    ]

    series: dict[str, dict[datetime, Optional[float]]] = {}
    units_map: dict[str, Optional[str]] = {}

    for col, tag, type_filter, units_col in specs:
        found = _read_param_values(params_root, tag, type_filter)
        if found is None:
            log.warning("parameter not found: <%s type=%r>", tag, type_filter)
            series[col] = {}
            continue
        layout_key, values, units = found
        series[col] = _align(layouts, layout_key, values)
        if units_col:
            units_map[units_col] = units
        log.debug(
            "%s: %d values, layout=%s units=%s", col, len(values), layout_key, units
        )

    # Union of all timestamps across parameters (some series can be shorter).
    all_times = sorted(
        {t for s in series.values() for t in s.keys()}
    )
    if not all_times:
        raise ValueError("No forecast timestamps extracted")

    rows = []
    for ts in all_times:
        rows.append(
            {
                "valid_ts_local": ts,
                "temp_f": series["temp_f"].get(ts),
                "precip_prob_pct": series["precip_prob_pct"].get(ts),
                "cloud_cover_pct": series["cloud_cover_pct"].get(ts),
                "wind_speed_raw": series["wind_speed_raw"].get(ts),
                "wind_dir_deg": series["wind_dir_deg"].get(ts),
            }
        )
    df = pd.DataFrame(rows)

    # NOAA's digitalDWML endpoint omits the `units=` attribute but returns
    # wind-speed values that match the HTML tabular page's "Surface Wind (mph)"
    # column exactly (verified 2026-04 against DC forecast). Treat as mph.
    # If NOAA ever starts populating `units=` with knots or m/s, convert.
    wind_units = (units_map.get("wind_speed_units") or "").lower()
    if "knot" in wind_units:
        df["wind_speed_mph"] = df["wind_speed_raw"] * KNOTS_TO_MPH
    elif "m/s" in wind_units or "meter" in wind_units:
        df["wind_speed_mph"] = df["wind_speed_raw"] * 2.23694
    else:
        # Empty or mph / miles / unrecognized — use as-is (empirically correct).
        if wind_units and "mph" not in wind_units and "mile" not in wind_units:
            log.warning(
                "unrecognized wind_speed units %r; assuming mph", wind_units
            )
        df["wind_speed_mph"] = df["wind_speed_raw"]
    df = df.drop(columns=["wind_speed_raw"])

    return df


def _annotate(
    df: pd.DataFrame,
    snapshot_ts_utc: datetime,
    location_name: str,
    lat: float,
    lon: float,
) -> pd.DataFrame:
    df = df.copy()
    df["snapshot_ts_utc"] = snapshot_ts_utc
    df["location_name"] = location_name
    df["lat"] = lat
    df["lon"] = lon
    # UTC version of valid timestamp.
    df["valid_ts_utc"] = pd.to_datetime(df["valid_ts_local"], utc=True)
    df["hour_offset"] = (
        (df["valid_ts_utc"] - pd.Timestamp(snapshot_ts_utc)).dt.total_seconds() / 3600.0
    ).round().astype("Int64")

    # Column order for readability.
    return df[
        [
            "snapshot_ts_utc",
            "location_name",
            "lat",
            "lon",
            "valid_ts_local",
            "valid_ts_utc",
            "hour_offset",
            "temp_f",
            "precip_prob_pct",
            "cloud_cover_pct",
            "wind_speed_mph",
            "wind_dir_deg",
        ]
    ]


def _write_parquet(
    df: pd.DataFrame,
    out_root: Path,
    location_name: str,
    snapshot_ts_utc: datetime,
) -> Path:
    day_dir = out_root / location_name / snapshot_ts_utc.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    fname = f"snapshot_{snapshot_ts_utc.strftime('%Y%m%dT%H%M%SZ')}.parquet"
    out_path = day_dir / fname

    # Make timestamp dtypes parquet-friendly (UTC, microseconds).
    df = df.copy()
    df["snapshot_ts_utc"] = pd.to_datetime(df["snapshot_ts_utc"], utc=True)
    df["valid_ts_utc"] = pd.to_datetime(df["valid_ts_utc"], utc=True)
    # valid_ts_local keeps its tz-aware info; pyarrow handles it.

    df.to_parquet(out_path, engine="pyarrow", index=False, compression="snappy")
    return out_path


def run(
    lat: float,
    lon: float,
    name: str,
    out_dir: Path,
) -> FetchResult:
    snapshot_ts_utc = datetime.now(timezone.utc).replace(microsecond=0)
    xml_bytes = _fetch_xml(lat, lon)
    df = parse_dwml(xml_bytes)
    df = _annotate(df, snapshot_ts_utc, name, lat, lon)
    out_path = _write_parquet(df, out_dir, name, snapshot_ts_utc)
    log.info("wrote %d rows -> %s", len(df), out_path)
    return FetchResult(rows=len(df), out_path=out_path)


def main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        result = run(args.lat, args.lon, args.name, args.out_dir)
    except Exception:
        log.exception("snapshot failed")
        return 1
    print(f"OK rows={result.rows} path={result.out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
