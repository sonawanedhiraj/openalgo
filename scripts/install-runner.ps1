#requires -Version 5.1
<#
.SYNOPSIS
  Bootstrap the OpenAlgo self-hosted GitHub Actions runner (Docker-isolated).
.DESCRIPTION
  Deploys the runner templates from the repo's ci/runner/ directory to the
  active runtime location (default C:\actions-runner, outside the repo working
  tree so the runner's file writes never collide with git operations on .git/).
  Verifies Docker Desktop is running and <Target>\.env.runner is configured,
  then brings up the hardened runner container and tails its log so you can
  watch it register. Does NOT generate the token — see ci/runner/README.md.
.PARAMETER Target
  Runtime directory the runner runs from. Defaults to C:\actions-runner.
.NOTES
  Run from anywhere:  pwsh scripts/install-runner.ps1
#>
[CmdletBinding()]
param(
    [string]$Target = 'C:\actions-runner',
    [int]$TailSeconds = 30
)

$ErrorActionPreference = 'Stop'
$repoRoot       = Split-Path -Parent $PSScriptRoot
$templateDir    = Join-Path $repoRoot 'ci/runner'
$srcCompose     = Join-Path $templateDir 'docker-compose.yml'
$srcEnvExample  = Join-Path $templateDir '.env.runner.example'
$composeFile    = Join-Path $Target 'docker-compose.yml'
$envFile        = Join-Path $Target '.env.runner'

Write-Host '== OpenAlgo self-hosted runner bootstrap ==' -ForegroundColor Cyan
Write-Host "    template dir : $templateDir" -ForegroundColor DarkGray
Write-Host "    runtime dir  : $Target" -ForegroundColor DarkGray

# 0. Ensure the runtime directory exists (outside the repo working tree).
New-Item -ItemType Directory -Force -Path $Target | Out-Null

# 0a. Deploy templates into the runtime dir (only if not already present, so a
#     configured runtime is never clobbered).
if (-not (Test-Path $composeFile)) {
    Copy-Item $srcCompose $composeFile
    Write-Host "[ok] Copied docker-compose.yml -> $composeFile" -ForegroundColor Green
} else {
    Write-Host "[ok] docker-compose.yml already present in $Target (left as-is)" -ForegroundColor Green
}
if (-not (Test-Path (Join-Path $Target '.env.runner.example'))) {
    Copy-Item $srcEnvExample (Join-Path $Target '.env.runner.example')
}

# 1. Docker daemon reachable?
try {
    docker version --format '{{.Server.Version}}' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'daemon not reachable' }
} catch {
    Write-Error 'Docker Desktop is not running (or `docker` is not on PATH). Start Docker Desktop (WSL2 backend) and retry.'
    exit 1
}
Write-Host '[ok] Docker daemon reachable' -ForegroundColor Green

# 2. .env.runner present and a token set?
if (-not (Test-Path $envFile)) {
    Write-Error "Missing $envFile. Copy $Target\.env.runner.example to $envFile and set ACCESS_TOKEN (see ci/runner/README.md)."
    exit 1
}
$hasAccess = Select-String -Path $envFile -Pattern '^\s*ACCESS_TOKEN\s*=\s*\S' -Quiet
$hasRunner = Select-String -Path $envFile -Pattern '^\s*RUNNER_TOKEN\s*=\s*\S' -Quiet
if (-not $hasAccess -and -not $hasRunner) {
    Write-Error "No ACCESS_TOKEN (or RUNNER_TOKEN) set in $envFile. Add your token (see ci/runner/README.md) and retry."
    exit 1
}
Write-Host '[ok] Token found in .env.runner' -ForegroundColor Green

# 3. Bring up the runner.
Write-Host '[..] Starting runner container (docker compose up -d)...' -ForegroundColor Yellow
docker compose -f $composeFile up -d
if ($LASTEXITCODE -ne 0) {
    Write-Error 'docker compose up failed. Check the output above.'
    exit 1
}
Write-Host '[ok] Container started' -ForegroundColor Green

# 4. Tail the log so registration is visible (the container keeps running after).
Write-Host "[..] Tailing runner log for $TailSeconds s..." -ForegroundColor Yellow
$job = Start-Job -ScriptBlock { param($cf) docker compose -f $cf logs -f } -ArgumentList $composeFile
Start-Sleep -Seconds $TailSeconds
Receive-Job $job
Stop-Job   $job -ErrorAction SilentlyContinue | Out-Null
Remove-Job $job -Force -ErrorAction SilentlyContinue | Out-Null

Write-Host ''
Write-Host '== Next steps ==' -ForegroundColor Cyan
Write-Host '  1. github.com/sonawanedhiraj/openalgo -> Settings -> Actions -> Runners'
Write-Host '     -> confirm "openalgo-laptop" shows status "Idle".'
Write-Host '  2. Open a PR to main/dev to trigger .github/workflows/ci-self-hosted.yml.'
Write-Host "  3. Manage: docker compose -f $composeFile [logs -f | stop | down]"
