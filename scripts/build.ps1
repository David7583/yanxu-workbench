$ErrorActionPreference = 'Stop'
$WorkbenchDirectory = Split-Path -Parent $PSScriptRoot
Push-Location $WorkbenchDirectory
try {
    $env:npm_config_cache = Join-Path $WorkbenchDirectory '.cache\npm'
    & npm.cmd ci --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed.' }
    & npm.cmd run build
    if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
} finally { Pop-Location }
