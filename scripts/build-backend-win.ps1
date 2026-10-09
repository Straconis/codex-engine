. "$PSScriptRoot\common.ps1"

$root = $RepoRoot
$backend = Join-Path $root "backend"
$icon = Join-Path $root "assets\icons-v2\codex-engine-v2.ico"
$python = $Python

# Fail fast if the backend version drifted from package.json (it feeds the update check).
$version = $PackageVersion
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
