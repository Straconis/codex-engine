# Shared helpers for the Windows build scripts. Dot-source it: . "$PSScriptRoot\common.ps1"
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# PowerShell doesn't stop on a failing native command, so every step is checked.
# Without this, a failed `npm run build` once silently packaged the previous build.
function Invoke-Checked([string]$FilePath, [string[]]$Arguments, [string]$WorkingDirectory = "") {
  $argText = $Arguments -join " "
  Write-Host "> $FilePath $argText"
  if ($WorkingDirectory) { Push-Location $WorkingDirectory }
  try {
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
      throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $argText"
    }
  }
  finally {
    if ($WorkingDirectory) { Pop-Location }
  }
}

function Invoke-Npm([string]$Script) {
  Write-Host "`n=== npm run $Script ===" -ForegroundColor Cyan
  npm run $Script
  if ($LASTEXITCODE -ne 0) { throw "npm run $Script failed with exit code $LASTEXITCODE" }
}

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = if ($env:CODEX_ENGINE_PYTHON) { $env:CODEX_ENGINE_PYTHON } else { "python" }
$PackageVersion = (Get-Content (Join-Path $RepoRoot "package.json") -Raw | ConvertFrom-Json).version
