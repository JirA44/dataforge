[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

if (-not (Test-Path ".venv/Scripts/python.exe")) {
    py -3.10 -m venv .venv
}

& ".venv/Scripts/python.exe" -m pip install -e ".[dev]"
& ".venv/Scripts/python.exe" -m pytest

