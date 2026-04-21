# capture_vendor.ps1
#
# Daily vendor forecast capture. Run on the work machine via Windows Task
# Scheduler at 10:00 AM local (14:00 UTC EDT / 15:00 UTC EST).
#
# Context:
#   The WGL OPS java process (TmprHistRefresherApp) regenerates the CSV twice
#   daily. The MORNING run is the one the scheduling team uses and the one we
#   want to capture. By 10 AM local the morning file has been written and
#   Ziya's laptop is on.
#
#   Source file lives on the WGES file server. It is overwritten in place on
#   each java run, so we have to grab it before the next run clobbers it.
#
# What it does:
#   1. Copies the java-generated vendor CSV from the WGES network share into
#      the local git clone, renamed to data/vendor/<captureDate>.csv where
#      captureDate is the source file's LastWriteTime (NOT "today").
#   2. Short-circuits if the same content has already been captured (SHA256).
#   3. git pull --quiet -> add -> commit -> push.
#
# Prerequisites:
#   - Git installed and on PATH.
#   - Git credentials configured (credential manager or SSH key).
#   - Local clone of noaa-forecast at $RepoPath below.
#   - Work machine has outbound HTTPS to github.com (verified).
#   - Read access to \\stpwsvcritfil04\WGES-Databases\ (corp VPN / domain).
#
# Idempotent: if today's file is already committed, the push is a no-op.

$ErrorActionPreference = "Stop"

# === Configure these two lines ===
$VendorFile = "\\stpwsvcritfil04\WGES-Databases\Reports\DailyHourlyTemp\ops-query-in-out_hourly_temp.csv"
$RepoPath   = Split-Path -Parent $PSScriptRoot
# =================================

# Use the date the java process generated the file (the file's modification
# date on the network share). If we used "today", a late-running task on
# 4/19 could mis-tag a file actually generated on 4/18.
if (-not (Test-Path $VendorFile)) {
    Write-Error "Vendor file not found: $VendorFile"
    exit 1
}
$src = Get-Item -LiteralPath $VendorFile
$captureDate = $src.LastWriteTime.Date.ToString("yyyy-MM-dd")

$destDir  = Join-Path $RepoPath "data\vendor"
$destFile = Join-Path $destDir "$captureDate.csv"

if (-not (Test-Path $destDir)) {
    New-Item -ItemType Directory -Path $destDir | Out-Null
}

# Skip the copy if we already have today's file with identical content.
if (Test-Path $destFile) {
    $existing = Get-FileHash -Path $destFile -Algorithm SHA256
    $incoming = Get-FileHash -Path $VendorFile -Algorithm SHA256
    if ($existing.Hash -eq $incoming.Hash) {
        Write-Host "Vendor file for $captureDate already captured (identical hash). Nothing to do."
        exit 0
    }
}

Copy-Item -LiteralPath $VendorFile -Destination $destFile -Force
Write-Host "Copied vendor file to $destFile"

Push-Location $RepoPath
try {
    git pull --quiet
    git add "data/vendor/$captureDate.csv"
    $status = git diff --cached --name-only
    if (-not $status) {
        Write-Host "No changes to commit."
        exit 0
    }
    git commit -m "vendor snapshot $captureDate" | Out-Null
    git push --quiet
    Write-Host "Pushed vendor snapshot for $captureDate."
} finally {
    Pop-Location
}
