# Vendor Capture — Work Machine Runbook

**Run this on the work laptop (`ke11982`) while connected to the corporate network (office or VPN).**
**Do NOT run from home X1 / RDP session — the UNC share is only reachable from corp.**

Repo location on work laptop: `C:\Users\ke11982\noaa-forecast\`

---

## 0. Pre-flight (sanity checks, 30 seconds)

Open PowerShell. Confirm you're on the corporate network:

```powershell
# Should succeed, port 445 (SMB) open to the file server
Test-NetConnection -ComputerName stpwsvcritfil04 -Port 445
```

`TcpTestSucceeded : True` — good. `False` — not on corp network / VPN. Stop here.

Confirm the UNC share is readable:

```powershell
Test-Path "\\stpwsvcritfil04\WGES-Databases\Reports\DailyHourlyTemp\ops-query-in-out_hourly_temp.csv"
```

`True` — proceed. `False` — you can reach the server but not the file. Check with ops / verify share permissions.

Optional sanity: open Windows Explorer, paste `\\stpwsvcritfil04\WGES-Databases\Reports\DailyHourlyTemp\` in the address bar. You should see `ops-query-in-out_hourly_temp.csv` with a recent timestamp.

---

## 1. Create a GitHub Personal Access Token (one-time)

The work laptop can't use GitHub Desktop (corporate block). We'll use a PAT + Windows Credential Manager.

1. From the work laptop, open Chrome/Edge → https://github.com/settings/tokens
2. Click **Generate new token → Generate new token (classic)**
3. Name: `work-laptop-ke11982-noaa-forecast`
4. Expiration: **90 days**
5. Scopes: check **`repo`** (the whole repo group — full control of private repos)
6. Click **Generate token**
7. **Copy the token immediately** — GitHub shows it once. Paste it into Notepad temporarily.

---

## 2. Prime git credentials (one-time)

Still in PowerShell:

```powershell
cd C:\Users\ke11982\noaa-forecast
git config --global credential.helper manager-core
git config --global user.name  "Ziya Kesrefoglu"
git config --global user.email "zkesrefoglu@gmail.com"
```

Trigger a credential prompt by pulling:

```powershell
git pull
```

Windows will pop a credential dialog:
- **Username:** `zkesrefoglu@gmail.com` (or your GitHub username)
- **Password:** paste the PAT from Step 1

Credential Manager caches it. You shouldn't be prompted again until the PAT expires.

Verify cached:

```powershell
cmdkey /list | Select-String github
```

You should see `LegacyGeneric:target=git:https://github.com` or similar.

---

## 3. Push the $PSScriptRoot change from work laptop

Earlier you rewrote `$RepoPath` in `capture_vendor.ps1` to use `Split-Path -Parent $PSScriptRoot`. That change is only on the work laptop locally. Commit and push it so home/X1 stays in sync.

```powershell
cd C:\Users\ke11982\noaa-forecast
git status
```

If `scripts/capture_vendor.ps1` shows as modified:

```powershell
git add scripts/capture_vendor.ps1
git commit -m "capture_vendor: use PSScriptRoot for path-agnostic RepoPath"
git push
```

If `git status` says clean, someone already pushed it — move on.

---

## 4. Manual test run of capture_vendor.ps1

```powershell
cd C:\Users\ke11982\noaa-forecast
.\scripts\capture_vendor.ps1
```

**Expected output (roughly):**

```
Copied vendor file to C:\Users\ke11982\noaa-forecast\data\vendor\2026-04-21.csv
Pushed vendor snapshot for 2026-04-21.
```

**If you see "Vendor file not found":** you're not on corp network. Bounce to Step 0.

**If you see a git push error:** PAT not primed. Bounce to Step 2.

**If you see "already captured (identical hash). Nothing to do":** file on share hasn't changed since last capture. That's fine — idempotent.

Verify the commit landed on GitHub: https://github.com/<your-org>/noaa-forecast/tree/main/data/vendor — there should be a new CSV named `<today>.csv`.

---

## 5. Install the scheduled task (one-time)

Daily at 10:00 AM local. Your machine is on by then and the morning Java run has finished.

```powershell
$scriptPath = "C:\Users\ke11982\noaa-forecast\scripts\capture_vendor.ps1"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`""

$trigger = New-ScheduledTaskTrigger -Daily -At 10:00AM

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERNAME" `
  -LogonType Interactive -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
  -StartWhenAvailable `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries

Register-ScheduledTask `
  -TaskName "NOAA-Forecast-Vendor-Capture" `
  -Action $action `
  -Trigger $trigger `
  -Principal $principal `
  -Settings $settings
```

Verify:

```powershell
Get-ScheduledTask -TaskName "NOAA-Forecast-Vendor-Capture" | Select-Object State, TaskName
```

Should show `Ready`.

Force an immediate run to confirm it works end-to-end under Task Scheduler:

```powershell
Start-ScheduledTask -TaskName "NOAA-Forecast-Vendor-Capture"
Start-Sleep 30
Get-ScheduledTaskInfo -TaskName "NOAA-Forecast-Vendor-Capture" | Select-Object LastRunTime, LastTaskResult, NextRunTime
```

`LastTaskResult : 0` — success. Anything else — check Task Scheduler GUI → History tab.

---

## 6. Tomorrow-morning verification

Tomorrow (2026-04-22) around 10:05 AM local, check:

1. **GitHub:** new file at `data/vendor/2026-04-22.csv` committed by you.
2. **Task Scheduler:** `Get-ScheduledTaskInfo` → `LastTaskResult : 0`, `LastRunTime` is today ~10 AM.

If both green: vendor capture is live. Hands off, it runs itself.

---

## Troubleshooting quick-ref

| Symptom | Likely cause | Fix |
|---|---|---|
| `Vendor file not found` | Not on corp network | Connect to VPN, re-run Step 0 |
| `Test-NetConnection` returns False | Off VPN / corp firewall | Connect VPN |
| `Test-Path` False but NetConnection True | Share permission issue | Contact ops, verify you have read on the share |
| Git push fails with 401/403 | PAT expired or not cached | Redo Step 1 + Step 2 |
| Scheduled task status `Disabled` | User policy | Right-click in Task Scheduler → Enable |
| `LastTaskResult` non-zero | Script errored under task context | Open Task Scheduler → History tab → read stderr |
| Script runs but no commit | File hash matched previous day | Expected if Java process hasn't updated the file yet |

---

## Kill switch (if you need to disable it)

```powershell
Unregister-ScheduledTask -TaskName "NOAA-Forecast-Vendor-Capture" -Confirm:$false
```

---

## Context reminder

The Java process (`TmprHistRefresherApp`) overwrites `ops-query-in-out_hourly_temp.csv` in place twice per day. The **morning run** is what the scheduling team commits against. Miss the morning capture and you lose that day's forecast forever — the afternoon file reflects different information and there's no DB archive.

10 AM local is chosen because (a) morning Java run has completed by then, (b) you're at your desk / laptop is on, (c) buffer before the afternoon Java run clobbers the file.

Source of truth for the full integration story: `outputs/noaa-forecast-validation/references/vendor-integration.md`.
