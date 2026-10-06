param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
& $Python (Join-Path $projectRoot 'server/server.py')
exit $LASTEXITCODE
