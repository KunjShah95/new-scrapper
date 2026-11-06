#Requires -Version 5.1
<#
.SYNOPSIS
    Weekly production cycle: scrape -> push to R2 -> verify -> report.

.DESCRIPTION
    Designed to run unattended (Windows Task Scheduler today, cron on a VPS once
    stable). Every step is idempotent, so an interrupted cycle is safe: the next
    one resumes from what is already on disk and in the bucket.

    Steps
      1. lock            - refuse to run two cycles at once
      2. targets         - rebuild the work list from the geo dataset
      3. scrape          - up to -MaxProjects new projects, uploading straight to R2
      4. sync            - push anything still local, then prune local documents
      5. verify          - full ETag/MD5 sweep of every object against disk
      6. report          - regenerate BROKEN_DOCUMENTS.{csv,json,md}
      7. summary         - append one line to _work/logs/history.csv

.PARAMETER MaxProjects
    Cap on new projects this cycle. Keeps a weekly run inside its window; the
    backlog drains over successive weeks.

.PARAMETER SkipScrape
    Only push + verify (use on a VPS where scraping happens elsewhere).

.EXAMPLE
    .\cloudflare\weekly_scrape.ps1
    .\cloudflare\weekly_scrape.ps1 -MaxProjects 300
#>
[CmdletBinding()]
param(
    [int]$MaxProjects = 1000,
    [switch]$SkipScrape,
    [int]$WaitForRunningMinutes = 120
)

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $repo "_work\logs"
$lockFile = Join-Path $repo "_work\scrape.lock"
$historyFile = Join-Path $logDir "history.csv"

New-Item -ItemType Directory -Force -Path $logDir | Out-Null
New-Item -ItemType Directory -Force -Path (Split-Path $lockFile) | Out-Null

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$log = Join-Path $logDir "cycle-$stamp.log"

function Write-Log {
    param([string]$Message, [string]$Colour = "Gray")
    $line = "{0}  {1}" -f (Get-Date -Format "HH:mm:ss"), $Message
    Write-Host $line -ForegroundColor $Colour
    Add-Content -Path $log -Value $line -Encoding utf8
}

function Test-ScrapeRunning {
    $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'run\.py' }
    return [bool]$procs
}

Push-Location $repo
$exitCode = 0
$startedAt = Get-Date
try {
    # ---- 1. lock ---------------------------------------------------------
    # An operator may already have a scrape running without holding the lock, so
    # check for the process too, not just the file.
    if ((Test-Path $lockFile) -or (Test-ScrapeRunning)) {
        $holder = if (Test-Path $lockFile) { Get-Content $lockFile -ErrorAction SilentlyContinue } else { "manual run" }
        Write-Log "another scrape is active ($holder) - waiting up to $WaitForRunningMinutes min" "Yellow"
        $deadline = (Get-Date).AddMinutes($WaitForRunningMinutes)
        while ((Test-ScrapeRunning) -and (Get-Date) -lt $deadline) {
            Start-Sleep -Seconds 60
        }
        if (Test-ScrapeRunning) {
            Write-Log "still running after $WaitForRunningMinutes min - skipping this cycle" "Yellow"
            exit 0
        }
        Remove-Item $lockFile -Force -ErrorAction SilentlyContinue
    }
    "pid=$PID started=$($startedAt.ToString('s'))" | Set-Content $lockFile -Encoding utf8
    Write-Log "=== cycle start ===" "Cyan"

    # ---- 2. refresh targets ---------------------------------------------
    Write-Log "refreshing target list"
    python build_targets.py *>> $log
    $missing = Get-Content output\targets_missing.json -Raw | ConvertFrom-Json
    Write-Log ("projects still to scrape: {0:N0}" -f $missing.Count)

    # ---- 3. scrape -------------------------------------------------------
    if (-not $SkipScrape -and $missing.Count -gt 0) {
        Write-Log "scraping up to $MaxProjects project(s), direct to R2" "Cyan"
        $env:PYTHONIOENCODING = "utf-8"
        python -u run.py --scrape --targets output/targets_missing.json `
            --limit $MaxProjects --direct-r2 *>> $log
        if ($LASTEXITCODE -ne 0) {
            Write-Log "scrape exited $LASTEXITCODE (continuing - uploads are already done)" "Yellow"
        }
    }
    else {
        Write-Log "nothing new to scrape" "Green"
    }

    # ---- 4. sync + prune -------------------------------------------------
    Write-Log "syncing to R2 and pruning local documents"
    python -u cloudflare\r2_sync.py --workers 16 --prune *>> $log

    # ---- 5. verify -------------------------------------------------------
    Write-Log "verifying every object against disk"
    python -u cloudflare\r2_verify.py --all --workers 48 `
        --report cloudflare\verify_full.json *>> $log
    $verifyOk = $LASTEXITCODE -eq 0
    Write-Log ("verify: " + $(if ($verifyOk) { "PASS" } else { "PROBLEMS FOUND" })) `
        $(if ($verifyOk) { "Green" } else { "Red" })

    # ---- 6. broken-document register ------------------------------------
    Write-Log "regenerating BROKEN_DOCUMENTS"
    python make_broken_report.py *>> $log

    # ---- 7. history ------------------------------------------------------
    $elapsed = [math]::Round(((Get-Date) - $startedAt).TotalMinutes, 1)
    $row = "{0},{1},{2},{3},{4}" -f $startedAt.ToString("s"), $missing.Count, $MaxProjects, $verifyOk, $elapsed
    if (-not (Test-Path $historyFile)) {
        "started,backlog,cap,verify_passed,minutes" | Set-Content $historyFile -Encoding utf8
    }
    Add-Content $historyFile -Value $row -Encoding utf8
    Write-Log "=== cycle finished in $elapsed min (log: $log) ===" "Cyan"

    if (-not $verifyOk) { $exitCode = 1 }
}
catch {
    Write-Log "FATAL: $_" "Red"
    $exitCode = 2
}
finally {
    Remove-Item $lockFile -Force -ErrorAction SilentlyContinue
    Pop-Location
}
exit $exitCode
