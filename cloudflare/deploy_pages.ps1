#Requires -Version 5.1
<#
.SYNOPSIS
    Publish docs/ to Cloudflare Pages, working around the 25 MiB per-file limit.

.DESCRIPTION
    Cloudflare Pages rejects any single asset larger than 25 MiB and any
    deployment with more than 20,000 files. The built site breaks the first rule
    (a handful of large PDFs plus the MARUTI 360 bundle), so this script stages a
    copy of docs/ with those files removed, then deploys the staged copy.

    docs/ itself is never modified, so re-running build_html.py cannot undo
    anything here. The excluded files are written to _deploy/excluded.json.

.PARAMETER Project
    Cloudflare Pages project name.

.PARAMETER Source
    Folder to publish. Defaults to docs/.

.PARAMETER MaxFileMb
    Per-file ceiling. Pages enforces 25 MiB.

.PARAMETER DryRun
    Stage the folder and report, but do not deploy.

.EXAMPLE
    .\cloudflare\deploy_pages.ps1

.EXAMPLE
    .\cloudflare\deploy_pages.ps1 -Project gujrera-projects -DryRun
#>
[CmdletBinding()]
param(
    [string]$Project = "gujrera-projects",
    [string]$Source = "docs",
    [string]$Staging = "_deploy/pages",
    [int]$MaxFileMb = 25,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot

Push-Location $repo
try {
    if (-not (Test-Path $Source)) {
        throw "source folder '$Source' not found. Run 'python build_html.py' first."
    }

    $wrangler = if (Get-Command wrangler -ErrorAction SilentlyContinue) {
        (Get-Command wrangler).Source
    } elseif (Test-Path "$env:APPDATA\npm\wrangler.ps1") {
        "$env:APPDATA\npm\wrangler.ps1"
    } else {
        throw "wrangler not found. Install it with: npm install -g wrangler"
    }

    Write-Host "staging $Source -> $Staging" -ForegroundColor Cyan
    if (Test-Path $Staging) { Remove-Item $Staging -Recurse -Force }
    # /MIR mirrors exactly; noisy robocopy output is suppressed.
    robocopy $Source $Staging /MIR /NFL /NDL /NJH /NJS /nc /ns /np | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy failed with exit code $LASTEXITCODE" }

    $limitBytes = $MaxFileMb * 1MB
    $oversize = Get-ChildItem $Staging -Recurse -File |
        Where-Object { $_.Length -gt $limitBytes } |
        Sort-Object Length -Descending

    $excluded = @()
    foreach ($file in $oversize) {
        $relative = $file.FullName.Substring((Resolve-Path $Staging).Path.Length).TrimStart("\", "/")
        $excluded += [pscustomobject]@{
            path = $relative -replace "\\", "/"
            megabytes = [math]::Round($file.Length / 1MB, 2)
        }
        Write-Host ("  excluding {0,8:N1} MB  {1}" -f ($file.Length / 1MB), $relative) -ForegroundColor Yellow
        Remove-Item $file.FullName -Force
    }

    New-Item -ItemType Directory -Force -Path "_deploy" | Out-Null

    # Without a 404.html, Pages answers unknown paths with the root index.html and
    # status 200 -- so a missing PDF would silently render as a web page instead of
    # reporting "not found".
    $notFound = Join-Path $Staging "404.html"
    if (-not (Test-Path $notFound)) {
        $html = @'
<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Not found &middot; GujRERA archive</title>
<style>
  :root{color-scheme:light dark;--dim:#71717a;--accent:#1d4ed8}
  @media (prefers-color-scheme:dark){:root{--dim:#a1a1aa;--accent:#7aa2f7}}
  body{margin:0;min-height:100vh;display:grid;place-items:center;padding:2rem;
       font:16px/1.6 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
  .box{max-width:34rem}
  h1{font-size:1.5rem;margin:0 0 .5rem;letter-spacing:-.01em}
  p{color:var(--dim);margin:0 0 1.25rem}
  a{color:var(--accent);text-decoration:none}
  a:hover{text-decoration:underline}
  nav{display:flex;gap:1.25rem;font-size:.9rem}
  code{font-size:.85em;background:#8881;padding:.15em .4em;border-radius:4px}
</style>
</head><body>
<div class="box">
  <h1>That page is not here</h1>
  <p>The report page or document you asked for does not exist at this address. Documents that
     exceed the 25&nbsp;MB static-hosting limit are held in the R2 archive instead of on this site.</p>
  <nav>
    <a href="/">&larr; All projects</a>
    <a href="/map">Scheme map</a>
    <a href="/report">Full report</a>
  </nav>
</div>
</body></html>
'@
        Set-Content -Path $notFound -Value $html -Encoding utf8
        Write-Host "  added 404.html (Pages would otherwise return index.html with status 200)" -ForegroundColor Cyan
    }

    $manifest = [pscustomobject]@{
        generated_at = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        project = $Project
        max_file_mb = $MaxFileMb
        note = "Excluded from Pages because each exceeds the per-asset limit. Host these on R2 and rewrite the links."
        excluded = $excluded
    }
    $manifest | ConvertTo-Json -Depth 4 | Set-Content "_deploy/excluded.json" -Encoding utf8

    $stats = Get-ChildItem $Staging -Recurse -File | Measure-Object Length -Sum
    Write-Host ("staged {0:N0} files, {1:N1} MB ({2} oversize excluded)" -f `
        $stats.Count, ($stats.Sum / 1MB), $excluded.Count) -ForegroundColor Green

    if ($stats.Count -gt 20000) {
        throw "staged folder has $($stats.Count) files; Pages allows 20,000. Narrow the content first."
    }

    if ($DryRun) {
        Write-Host "dry run -- not deploying" -ForegroundColor Cyan
        return
    }

    Write-Host "deploying to project '$Project'" -ForegroundColor Cyan
    & $wrangler pages deploy $Staging --project-name $Project --branch main --commit-dirty=true
    if ($LASTEXITCODE -ne 0) { throw "deployment failed (exit $LASTEXITCODE)" }

    Write-Host "`nlive at https://$Project.pages.dev" -ForegroundColor Green
}
finally {
    Pop-Location
}
