$ErrorActionPreference = "Stop"

$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$iss = Join-Path $root "installer\codex-engine.iss"
$sourceDir = Join-Path $root "dist\win-unpacked"

if (-not (Test-Path $sourceDir)) {
  throw "Electron unpacked app not found at $sourceDir. Run npm run package:dir first."
}

$candidates = @(
  "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
  "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
) | Where-Object { $_ -and (Test-Path $_) }

$iscc = $candidates | Select-Object -First 1
if (-not $iscc) {
  $cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
  if ($cmd) { $iscc = $cmd.Source }
}

if (-not $iscc) {
  throw "ISCC.exe was not found. Install Inno Setup 6 or add ISCC.exe to PATH."
}

New-Item -ItemType Directory -Force (Join-Path $root "release\installer") | Out-Null
& $iscc $iss


