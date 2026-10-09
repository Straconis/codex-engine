. "$PSScriptRoot\common.ps1"

Invoke-Npm build
Invoke-Npm build:backend:win
Invoke-Npm build:updater:win
Invoke-Npm package:dir
Invoke-Npm installer:inno
