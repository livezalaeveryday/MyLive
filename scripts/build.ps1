param([string]$Python = 'python', [switch]$AdsOnly)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$spec = if ($AdsOnly) { 'ad_server.spec' } else { 'server.spec' }
& $Python -m PyInstaller --noconfirm --distpath (Join-Path $projectRoot 'dist') --workpath (Join-Path $projectRoot 'build') (Join-Path $projectRoot "server/$spec")
exit $LASTEXITCODE
