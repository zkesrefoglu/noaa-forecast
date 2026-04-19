"""
Generate a single-file interactive HTML dashboard from the parquet history.

Reads every snapshot in ./data/**/*.parquet, computes latest forecast, drift
series, and stability stats via DuckDB, then writes ./docs/index.html.

Run after git pull (or after a cron snapshot):
    python build_dashboard.py

Output: docs/index.html (open in any browser). Data is embedded inline as
JSON; charts are drawn by Plotly loaded from a CDN.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

DATA_GLOB = "data/**/*.parquet"
OUT_PATH = Path("docs/index.html")
TEMPLATE_PATH = Path("dashboard_template.html")


def _jsonable(df: pd.DataFrame) -> list[dict]:
    """Convert a DataFrame to JSON-friendly records (ISO timestamps, no NaN)."""
    out = df.copy()
    for c in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[c]):
            out[c] = out[c].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    # Replace NaN with None so json.dumps gives us null, not NaN (invalid JSON).
    return json.loads(out.to_json(orient="records"))


def build_payload() -> dict:
    con = duckdb.connect()
    con.execute(
        f"CREATE VIEW fh AS "
        f"SELECT * FROM read_parquet('{DATA_GLOB}', union_by_name=true)"
    )

    # --- metadata ---
    meta_row = con.execute(
        """
        SELECT
            COUNT(DISTINCT snapshot_ts_utc)         AS n_snapshots,
            MIN(snapshot_ts_utc)                    AS first_snap,
            MAX(snapshot_ts_utc)                    AS last_snap,
            COUNT(DISTINCT location_name)           AS n_locations,
            ANY_VALUE(location_name)                AS location_name,
            ANY_VALUE(lat)                          AS lat,
            ANY_VALUE(lon)                          AS lon
        FROM fh
        """
    ).fetchone()
    (n_snapshots, first_snap, last_snap, n_locations,
     location_name, lat, lon) = meta_row

    # Cron cadence: median minutes between consecutive snapshots.
    gap_df = con.execute(
        """
        WITH ts AS (
            SELECT DISTINCT snapshot_ts_utc FROM fh ORDER BY snapshot_ts_utc
        )
        SELECT DATE_DIFF(
            'minute',
            LAG(snapshot_ts_utc) OVER (ORDER BY snapshot_ts_utc),
            snapshot_ts_utc
        ) AS gap_min
        FROM ts
        """
    ).fetchdf().dropna()
    median_gap = float(gap_df["gap_min"].median()) if len(gap_df) else None

    # --- latest forecast (hero chart) ---
    latest_df = con.execute(
        """
        SELECT
            valid_ts_utc,
            hour_offset,
            temp_f,
            precip_prob_pct,
            cloud_cover_pct,
            wind_speed_mph,
            wind_dir_deg
        FROM fh
        WHERE snapshot_ts_utc = (SELECT MAX(snapshot_ts_utc) FROM fh)
        ORDER BY valid_ts_utc
        """
    ).fetchdf()

    # --- spaghetti: every snapshot's temperature curve ---
    spaghetti_df = con.execute(
        """
        SELECT
            snapshot_ts_utc,
            valid_ts_utc,
            temp_f,
            precip_prob_pct
        FROM fh
        ORDER BY snapshot_ts_utc, valid_ts_utc
        """
    ).fetchdf()

    # --- drift heatmap: for each valid_ts_utc, temp prediction per snapshot.
    # Rows = forecast hour (valid_ts_utc), Cols = snapshot_ts_utc, Values = temp.
    heatmap_df = con.execute(
        """
        SELECT snapshot_ts_utc, valid_ts_utc, temp_f
        FROM fh
        WHERE valid_ts_utc IN (
            -- only valid hours that have >=2 snapshots, else heatmap is sparse
            SELECT valid_ts_utc FROM fh
            GROUP BY valid_ts_utc
            HAVING COUNT(DISTINCT snapshot_ts_utc) >= 2
        )
        ORDER BY valid_ts_utc, snapshot_ts_utc
        """
    ).fetchdf()

    # --- stability: forecast spread (max-min) vs leadtime ---
    # For each valid_ts_utc, compute spread across snapshots. Bucket the result
    # by the EARLIEST hour_offset at which we first predicted it (the "leadtime
    # horizon" — how far in advance the oldest prediction was made).
    stability_df = con.execute(
        """
        WITH per_valid AS (
            SELECT
                valid_ts_utc,
                MAX(hour_offset)                              AS max_leadtime,
                MAX(temp_f) - MIN(temp_f)                     AS temp_spread,
                MAX(precip_prob_pct) - MIN(precip_prob_pct)   AS precip_spread,
                MAX(wind_speed_mph) - MIN(wind_speed_mph)     AS wind_spread,
                STDDEV(temp_f)                                AS temp_std,
                COUNT(DISTINCT snapshot_ts_utc)               AS n_snaps
            FROM fh
            GROUP BY valid_ts_utc
            HAVING COUNT(DISTINCT snapshot_ts_utc) >= 2
        )
        SELECT
            max_leadtime,
            temp_spread,
            precip_spread,
            wind_spread,
            temp_std,
            n_snaps
        FROM per_valid
        ORDER BY max_leadtime
        """
    ).fetchdf()

    # Also a bucketed view for the line chart: avg spread per 6-hour leadtime bucket.
    stability_bucketed = con.execute(
        """
        WITH per_valid AS (
            SELECT
                valid_ts_utc,
                MAX(hour_offset)                              AS max_leadtime,
                MAX(temp_f) - MIN(temp_f)                     AS temp_spread,
                MAX(precip_prob_pct) - MIN(precip_prob_pct)   AS precip_spread,
                MAX(wind_speed_mph) - MIN(wind_speed_mph)     AS wind_spread
            FROM fh
            GROUP BY valid_ts_utc
            HAVING COUNT(DISTINCT snapshot_ts_utc) >= 2
        )
        SELECT
            CAST(FLOOR(max_leadtime / 6.0) * 6 AS INTEGER) AS leadtime_bucket,
            AVG(temp_spread)                               AS avg_temp_spread,
            AVG(precip_spread)                             AS avg_precip_spread,
            AVG(wind_spread)                               AS avg_wind_spread,
            COUNT(*)                                       AS n
        FROM per_valid
        WHERE max_leadtime >= 0
        GROUP BY leadtime_bucket
        ORDER BY leadtime_bucket
        """
    ).fetchdf()

    # --- quick headline stats for the header tiles ---
    current = latest_df.iloc[0] if len(latest_df) else None
    headline = None
    if current is not None:
        headline = {
            "temp_f": float(current["temp_f"]) if pd.notna(current["temp_f"]) else None,
            "precip_prob_pct": float(current["precip_prob_pct"]) if pd.notna(current["precip_prob_pct"]) else None,
            "cloud_cover_pct": float(current["cloud_cover_pct"]) if pd.notna(current["cloud_cover_pct"]) else None,
            "wind_speed_mph": float(current["wind_speed_mph"]) if pd.notna(current["wind_speed_mph"]) else None,
            "wind_dir_deg": float(current["wind_dir_deg"]) if pd.notna(current["wind_dir_deg"]) else None,
        }

    payload = {
        "built_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "meta": {
            "n_snapshots": int(n_snapshots or 0),
            "first_snap": first_snap.strftime("%Y-%m-%dT%H:%M:%SZ") if first_snap else None,
            "last_snap": last_snap.strftime("%Y-%m-%dT%H:%M:%SZ") if last_snap else None,
            "n_locations": int(n_locations or 0),
            "location_name": location_name,
            "lat": float(lat) if lat is not None else None,
            "lon": float(lon) if lon is not None else None,
            "median_gap_min": median_gap,
        },
        "headline": headline,
        "latest": _jsonable(latest_df),
        "spaghetti": _jsonable(spaghetti_df),
        "heatmap": _jsonable(heatmap_df),
        "stability": _jsonable(stability_df),
        "stability_bucketed": _jsonable(stability_bucketed),
    }
    return payload


def render_html(payload: dict) -> str:
    tpl = TEMPLATE_PATH.read_text(encoding="utf-8")
    data_json = json.dumps(payload, separators=(",", ":"))
    return tpl.replace("__DATA_PAYLOAD__", data_json)


def main() -> int:
    if not any(Path(".").rglob("*.parquet")):
        print("No parquet files found under ./data. Run noaa_forecast.py first.", file=sys.stderr)
        return 1
    payload = build_payload()
    html = render_html(payload)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(html, encoding="utf-8")
    print(
        f"OK wrote {OUT_PATH} "
        f"({len(html)/1024:.1f} KB, {payload['meta']['n_snapshots']} snapshots)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
