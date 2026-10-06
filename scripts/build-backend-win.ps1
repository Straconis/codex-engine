$ErrorActionPreference = "Stop"

function Invoke-Checked($FilePath, [string[]]$Arguments, $WorkingDirectory = $null) {
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

$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$backend = Join-Path $root "backend"
$icon = Join-Path $root "assets\icons-v2\codex-engine-v2.ico"
$python = if ($env:CODEX_ENGINE_PYTHON) { $env:CODEX_ENGINE_PYTHON } else { "python" }

# Fail fast if the backend version drifted from package.json (it feeds the update check).
$version = (Get-Content (Join-Path $root "package.json") -Raw | ConvertFrom-Json).version
$configText = Get-Content (Join-Path $backend "codex_engine\config.py") -Raw
if ($configText -notmatch 'APP_VERSION\s*=\s*"([^"]+)"') { throw "APP_VERSION not found in backend\codex_engine\config.py" }
if ($Matches[1] -ne $version) {
  throw "Version mismatch: package.json is $version but backend config.py APP_VERSION is $($Matches[1])"
}

Invoke-Checked $python @("-m", "pip", "install", "-r", "requirements-build.txt") $backend
Invoke-Checked $python @(
  "-m", "PyInstaller",
  "--clean",
  "--noconfirm",
  "--name", "codex-engine-backend",
  "--onefile",
  "--icon", $icon,
  "--paths", ".",
  "--collect-submodules", "codex_engine",
  "server_entry.py"
) $backend

$out = Join-Path $root "resources\backend"
New-Item -ItemType Directory -Force $out | Out-Null
Copy-Item (Join-Path $backend "dist\codex-engine-backend.exe") (Join-Path $out "codex-engine-backend.exe") -Force
Write-Host "Backend sidecar built at resources\backend\codex-engine-backend.exe"
