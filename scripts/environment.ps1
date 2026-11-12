# Dot-source to configure only the current PowerShell process.
$WorkbenchRoot = Split-Path -Parent $PSScriptRoot
$env:UV_PYTHON_INSTALL_DIR = Join-Path $WorkbenchRoot '.tools\python'
$env:UV_PYTHON_BIN_DIR = Join-Path $WorkbenchRoot '.tools\bin'
$env:UV_TOOL_DIR = Join-Path $WorkbenchRoot '.tools\uv-tools'
$env:UV_TOOL_BIN_DIR = Join-Path $WorkbenchRoot '.tools\bin'
$env:UV_CACHE_DIR = Join-Path $WorkbenchRoot '.cache\uv'
$env:UV_PROJECT_ENVIRONMENT = Join-Path $WorkbenchRoot '.venv'
$env:VIRTUAL_ENV = $env:UV_PROJECT_ENVIRONMENT
$env:UV_PYTHON_INSTALL_REGISTRY = '0'
$env:UV_PYTHON_INSTALL_BIN = '0'
$env:UV_MANAGED_PYTHON = '1'
$env:PYTHONNOUSERSITE = '1'
$env:PYTHONUTF8 = '1'
$env:PIP_REQUIRE_VIRTUALENV = 'true'
$env:PIP_CACHE_DIR = Join-Path $WorkbenchRoot '.cache\pip'
$env:TEMP = Join-Path $WorkbenchRoot '.cache\tmp'
$env:TMP = $env:TEMP
# Prevent an inherited Conda/Python configuration from contaminating this workspace.
Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $env:TEMP | Out-Null
