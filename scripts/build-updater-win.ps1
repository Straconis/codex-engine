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

Invoke-Checked $python @("-m", "pip", "install", "-r", "requirements-build.txt") $backend
Invoke-Checked $python @(
  "-m", "PyInstaller",
  "--clean",
  "--noconfirm",
  "--name", "codex-engine-updater",
  "--onefile",
  "--icon", $icon,
  "codex_engine\updater\updater.py"
) $backend

$out = Join-Path $root "resources\updater"
New-Item -ItemType Directory -Force $out | Out-Null
Copy-Item (Join-Path $backend "dist\codex-engine-updater.exe") (Join-Path $out "codex-engine-updater.exe") -Force
Write-Host "Updater built at resources\updater\codex-engine-updater.exe"
