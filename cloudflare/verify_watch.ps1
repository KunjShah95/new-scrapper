#Requires -Version 5.1
<#
.SYNOPSIS
    Keep sampling R2 objects for corruption while the upload is running.

.DESCRIPTION
    Every N minutes, draws a random sample of already-uploaded objects and checks
    size + ETag against the local file. Appends results to a log, then exits when
    the r2_sync upload process is no longer running.

    HEAD requests and local hashing only - nothing is downloaded, and the check
    does not compete with the upload for bandwidth.

.EXAMPLE
    .\cloudflare\verify_watch.ps1
    .\cloudflare\verify_watch.ps1 -IntervalMinutes 5 -Sample 200
#>
[CmdletBinding()]
param(
    [int]$IntervalMinutes = 10,
    [int]$Sample = 150,
    [int]$Workers = 48,
    [string]$Log = "cloudflare/r2_verify.log"
)

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
Push-Location $repo
try {
    function Test-UploadRunning {
        $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -match 'r2_sync' }
        return [bool]$procs
    }

    if (-not (Test-Path $Log)) {
        "# integrity watch - sampling every $IntervalMinutes min" |
            Out-File -FilePath $Log -Encoding utf8
    }

    $round = 0
    while (Test-UploadRunning) {
        $round++
        $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        "`n=== sample #$round at $stamp ===" |
            Out-File -FilePath $Log -Append -Encoding utf8
        # Force UTF-8 for both the redirect and python's stdout so the log stays
        # readable; PS 5.1 otherwise mixes UTF-16 and ANSI in the same file.
        $env:PYTHONIOENCODING = "utf-8"
        python cloudflare/r2_verify.py --sample $Sample --workers $Workers *>&1 |
            Out-File -FilePath $Log -Append -Encoding utf8
        Start-Sleep -Seconds ($IntervalMinutes * 60)
    }

    "`n=== upload finished; watch ended $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') after $round round(s) ===" |
        Out-File -FilePath $Log -Append -Encoding utf8
}
finally {
    Pop-Location
}
