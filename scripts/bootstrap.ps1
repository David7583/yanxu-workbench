[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'environment.ps1')
$uvVersion = '0.13.7'
$uvChecksum = '0cdc5b38fe5a8168ed6a25814f5ba43c21d313dd3187314fccb37b7cec46e021'
$uvExecutable = Join-Path $WorkbenchRoot '.tools\bin\uv.exe'
if (-not (Test-Path -LiteralPath $uvExecutable)) {
    $downloads = Join-Path $WorkbenchRoot '.tools\downloads'
    $binaryDirectory = Join-Path $WorkbenchRoot '.tools\bin'
    New-Item -ItemType Directory -Force -Path $downloads,$binaryDirectory | Out-Null
    $archive = Join-Path $downloads 'uv-x86_64-pc-windows-msvc.zip'
    Invoke-WebRequest -Uri "https://github.com/astral-sh/uv/releases/download/$uvVersion/uv-x86_64-pc-windows-msvc.zip" -OutFile $archive
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $uvChecksum) {
        throw 'uv archive checksum mismatch.'
    }
    Expand-Archive -LiteralPath $archive -DestinationPath $binaryDirectory -Force
}
$pythonVersion = (Get-Content -LiteralPath (Join-Path $WorkbenchRoot '.python-version') -Raw).Trim()
$managedPython = Join-Path $env:UV_PYTHON_INSTALL_DIR "cpython-$pythonVersion-windows-x86_64-none\python.exe"
if (-not (Test-Path -LiteralPath $managedPython)) {
    & $uvExecutable --no-config python install $pythonVersion --no-bin --no-registry
    if ($LASTEXITCODE -ne 0) { throw 'Local Python installation failed.' }
}
# Use the exact interpreter path so the venv does not depend on a minor-version junction.
& $uvExecutable --directory $WorkbenchRoot --project $WorkbenchRoot sync --python $managedPython
if ($LASTEXITCODE -ne 0) { throw 'Virtual environment sync failed.' }
New-Item -ItemType Directory -Force -Path (Join-Path $WorkbenchRoot 'data\projects'),(Join-Path $WorkbenchRoot 'data\library'),(Join-Path $WorkbenchRoot 'exports'),(Join-Path $WorkbenchRoot '.state') | Out-Null
& (Join-Path $WorkbenchRoot '.venv\Scripts\python.exe') -I (Join-Path $PSScriptRoot 'check_environment.py')
if ($LASTEXITCODE -ne 0) { throw 'Environment verification failed.' }
