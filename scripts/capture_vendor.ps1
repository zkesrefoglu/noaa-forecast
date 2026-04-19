# capture_vendor.ps1
#
# Daily vendor forecast capture. Run on the work machine via Windows Task
# Scheduler at 10:00 AM local time (post-8:30 AM java dump, pre-arbitrary).
#
# What it does:
#   1. Copies the java-generated vendor file from the internal network share
#      into the local git clone.
#   2. Renames it with today's date (YYYY-MM-DD.csv).
#   3. Commits and pushes to the noaa-forecast repo.
#
# Prerequisites:
#   - Git installed and on PATH.
#   - Git credentials configured (credential manager or SSH key).
#   - Local clone of noaa-forecast at $RepoPath below.
#   - Work machine has outbound HTTPS to github.com (verified).
#
# Idempotent: if today's file is already committed, the push is a no-op.

$ErrorActionPreference = "Stop"

# === Configure these two lines ===
$VendorFile = "\\FILL_IN_SERVER\fill\in\path\ops-query-in-out_hourly_temp.csv"
$RepoPath   = "C:\Users\Ziya\Documents\GitHub\noaa-forecast"
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
