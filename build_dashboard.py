"""
Generate a single-file interactive HTML dashboard from the parquet history.

Multi-zone: reads every snapshot in ./data/**/*.parquet, computes latest
forecast, drift series, and stability stats per zone via DuckDB, then writes
./docs/index.html with a zone dropdown so all seven zones are navigable from
one page.

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

# Anything under these prefixes is not a per-zone snapshot parquet.
# `data/asos/` is ASOS truth, `data/scores/` is the scorer's output. Including
# them in the union_by_name view would blow up because their schemas don't
# match the NOAA snapshots.
EXCLUDE_PREFIXES = ("data/asos/", "data/scores/")


def _jsonable(df: pd.DataFrame) -> list[dict]:
    """Convert a DataFrame to JSON-friendly records (ISO timestamps, no NaN)."""
    out = df.copy()
    for c in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[c]):
            out[c] = out[c].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    # Replace NaN with None so json.dumps gives us null, not NaN (invalid JSON).
    return json.loads(out.to_json(orient="records"))


def _zone_payload(con: duckdb.DuckDBPyConnection, zone: str) -> dict:
    """Compute the full payload for a single zone. Mirrors the original
    single-zone build, but every query filters by location_name."""

    meta_row = con.execute(
        """
        SELECT
            COUNT(DISTINCT snapshot_ts_utc) AS n_snapshots,
            MIN(snapshot_ts_utc)            AS first_snap,
            MAX(snapshot_ts_utc)            AS last_snap,
            ANY_VALUE(lat)                  AS lat,
            ANY_VALUE(lon)                  AS lon
        FROM fh
        WHERE location_name = ?
        """,
        [zone],
    ).fetchone()
    n_snapshots, first_snap, last_snap, lat, lon = meta_row

    gap_df = con.execute(
        """
        WITH ts AS (
            SELECT DISTINCT snapshot_ts_utc FROM fh
            WHERE location_name = ?
            ORDER BY snapshot_ts_utc
        )
        SELECT DATE_DIFF(
            'minute',
            LAG(snapshot_ts_utc) OVER (ORDER BY snapshot_ts_utc),
            snapshot_ts_utc
        ) AS gap_min
        FROM ts
        """,
        [zone],
    ).fetchdf().dropna()
    median_gap = float(gap_df["gap_min"].median()) if len(gap_df) else None

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
        WHERE location_name = ?
          AND snapshot_ts_utc = (
              SELECT MAX(snapshot_ts_utc) FROM fh WHERE location_name = ?
          )
        ORDER BY valid_ts_utc
        """,
        [zone, zone],
    ).fetchdf()

    spaghetti_df = con.execute(
        """
        SELECT
            snapshot_ts_utc,
            valid_ts_utc,
            temp_f,
            precip_prob_pct
        FROM fh
        WHERE location_name = ?
        ORDER BY snapshot_ts_utc, valid_ts_utc
        """,
        [zone],
    ).fetchdf()

    heatmap_df = con.execute(
        """
        SELECT snapshot_ts_utc, valid_ts_utc, temp_f
        FROM fh
        WHERE location_name = ?
          AND valid_ts_utc IN (
              SELECT valid_ts_utc FROM fh
              WHERE location_name = ?
              GROUP BY valid_ts_utc
              HAVING COUNT(DISTINCT snapshot_ts_utc) >= 2
          )
        ORDER BY valid_ts_utc, snapshot_ts_utc
        """,
        [zone, zone],
    ).fetchdf()

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
            WHERE location_name = ?
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
        """,
        [zone],
    ).fetchdf()

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
            WHERE location_name = ?
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
        """,
        [zone],
    ).fetchdf()

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

    return {
        "zone": zone,
        "meta": {
            "n_snapshots": int(n_snapshots or 0),
            "first_snap": first_snap.strftime("%Y-%m-%dT%H:%M:%SZ") if first_snap else None,
            "last_snap": last_snap.strftime("%Y-%m-%dT%H:%M:%SZ") if last_snap else None,
            "location_name": zone,
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


def build_payload() -> dict:
    # Enumerate parquets on disk and exclude ASOS / scores — only feed the view
    # the NOAA snapshots, whose schema is uniform.
    all_parquets = [p.as_posix() for p in Path(".").rglob("*.parquet")]
    zone_parquets = [
        p for p in all_parquets
        if not any(p.startswith(prefix) for prefix in EXCLUDE_PREFIXES)
    ]
    if not zone_parquets:
        raise RuntimeError("No NOAA snapshot parquets found under ./data.")

    con = duckdb.connect()
    # DuckDB DDL can't bind parameters, so inline the filtered list as a SQL
    # array literal. Paths come from rglob on our own repo — not user input —
    # so this is safe, but we still defensively reject anything with a quote.
    for p in zone_parquets:
        if "'" in p:
            raise RuntimeError(f"refusing to inline parquet path with quote: {p!r}")
    sql_list = "[" + ", ".join(f"'{p}'" for p in zone_parquets) + "]"
    con.execute(
        f"CREATE VIEW fh AS "
        f"SELECT * FROM read_parquet({sql_list}, union_by_name=true)"
    )

    # Globally unique zones, ordered deterministically.
    zones_df = con.execute(
        "SELECT DISTINCT location_name FROM fh "
        "WHERE location_name IS NOT NULL ORDER BY 1"
    ).fetchdf()
    zones = zones_df["location_name"].tolist()
    if not zones:
        raise RuntimeError("No zones found in snapshot parquets.")

    by_zone = {z: _zone_payload(con, z) for z in zones}

    # Pipeline-wide meta (shown in the footer / global header).
    global_meta = con.execute(
        """
        SELECT
            COUNT(DISTINCT snapshot_ts_utc) AS n_snapshots_total,
            COUNT(DISTINCT location_name)   AS n_zones,
            MIN(snapshot_ts_utc)            AS first_snap,
            MAX(snapshot_ts_utc)            AS last_snap
        FROM fh
        """
    ).fetchone()
    n_snapshots_total, n_zones, first_snap_g, last_snap_g = global_meta

    return {
        "built_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "zones": zones,
        "default_zone": "DCA" if "DCA" in zones else zones[0],
        "global": {
            "n_snapshots_total": int(n_snapshots_total or 0),
            "n_zones": int(n_zones or 0),
            "first_snap": first_snap_g.strftime("%Y-%m-%dT%H:%M:%SZ") if first_snap_g else None,
            "last_snap": last_snap_g.strftime("%Y-%m-%dT%H:%M:%SZ") if last_snap_g else None,
        },
        "by_zone": by_zone,
    }


def render_html(payload: dict) -> str:
    tpl = TEMPLATE_PATH.read_text(encoding="utf-8")
    data_json = json.dumps(payload, separators=(",", ":"))
    return tpl.replace("__DATA_PAYLOAD__", data_json)


def main() -> int:
    try:
        payload = build_payload()
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    html = render_html(payload)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(html, encoding="utf-8")
    print(
        f"OK wrote {OUT_PATH} "
        f"({len(html)/1024:.1f} KB, "
        f"{payload['global']['n_snapshots_total']} snapshots, "
        f"{payload['global']['n_zones']} zones)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
