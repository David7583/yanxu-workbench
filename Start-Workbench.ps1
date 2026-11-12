[CmdletBinding()]
param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$WorkbenchDirectory = $PSScriptRoot
. (Join-Path $WorkbenchDirectory 'scripts\environment.ps1')
$pythonExecutable = Join-Path $WorkbenchDirectory '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExecutable)) { throw 'Run scripts\bootstrap.ps1 first.' }
if (-not (Test-Path -LiteralPath (Join-Path $WorkbenchDirectory 'frontend\dist\index.html'))) {
    Push-Location $WorkbenchDirectory
    try { & npm.cmd run build; if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' } }
    finally { Pop-Location }
}
$baseUrl = 'http://127.0.0.1:8765'
$existing = $false
try { $health = Invoke-RestMethod -Uri "$baseUrl/api/health" -TimeoutSec 2; $existing = $health.status -eq 'ok' -and $health.name -eq '研序' -and $health.storage -eq 'local' } catch { }
if (-not $existing) {
    $stateDirectory = Join-Path $WorkbenchDirectory '.state'
    New-Item -ItemType Directory -Force -Path $stateDirectory | Out-Null
    Push-Location $WorkbenchDirectory
    try { & $pythonExecutable -m backend.storage_migrate; if ($LASTEXITCODE -ne 0) { throw 'Storage migration failed. Original data retained.' } }
    finally { Pop-Location }
    $serverProcess = Start-Process -FilePath $pythonExecutable -ArgumentList @('-m','uvicorn','backend.app:app','--host','127.0.0.1','--port','8765') -WorkingDirectory $WorkbenchDirectory -WindowStyle Hidden -RedirectStandardOutput (Join-Path $stateDirectory 'server.log') -RedirectStandardError (Join-Path $stateDirectory 'server-error.log') -PassThru
    $ready = $false
    for ($attempt = 0; $attempt -lt 25; $attempt++) {
        Start-Sleep -Milliseconds 400
        if ($serverProcess.HasExited) { throw 'Server exited. Check .state\server-error.log.' }
        try { $health = Invoke-RestMethod -Uri "$baseUrl/api/health" -TimeoutSec 1; if ($health.status -eq 'ok' -and $health.name -eq '研序' -and $health.storage -eq 'local') { $ready = $true; break } } catch { }
    }
    if (-not $ready) { throw 'Server is taking longer than expected. Check .state\server-error.log.' }
    @{ pid = $serverProcess.Id; started = $serverProcess.StartTime.ToUniversalTime().ToString('o'); url = $baseUrl } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $stateDirectory 'server.json') -Encoding utf8
}
Write-Output "Yanxu is running at $baseUrl"
if (-not $NoBrowser) { Start-Process $baseUrl }
