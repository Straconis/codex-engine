. "$PSScriptRoot\common.ps1"

$root = $RepoRoot
$backend = Join-Path $root "backend"
$icon = Join-Path $root "assets\icons-v2\codex-engine-v2.ico"
$python = $Python

Invoke-Checked $python @("-m", "pip", "install", "-r", "requirements-build.txt") $backend
Invoke-Checked $python @(
  "-m", "PyInstaller",
  "--clean",
  "--noconfirm",
  "--name", "codex-engine-updater",
  "--onefile",
  "--noconsole",
  "--icon", $icon,
  "codex_engine\updater\updater.py"
) $backend

$out = Join-Path $root "resources\updater"
New-Item -ItemType Directory -Force $out | Out-Null
Copy-Item (Join-Path $backend "dist\codex-engine-updater.exe") (Join-Path $out "codex-engine-updater.exe") -Force
Write-Host "Updater built at resources\updater\codex-engine-updater.exe"
