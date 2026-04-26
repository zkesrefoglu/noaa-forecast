#!/usr/bin/env python3
"""
Historical backfill orchestrator.

Walks a date range, pulls ASOS truth from Iowa Mesonet (skipping dates that
already have parquets), then runs score_daily.py against each date. Per-date
failures are logged but do not stop the batch.

Idempotent / resumable:
  - ASOS: skipped if data/asos/<date>.parquet already exists.
  - Scoring: always runs; score_daily.py upserts by (date, zone, source, bucket).

Subprocess output is inherited (streams to terminal), so you can watch ASOS
retries and scoring logs live.

Usage:
    python scripts/historical_backfill.py --start 2025-04-25 --end 2026-04-24
    python scripts/historical_backfill.py --start 2025-04-25 --end 2026-04-24 --dry-run
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

log = logging.getLogger("backfill")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start", required=True, help="YYYY-MM-DD inclusive")
    p.add_argument("--end", required=True, help="YYYY-MM-DD inclusive")
    p.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Path to the noaa-forecast repo root (default: parent of scripts/).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the per-date plan without invoking subprocesses.",
    )
    p.add_argument(
        "--skip-scoring",
        action="store_true",
        help="Only run ASOS pulls. Useful for staging the truth data first.",
    )
    return p.parse_args()


def _date_range(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _run(cmd: list[str], cwd: Path, timeout_s: int = 600) -> tuple[bool, str]:
    """Run subprocess inheriting stdout/stderr; return (ok, summary)."""
    try:
        result = subprocess.run(cmd, cwd=cwd, check=False, timeout=timeout_s)
        if result.returncode == 0:
            return True, "ok"
        return False, f"rc={result.returncode}"
    except subprocess.TimeoutExpired:
        return False, f"timeout (>{timeout_s}s)"
    except Exception as e:  # noqa: BLE001
        return False, f"exception: {e}"


def _fmt_eta(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds/60:.1f}m"
    return f"{seconds/3600:.1f}h"


def main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end)
    except ValueError as e:
        log.error("bad date arg: %s", e)
        return 2

    if start > end:
        log.error("--start (%s) is after --end (%s)", start, end)
        return 2

    repo = args.repo_root.resolve()
    if not (repo / "asos_truth.py").exists():
        log.error("asos_truth.py not found under %s; pass --repo-root.", repo)
        return 2

    asos_dir = repo / "data" / "asos"
    asos_dir.mkdir(parents=True, exist_ok=True)

    py = sys.executable
    asos_script = str(repo / "asos_truth.py")
    score_script = str(repo / "score_daily.py")

    dates = list(_date_range(start, end))
    total = len(dates)
    log.info("backfill plan: %d dates from %s to %s", total, start, end)
    log.info("repo root: %s", repo)
    log.info("python:    %s", py)
    if args.dry_run:
        log.info("DRY RUN -- no subprocesses will be called")
    if args.skip_scoring:
        log.info("scoring will be SKIPPED (only ASOS pulls)")

    started = datetime.now()
    asos_ok = 0
    asos_skipped = 0
    asos_failed = 0
    failed_asos_dates: list[str] = []
    score_ok = 0
    score_failed = 0
    failed_score_dates: list[str] = []

    for i, d in enumerate(dates, 1):
        d_iso = d.isoformat()

        # ETA based on elapsed wall time + remaining count
        elapsed = (datetime.now() - started).total_seconds()
        if i > 1:
            avg = elapsed / (i - 1)
            eta = _fmt_eta(avg * (total - i + 1))
            eta_str = f" | ETA {eta}"
        else:
            eta_str = ""

        # === ASOS ===
        asos_path = asos_dir / f"{d_iso}.parquet"
        asos_status: str
        if asos_path.exists():
            asos_skipped += 1
            asos_status = "skip"
        else:
            log.info("[%d/%d] %s pulling ASOS%s", i, total, d_iso, eta_str)
            if args.dry_run:
                asos_status = "dry"
            else:
                ok, msg = _run([py, asos_script, "--date", d_iso], cwd=repo, timeout_s=300)
                if ok:
                    asos_ok += 1
                    asos_status = "ok"
                else:
                    asos_failed += 1
                    failed_asos_dates.append(d_iso)
                    log.warning("    ASOS FAILED for %s: %s", d_iso, msg)
                    asos_status = "fail"

        # === Scoring ===
        # If ASOS failed, scoring will hard-fail (truth missing). Skip it.
        if asos_status == "fail":
            continue
        if args.skip_scoring:
            continue
        if args.dry_run:
            log.info("[%d/%d] %s would score%s", i, total, d_iso, eta_str)
            continue

        # asos_status is "ok" or "skip" -- truth is available
        log.info("[%d/%d] %s scoring", i, total, d_iso)
        ok, msg = _run([py, score_script, "--date", d_iso], cwd=repo, timeout_s=120)
        if ok:
            score_ok += 1
        else:
            score_failed += 1
            failed_score_dates.append(d_iso)
            log.warning("    SCORE FAILED for %s: %s", d_iso, msg)

    elapsed = (datetime.now() - started).total_seconds()
    log.info("=" * 60)
    log.info("BACKFILL SUMMARY")
    log.info("  total dates:       %d", total)
    log.info("  asos pulled:       %d", asos_ok)
    log.info("  asos pre-existing: %d", asos_skipped)
    log.info("  asos failed:       %d", asos_failed)
    log.info("  scored ok:         %d", score_ok)
    log.info("  scored failed:     %d", score_failed)
    log.info("  elapsed: %s", _fmt_eta(elapsed))
    if failed_asos_dates:
        log.warning("  asos-failed dates: %s", ", ".join(failed_asos_dates[:10])
                    + (f" ... (+{len(failed_asos_dates)-10} more)" if len(failed_asos_dates) > 10 else ""))
    if failed_score_dates:
        log.warning("  score-failed dates: %s", ", ".join(failed_score_dates[:10])
                    + (f" ... (+{len(failed_score_dates)-10} more)" if len(failed_score_dates) > 10 else ""))
    return 0 if (asos_failed == 0 and score_failed == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
