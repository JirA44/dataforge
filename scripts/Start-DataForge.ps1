[CmdletBinding()]
param(
    [string]$DatabasePath = "data/dataforge.sqlite3",
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8010
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

if (-not (Test-Path ".venv/Scripts/python.exe")) {
    py -3.10 -m venv .venv
}

& ".venv/Scripts/python.exe" -m pip install -e .
$env:DATAFORGE_DB_PATH = $DatabasePath
$env:DATAFORGE_HOST = $HostAddress
$env:DATAFORGE_PORT = $Port.ToString()
& ".venv/Scripts/python.exe" -m dataforge.main

