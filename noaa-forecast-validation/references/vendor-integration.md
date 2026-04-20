# Vendor Integration — State of Play

The vendor capture is the last unfinished piece of the pipeline. This document tracks exactly what's done, what's blocked, and how to finish once the blocker clears.

## What the vendor feed is

The vendor (not named here for obvious reasons) produces a daily forecast CSV consumed by Ziya's company's Excel load-scheduling model. A Java process on the work machine writes it to an internal network share every morning at ~8:30 AM local. The filename is `ops-query-in-out_hourly_temp.csv`. The Java process does NOT retain history — today's file overwrites yesterday's.

**Critical:** if nobody captures today's file before tomorrow's 8:30 AM, today's vendor forecasts are lost forever.

## What's done

- **PowerShell capture script:** `scripts/capture_vendor.ps1` in the repo. Written, committed, tested in isolation.
- **Script behavior:**
  - Copies the file from the UNC path to `data/vendor/<capture_date>.csv` in the local clone.
  - Capture date = the source file's `LastWriteTime.Date` (not "today") — protects against a late-running scheduled task tagging the wrong day.
  - Short-circuits with a "nothing to do" message if the same content is already captured (SHA256 hash compare).
  - `git pull` → `git add` → `git commit` → `git push`.
  - Errors loudly if the source file isn't readable.

- **Scorer hook:** `score_daily.py._load_vendor` reads `data/vendor/<date>.csv`, filters to forecast rows (`C_WEATHER_SOURCE=4`), converts `(D_TEMP, H_TEMP)` from America/New_York to UTC, maps `C_REGION` to zone name via `zones.csv`. Handles missing vendor dir gracefully (returns empty frame, scoring proceeds with NOAA-only).

## What's blocked

**The UNC path in `scripts/capture_vendor.ps1` line 23 is a placeholder.** Ziya has to provide the actual path from the work machine. The current placeholder:

```powershell
$VendorFile = "\\FILL_IN_SERVER\fill\in\path\ops-query-in-out_hourly_temp.csv"
```

Until this is filled in, the Windows Task Scheduler job can't be installed and no vendor data lands.

## Finishing the wiring (once Ziya provides the path)

1. **Ziya provides the UNC path**, e.g., `\\corp-fs01\ops\weather\ops-query-in-out_hourly_temp.csv`.
2. **Update the script:** edit line 23 of `scripts/capture_vendor.ps1`. Commit + push.
3. **Verify manually first** before installing as a scheduled task:
   ```powershell
   cd C:\Users\Ziya\Documents\GitHub\noaa-forecast
   .\scripts\capture_vendor.ps1
   ```
   Expected: a file appears at `data/vendor/<today>.csv`, a commit is pushed to the repo, and the GitHub Actions score-daily run at 09:00 UTC tomorrow will pick it up.
4. **Install the scheduled task** (one-time):
   ```powershell
   $action = New-ScheduledTaskAction -Execute "powershell.exe" `
     -Argument "-NoProfile -ExecutionPolicy Bypass -File `"C:\Users\Ziya\Documents\GitHub\noaa-forecast\scripts\capture_vendor.ps1`""
   $trigger = New-ScheduledTaskTrigger -Daily -At 10:00AM
   $principal = New-ScheduledTaskPrincipal -UserId "$env:USERNAME" -LogonType Interactive -RunLevel Limited
   $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
   Register-ScheduledTask -TaskName "NOAA-Forecast-Vendor-Capture" `
     -Action $action -Trigger $trigger -Principal $principal -Settings $settings
   ```
   Note: "StartWhenAvailable" means if the machine is off at 10:00 AM, the task runs the next time the machine wakes up — safer than "skip silently."
5. **Wait a day.** The next morning's run at 10 AM local should capture and push. Verify by looking at `data/vendor/<yesterday_or_today>.csv` on GitHub.

## Things to expect once vendor data flows

- `score-daily.yml` will produce per-zone per-bucket MAE for both NOAA and vendor, side by side.
- The first full week (7 vendor captures, 7 ASOS days, 7 score runs) is when real numbers start to mean something.
- Vendor tends to be strong at 24-48h (that's what it's sold for). NOAA should be competitive or better at 0-6h (public models do well at nowcasting) and wider-variance at long leadtimes.
- If NOAA beats vendor at 24-48h by a meaningful margin (say, 0.5°F MAE consistently), that's the headline for the management report.

## Don't do this

- **Don't re-architect around the Java process.** It exists, it works, it generates the file reliably. The only question is how to capture the file before it gets overwritten. PowerShell + scheduled task is the simplest answer and it's done.
- **Don't store the UNC path in the repo as a committed secret.** The placeholder pattern is intentional — when Ziya fills it in, he can commit the real path (it's an internal share, not a secret). If the path IS sensitive, make it an env var and read it in the script.
- **Don't rely on the scheduled task running on a schedule that doesn't match when the work machine is actually on.** 10 AM local was chosen specifically because it's mid-morning, post-java-dump, and Ziya's machine is definitely on by then.
- **Don't backfill vendor from a DB dump.** Ziya confirmed: the Java process does NOT save history, and there's no Oracle-side archive of this file. What's captured going forward is all we'll ever have. Missed days are gone.

## Open questions (ask Ziya when relevant)

- Does the vendor file format ever change mid-year? If columns shift, `_load_vendor` needs a version flag.
- Are there days the Java process fails or doesn't run (holidays)? If so, the capture script should handle a missing source file without failing loudly — current behavior IS to fail loudly, which may create noise.
- Does Ziya want a Slack/email alert when a capture is missed, or is checking the repo daily enough?
