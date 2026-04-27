# Vendor Capture — Server Runbook (`stpwsvcritfil04`)

This is the going-forward capture setup. Run on `stpwsvcritfil04` (the file server itself, also the host for the WGL Java/SQL temperature jobs). After this is wired, the work laptop is out of the picture entirely.

**Run all steps on the server while you're connected to it.** Claude is blocked at corp, so this runbook is self-contained — execute it independently when you have a session on the server.

---

## Why the server, not the laptop

- The CSV file is on `stpwsvcritfil04`'s own disk. Capture has zero network dependency on the server.
- Server is 24/7. Capture happens whether the laptop is on, off, or doesn't exist.
- The WGL SQL job `Ops_SQLQueryInputOutput_Hourly_Temp` writes the morning file at ~8:39 AM. Our task fires at 9:00 AM, well after the morning write and 6+ hours before the 3:39 PM overwrite.

**Fallback if anything breaks:** the file is also emailed to Ziya as an attachment on every SQL run. Manual save from Outlook is always available.

---

## 0. Prerequisites (sanity checks)

Open PowerShell on the server. Confirm:

```powershell
# Git installed?
git --version
```

If `git --version` errors out → install Git for Windows from https://git-scm.com/download/win using your account. Default settings are fine.

```powershell
# GitHub reachable?
Test-NetConnection -ComputerName github.com -Port 443
```

`TcpTestSucceeded : True` → good. Already confirmed by Ziya, but worth re-checking after any corporate network change.

```powershell
# Source CSV present?
Test-Path "\\stpwsvcritfil04\WGES-Databases\Reports\DailyHourlyTemp\ops-query-in-out_hourly_temp.csv"
```

Should return `True`. Since you ARE on the file server, the UNC path resolves locally and is always present (assuming the WGL SQL job ran today).

---

## 1. Clone the repo into the agreed location

```powershell
$RepoParent = "\\stpwsvcritfil04\WGES-Databases\OPSJobs\weather"
$RepoPath   = Join-Path $RepoParent "noaa-forecast"

# If somehow already cloned, skip
if (-not (Test-Path $RepoPath)) {
    Set-Location $RepoParent
    git clone https://github.com/zkesrefoglu/noaa-forecast.git
}
Set-Location $RepoPath
git status
```

Expected: clone completes, `git status` says `On branch main, working tree clean`.

---

## 2. Set up GitHub credentials (one-time)

Create a Personal Access Token if you haven't already (or if your existing one is laptop-only):

1. From the server's browser: https://github.com/settings/tokens
2. **Generate new token (classic)**
3. Name: `server-stpwsvcritfil04-noaa-forecast`
4. Expiration: **90 days**
5. Scopes: check `repo` (full repo control)
6. Generate, copy the token immediately

Then prime credential manager:

```powershell
git config --global credential.helper manager
git config --global user.name  "Ziya Kesrefoglu"
git config --global user.email "zkesrefoglu@gmail.com"

# Trigger a credential prompt
Set-Location $RepoPath
git pull
```

Windows credential dialog pops:
- Username: your GitHub username (`zkesrefoglu` or whatever you use)
- Password: paste the PAT from step 1

Verify the credential is cached:

```powershell
cmdkey /list | Select-String github
```

You should see `LegacyGeneric:target=git:https://github.com` or similar. Token is now cached for this user on this server. You won't be prompted again until the PAT expires.

---

## 3. Manual smoke test of capture_vendor.ps1

Before installing the scheduled task, verify the script works end-to-end as your user.

**Important:** the script lives on a UNC path (`\\stpwsvcritfil04\...`), which Windows treats as untrusted by default. Calling it directly will fail with a `PSSecurityException: UnauthorizedAccess` error. Use one of these two forms instead:

```powershell
# Option A: one-shot bypass via powershell.exe (matches what the scheduled task does)
Set-Location $RepoPath
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\capture_vendor.ps1

# Option B: bypass the current shell session, then run normally
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\scripts\capture_vendor.ps1
```

Process-scope bypass only affects this PowerShell window — exits when you close it. Doesn't change system-wide security.

**Expected output (roughly):**

```
Copied vendor file to \\stpwsvcritfil04\WGES-Databases\OPSJobs\weather\noaa-forecast\data\vendor\2026-04-27.csv
Pushed vendor snapshot for 2026-04-27.
```

**If you see "Vendor file not found":** WGL SQL job hasn't run yet today, or share permissions have shifted. Verify with `Test-Path`.

**If you see "already captured (identical hash). Nothing to do":** the script already ran today and the file hasn't changed since. That's fine — idempotent by design.

**If git push errors:** PAT not cached or expired. Redo step 2.

Verify on GitHub directly: https://github.com/zkesrefoglu/noaa-forecast/tree/main/data/vendor — you should see today's date as a new CSV.

---

## 4. Install the scheduled task

Daily at 9:00 AM. Runs whether you're logged in or not.

**You'll be prompted for your domain password** during registration. The task framework needs it to launch your user context for non-interactive runs.

```powershell
$scriptPath = "\\stpwsvcritfil04\WGES-Databases\OPSJobs\weather\noaa-forecast\scripts\capture_vendor.ps1"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`""

$trigger = New-ScheduledTaskTrigger -Daily -At 9:00AM

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 5)

