# noaa-forecast

Hourly snapshots of NOAA's digital DWML forecast for Washington DC. Every hour, GitHub Actions fetches the next ~7 days of hourly predictions and commits one Parquet file to this repo. That gives you a versioned history you can replay to see how the forecast for any given hour drifted over time.

## What gets stored

Per snapshot (roughly 168 rows = 7 days x 24 hours):

| column              | type              | notes                                         |
|---------------------|-------------------|-----------------------------------------------|
| snapshot_ts_utc     | timestamp (UTC)   | when this forecast was fetched                |
| location_name       | string            | slug, e.g. `washington_dc`                    |
| lat, lon            | float             | coordinates used                              |
| valid_ts_local      | timestamp w/ tz   | forecast hour as NOAA returns it              |
| valid_ts_utc        | timestamp (UTC)   | same, normalized to UTC                       |
| hour_offset         | int               | hours from snapshot to forecast hour          |
| temp_f              | float             | hourly temperature                            |
| precip_prob_pct     | float             | probability of precipitation                  |
| cloud_cover_pct     | float             | total cloud amount                            |
| wind_speed_mph      | float             | sustained wind speed (converted from knots)   |
| wind_dir_deg        | float             | wind direction, degrees true (0=N, 90=E)      |

Layout on disk:

```
data/washington_dc/2026-04-18/snapshot_20260418T140700Z.parquet
data/washington_dc/2026-04-18/snapshot_20260418T150700Z.parquet
```

Missing values from NOAA stay as `NULL`, never zero.

## Setup (one time)

```bash
git clone https://github.com/zkesrefoglu/noaa-forecast.git
cd noaa-forecast
pip install -r requirements.txt
```

Enable Actions in the repo settings (it is on by default for public repos). The workflow runs every hour at minute `:07`. You can also trigger a manual run from the Actions tab with "Run workflow".

## Query the history locally

The `query.py` helper registers DuckDB views over all parquet files.

```bash
# list all snapshots collected so far
python query.py snapshots

# dump the most recent forecast
python query.py latest

# see how the forecast for a specific hour drifted over time
python query.py drift --valid "2026-04-19T18:00:00Z"

# interactive SQL shell with views: forecast_hourly, snapshots, latest_forecast
python query.py shell
```

From your own code it's just:

```python
import duckdb
con = duckdb.connect()
con.execute("""
    CREATE VIEW forecast_hourly AS
    SELECT * FROM read_parquet('data/**/*.parquet', union_by_name=true)
""")
con.execute("SELECT * FROM forecast_hourly LIMIT 10").fetchdf()
```

## Add another location later

Edit `.github/workflows/noaa-forecast.yml` and add another step:

```yaml
- name: Fetch NOAA snapshot (NYC)
  run: |
    python noaa_forecast.py \
      --lat 40.7128 \
      --lon -74.0060 \
      --name nyc
```

## Cost / limits

- NOAA's endpoint is free and has no auth. Be nice with the `User-Agent` (already set).
- Public repos get unlimited Actions minutes on GitHub. Each run takes ~30 seconds.
- Storage grows at ~10-20 KB per snapshot, so roughly 100-200 MB per year. Fine for git.

## Why Parquet instead of one DuckDB file

A DuckDB file is a single binary blob. Rewriting it every hour would make `git` explode in size because every write is a full new copy in history. Parquet snapshots are append-only, git-friendly, and DuckDB reads them natively via `read_parquet()`.
