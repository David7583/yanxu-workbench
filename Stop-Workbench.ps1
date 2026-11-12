$ErrorActionPreference = 'Stop'
$stateFile = Join-Path $PSScriptRoot '.state\server.json'
if (-not (Test-Path -LiteralPath $stateFile)) { Write-Output 'No owned server record found.'; exit 0 }
$record = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
$serverProcess = Get-Process -Id $record.pid -ErrorAction SilentlyContinue
if ($serverProcess) {
    $expectedExecutable = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '.venv\Scripts\python.exe'))
    $expectedStart = ([DateTime]$record.started).ToUniversalTime()
    if ($serverProcess.Path -ne $expectedExecutable -or $serverProcess.StartTime.ToUniversalTime().Ticks -ne $expectedStart.Ticks) {
        throw 'Process identity changed; refusing to stop it.'
    }
    # uv's venv launcher owns a child interpreter; terminate that verified process tree.
    & taskkill.exe /PID $serverProcess.Id /T /F | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'The owned server could not be stopped.' }
}
Remove-Item -LiteralPath $stateFile
Write-Output 'Yanxu stopped.'
