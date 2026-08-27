[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
$env:PYTHONPATH = Join-Path $ProjectRoot "src"
py -3.10 -m unittest discover -s tests -v

