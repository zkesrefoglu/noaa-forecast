"""
Local DuckDB helper for exploring the parquet forecast history.

All data lives as parquet files under ./data/**. This script reads them
through DuckDB so you can write SQL against the full history without any
database setup.

Examples:
    # list snapshots
    python query.py snapshots

    # show latest forecast (flat table)
    python query.py latest

    # show how the forecast for a specific hour drifted across snapshots
    python query.py drift --valid "2026-04-19T18:00:00Z"

    # open an interactive DuckDB shell with views already registered
    python query.py shell
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

DEFAULT_GLOB = "data/**/*.parquet"

SETUP_SQL = f"""
CREATE OR REPLACE VIEW forecast_hourly AS
    SELECT * FROM read_parquet('{{glob}}', union_by_name=true);

CREATE OR REPLACE VIEW snapshots AS
    SELECT
        snapshot_ts_utc,
        location_name,
        COUNT(*)                         AS rows,
        MIN(valid_ts_utc)                AS horizon_start,
        MAX(valid_ts_utc)                AS horizon_end,
        MIN(temp_f)                      AS min_temp_f,
        MAX(temp_f)                      AS max_temp_f
    FROM forecast_hourly
    GROUP BY snapshot_ts_utc, location_name
    ORDER BY snapshot_ts_utc DESC;

CREATE OR REPLACE VIEW latest_forecast AS
    WITH latest AS (
        SELECT location_name, MAX(snapshot_ts_utc) AS snapshot_ts_utc
        FROM forecast_hourly
        GROUP BY location_name
    )
    SELECT f.*
    FROM forecast_hourly f
    JOIN latest l USING (location_name, snapshot_ts_utc)
    ORDER BY f.location_name, f.valid_ts_utc;
"""


def _open(glob: str) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    # Will raise if no files match — user gets a clear error.
    if not any(Path(".").glob(glob.replace("**", "*/*"))) and not list(
        Path(".").rglob("*.parquet")
    ):
        raise SystemExit(
            f"No parquet files found matching {glob!r}. "
            "Run noaa_forecast.py first (or cd to the repo root)."
        )
    con.execute(SETUP_SQL.format(glob=glob))
    return con


def cmd_snapshots(args: argparse.Namespace) -> None:
    con = _open(args.glob)
    print(con.execute("SELECT * FROM snapshots").fetchdf().to_string(index=False))


def cmd_latest(args: argparse.Namespace) -> None:
    con = _open(args.glob)
    df = con.execute(
        """
        SELECT
            location_name,
            valid_ts_utc,
            hour_offset,
            temp_f,
            precip_prob_pct,
            cloud_cover_pct,
            ROUND(wind_speed_mph, 1) AS wind_mph,
            wind_dir_deg
        FROM latest_forecast
        ORDER BY location_name, valid_ts_utc
        """
    ).fetchdf()
    print(df.to_string(index=False))


def cmd_drift(args: argparse.Namespace) -> None:
    con = _open(args.glob)
    df = con.execute(
        """
        SELECT
            snapshot_ts_utc,
            hour_offset,
            temp_f,
            precip_prob_pct,
            cloud_cover_pct,
            ROUND(wind_speed_mph, 1) AS wind_mph
        FROM forecast_hourly
        WHERE valid_ts_utc = ?
        ORDER BY snapshot_ts_utc
        """,
        [args.valid],
    ).fetchdf()
    if df.empty:
        print(f"No forecasts found for valid_ts_utc = {args.valid}")
        return
    print(df.to_string(index=False))


def cmd_shell(args: argparse.Namespace) -> None:
    con = _open(args.glob)
    print("DuckDB views registered: forecast_hourly, snapshots, latest_forecast")
    print("Type SQL ending with ';' (blank line to execute). Ctrl-D to exit.\n")
    buf: list[str] = []
    while True:
        try:
            line = input("duckdb> " if not buf else "    ... ")
        except EOFError:
            print()
            return
        if line.strip() == "" and buf:
            sql = "\n".join(buf)
            buf = []
            try:
                result = con.execute(sql).fetchdf()
                print(result.to_string(index=False))
            except Exception as e:
                print(f"error: {e}")
            continue
        buf.append(line)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--glob", default=DEFAULT_GLOB, help="Parquet glob path")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("snapshots", help="list all snapshots with row counts")
    sub.add_parser("latest", help="show the most recent forecast per location")

    d = sub.add_parser("drift", help="show how forecasts for one hour drifted over time")
    d.add_argument("--valid", required=True, help="Forecast hour in UTC, e.g. 2026-04-19T18:00:00Z")

    sub.add_parser("shell", help="open an interactive DuckDB shell")

    args = p.parse_args()
    dispatch = {
        "snapshots": cmd_snapshots,
        "latest": cmd_latest,
        "drift": cmd_drift,
        "shell": cmd_shell,
    }
    dispatch[args.cmd](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
