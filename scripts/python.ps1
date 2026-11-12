. (Join-Path $PSScriptRoot 'environment.ps1')
$pythonExecutable = Join-Path $WorkbenchRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExecutable)) {
    throw 'Run scripts\bootstrap.ps1 first.'
}
& $pythonExecutable @args
exit $LASTEXITCODE
