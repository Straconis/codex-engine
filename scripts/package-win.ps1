$ErrorActionPreference = "Stop"

function Invoke-Npm([string]$Script) {
  Write-Host "`n=== npm run $Script ===" -ForegroundColor Cyan
  npm run $Script
  if ($LASTEXITCODE -ne 0) { throw "npm run $Script failed with exit code $LASTEXITCODE" }
}

Invoke-Npm build
Invoke-Npm build:backend:win
Invoke-Npm build:updater:win
Invoke-Npm package:win
