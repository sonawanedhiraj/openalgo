# ensure_runner.ps1 — self-heal watchdog for the GitHub Actions self-hosted runner.
#
# Detects the "zombie" state where the runner container is Up but has deregistered
# from GitHub (total_count == 0), or the container has exited. Recreates it when
# needed.
#
# Designed to be run:
#   - Manually when the runner seems stuck ("why isn't my PR picking up?")
#   - As a scheduled Task Scheduler job (every 15 min) for hands-free recovery
#
# Usage:
#   pwsh scripts/runner/ensure_runner.ps1            # check + fix
#   pwsh scripts/runner/ensure_runner.ps1 -DryRun    # check only, no changes
#   pwsh scripts/runner/ensure_runner.ps1 -Schedule  # install as Task Scheduler job
#
# Prerequisites: gh CLI authenticated, Docker Desktop running, C:\actions-runner exists.
param(
  [switch]$DryRun,
  [switch]$Schedule
)

$ErrorActionPreference = "Stop"
$RunnerDir   = "C:\actions-runner"
$Container   = "openalgo-gh-runner"
$Repo        = "sonawanedhiraj/openalgo"

function Write-Banner($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Write-OK($msg)     { Write-Host "  ✅ $msg" -ForegroundColor Green }
function Write-Warn($msg)   { Write-Host "  ⚠️  $msg" -ForegroundColor Yellow }
function Write-Err($msg)    { Write-Host "  ❌ $msg" -ForegroundColor Red }

if ($Schedule) {
  # Register a Task Scheduler job that runs this script every 15 minutes.
  $ScriptPath = (Resolve-Path $PSCommandPath).Path
  $Action  = New-ScheduledTaskAction -Execute "pwsh" -Argument "-NonInteractive -File `"$ScriptPath`""
  $Trigger = New-ScheduledTaskTrigger -RepetitionInterval (New-TimeSpan -Minutes 15) -Once -At (Get-Date)
  $Settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -MultipleInstances IgnoreNew
  Register-ScheduledTask -TaskName "OpenAlgo-RunnerWatchdog" -Action $Action -Trigger $Trigger `
    -Settings $Settings -RunLevel Highest -Force | Out-Null
  Write-OK "Scheduled task 'OpenAlgo-RunnerWatchdog' registered (every 15 min)"
  exit 0
}

Write-Banner "Checking runner health"

# 1. Is Docker reachable?
try {
  $dockerVer = docker version --format '{{.Server.Version}}' 2>&1
  Write-OK "Docker daemon: $dockerVer"
} catch {
  Write-Err "Docker daemon not reachable — is Docker Desktop running?"
  exit 1
}

# 2. Is the container running?
$containerStatus = docker inspect $Container --format '{{.State.Status}}' 2>&1
if ($LASTEXITCODE -ne 0) { $containerStatus = "missing" }

Write-Host "  Container '$Container': $containerStatus"

# 3. Are there any registered runners?
try {
  $runnersJson = gh api "repos/$Repo/actions/runners" 2>&1
  $runnerCount = ($runnersJson | ConvertFrom-Json).total_count
} catch {
  Write-Warn "Could not query runner API (gh auth issue?): $_"
  $runnerCount = -1
}

Write-Host "  GitHub registered runners: $runnerCount"

# 4. Decide action
$needsRestart = $false

if ($containerStatus -ne "running") {
  Write-Warn "Container is not running ($containerStatus)"
  $needsRestart = $true
} elseif ($runnerCount -eq 0) {
  Write-Warn "Container is Up but deregistered from GitHub (ephemeral runner consumed its registration)"
  $needsRestart = $true
} elseif ($runnerCount -lt 0) {
  Write-Warn "Could not determine runner count — skipping restart to avoid false positive"
} else {
  Write-OK "Runner healthy ($runnerCount registered, container $containerStatus)"
}

if (-not $needsRestart) { exit 0 }

if ($DryRun) {
  Write-Warn "DryRun: would recreate runner container. Run without -DryRun to apply."
  exit 0
}

# 5. Recreate
Write-Banner "Recreating runner"
Write-Host "  Removing stale container..."
docker rm -f $Container 2>&1 | Out-Null
Write-Host "  Starting fresh container..."
Push-Location $RunnerDir
docker compose up -d 2>&1 | Select-Object -Last 3
Pop-Location

# 6. Wait and verify
Write-Host "  Waiting for registration (up to 30s)..."
$registered = $false
for ($i = 0; $i -lt 10; $i++) {
  Start-Sleep -Seconds 3
  try {
    $count = (gh api "repos/$Repo/actions/runners" | ConvertFrom-Json).total_count
    if ($count -gt 0) { $registered = $true; break }
  } catch {}
}

if ($registered) {
  Write-OK "Runner registered and ready"
} else {
  Write-Err "Runner did not register within 30s — check: docker logs $Container"
  exit 1
}
