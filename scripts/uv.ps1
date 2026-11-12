. (Join-Path $PSScriptRoot 'environment.ps1')
$uvExecutable = Join-Path $WorkbenchRoot '.tools\bin\uv.exe'
if (-not (Test-Path -LiteralPath $uvExecutable)) {
    throw 'Run scripts\bootstrap.ps1 first.'
}
& $uvExecutable --directory $WorkbenchRoot --project $WorkbenchRoot @args
exit $LASTEXITCODE