# Prompt for domain password so the task can run while you're not logged in.
# Format: DOMAIN\username (e.g. WGLE\ke11982). $env:USERDOMAIN gives the domain.
$user = "$env:USERDOMAIN\$env:USERNAME"
$pwd  = Read-Host "Password for $user" -AsSecureString
$cred = New-Object System.Management.Automation.PSCredential($user, $pwd)
$plain = $cred.GetNetworkCredential().Password

Register-ScheduledTask `
    -TaskName "ZKE_NOAA_Vendor_Capture" `
    -Description "Captures the morning ops-query-in-out_hourly_temp.csv to the noaa-forecast repo and pushes to GitHub. Fallback: capture file is also emailed to Ziya, manual recovery via Outlook backfill macro." `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -User $user `
    -Password $plain `
    -RunLevel Limited
```

Verify it was created:

```powershell
Get-ScheduledTask -TaskName "ZKE_NOAA_Vendor_Capture" |
    Select-Object State, TaskName, @{n='Next';e={(Get-ScheduledTaskInfo $_).NextRunTime}}
```

Should show `Ready` and tomorrow's 9:00 AM as the next run.

---

## 5. Force-run once to confirm task-context execution works

```powershell
Start-ScheduledTask -TaskName "ZKE_NOAA_Vendor_Capture"
Start-Sleep 30
Get-ScheduledTaskInfo -TaskName "ZKE_NOAA_Vendor_Capture" |
    Select-Object LastRunTime, LastTaskResult, NextRunTime
```

`LastTaskResult : 0` → success.
Anything else → open Task Scheduler GUI → find ZKE_NOAA_Vendor_Capture → History tab → read the error.

The most common task-context failure mode is "credential not available in the task's user context." If git push fails when run as a task but works manually, the credential helper isn't reading from the same store the task can access. Open Task Scheduler GUI → task properties → General tab → confirm "Run whether user is logged on or not" is selected (not "Run only when user is logged on").

If it succeeds, double-check the resulting commit on GitHub.

---

## 6. Tomorrow-morning verification

The next day around 9:05 AM local:

1. **GitHub**: https://github.com/zkesrefoglu/noaa-forecast/tree/main/data/vendor — new file `<tomorrow's date>.csv`, committed by you.
2. **Task Scheduler**:
   ```powershell
   Get-ScheduledTaskInfo -TaskName "ZKE_NOAA_Vendor_Capture" |
       Select-Object LastRunTime, LastTaskResult
   ```
   `LastRunTime` = today ~9:00 AM, `LastTaskResult : 0`.

If both green: capture is officially live. Hands off.

---

## Troubleshooting quick-reference

| Symptom | Likely cause | Fix |
|---|---|---|
| `Vendor file not found` (manual run) | WGL SQL job hasn't run yet | Wait until after 8:39 AM |
| `Vendor file not found` (task run) | Task ran before 8:39 AM | Bump trigger time later |
| `git: command not found` | Git not installed or not in PATH | Install git-for-windows |
| 401 / 403 on push | PAT expired or wrong scope | Redo step 2 |
| Task `LastTaskResult` = 2147942402 (0x80070002) | Path not found | Confirm `$scriptPath` resolves; UNC path access from task context |
| Task `LastTaskResult` = 1 | Script returned non-zero | Run script manually; whatever errors there are will repro |
| Task `State : Disabled` | Task got auto-disabled (rare) | Re-enable from Task Scheduler GUI |
| `LastTaskResult : 267011 (0x41303)` | Task hasn't run yet | Wait for next trigger |
| Same data captured all week | Java/SQL chain stopped writing | Check WGL OPS team — separate WGL issue, not ours |

---

## Kill switch

If you ever need to disable the capture:

```powershell
Disable-ScheduledTask -TaskName "ZKE_NOAA_Vendor_Capture"
```

Or fully remove:

```powershell
Unregister-ScheduledTask -TaskName "ZKE_NOAA_Vendor_Capture" -Confirm:$false
```

---

## Why this design over alternatives

**Why 9:00 AM?** Morning SQL run finishes at ~8:39-8:40 AM. 9:00 AM gives a 20-minute buffer for the SQL job to fully complete the write. Afternoon SQL run is at 3:39 PM — capture window is ~6h.

**Why not event-chain off the SQL job's completion?** Tempting, but `Ops_SQLQueryInputOutput_Hourly_Temp` has both 8:39 AM and 3:39 PM triggers; chaining would fire twice/day. We only want the morning capture (it's what the scheduling team commits against). A clock-based 9:00 AM trigger filters cleanly without time-of-day guards in the script.

**Why your account, not a service account?** You already have a working scheduled task on this server (`ZKE_CET_Data_Process`) running as your user. Reusing the same identity matches your existing operational pattern, avoids requesting service-account provisioning, and keeps git credentials simple (your PAT under your user). If your account changes, the task moves with you.

**Why not run from the file's local path (e.g. `D:\WGES-Databases\...`)?** UNC works regardless of which drive letter Windows assigns the share. Avoids breaking the script if the server's drive layout shifts.

---

## File / script index

- `scripts/capture_vendor.ps1` — the actual capture (path-agnostic via `$PSScriptRoot`)
- `data/vendor/<YYYY-MM-DD>.csv` — committed AM captures
- `noaa-forecast-validation/references/vendor-integration.md` — full context on the vendor feed and what its data tells us
