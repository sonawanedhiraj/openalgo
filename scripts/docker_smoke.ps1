<#
.SYNOPSIS
  Windows wrapper for scripts/docker_smoke.sh (the Docker deployment smoke test).

.DESCRIPTION
  The smoke logic lives in docker_smoke.sh (one source of truth, also used in CI
  and on Linux/macOS). This wrapper locates Git Bash on the Windows laptop and
  runs it. Pass-through env vars (BUILD, KEEP, HOST_HTTP_PORT, ...) are honoured.

.EXAMPLE
  pwsh scripts/docker_smoke.ps1
.EXAMPLE
  $env:BUILD=0; pwsh scripts/docker_smoke.ps1   # reuse existing openalgo:smoketest
#>
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$script = Join-Path $PSScriptRoot 'docker_smoke.sh'
if (-not (Test-Path $script)) { Write-Error "Not found: $script"; exit 1 }

# Find a bash (Git Bash ships one). Prefer PATH, then common install locations.
$bash = (Get-Command bash.exe -ErrorAction SilentlyContinue).Source
if (-not $bash) {
  foreach ($p in @(
      "$env:ProgramFiles\Git\bin\bash.exe",
      "${env:ProgramFiles(x86)}\Git\bin\bash.exe",
      "$env:LOCALAPPDATA\Programs\Git\bin\bash.exe")) {
    if (Test-Path $p) { $bash = $p; break }
  }
}
if (-not $bash) {
  Write-Error "bash.exe not found. Install Git for Windows, or run scripts/docker_smoke.sh from Git Bash directly."
  exit 1
}

Write-Host "Running docker_smoke.sh via $bash ..."
& $bash -lc "cd '$root' && scripts/docker_smoke.sh"
exit $LASTEXITCODE
