$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot
$VenvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$Python = if (Test-Path $VenvPython) { $VenvPython } else { "python" }

& $Python -m openswap
exit $LASTEXITCODE
