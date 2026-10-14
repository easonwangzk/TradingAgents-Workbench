param([int]$Port = 8765)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$python = Join-Path $root '.venv/Scripts/python.exe'
if (-not (Test-Path $python)) { throw 'Install the project workbench extra into .venv first.' }
& $python -m tradingagents.workbench.server --port $Port
exit $LASTEXITCODE