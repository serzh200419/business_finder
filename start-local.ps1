param([ValidateRange(1,65535)][int]$Port = 8000, [switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONIOENCODING = 'utf-8'
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
try {
    if (-not (Test-Path -LiteralPath $pythonPath)) {
        if (Get-Command py -ErrorAction SilentlyContinue) { & py -3 -m venv .venv }
        elseif (Get-Command python -ErrorAction SilentlyContinue) { & python -m venv .venv }
        else { throw 'Install Python 3.11 or newer, enable its launcher/PATH, then rerun this script.' }
        if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed. Install Python 3.11+ with venv support.' }
    }
    & $pythonPath -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)'
    if ($LASTEXITCODE -ne 0) { throw 'The existing virtual environment needs Python 3.11+. Preserve it and create a compatible .venv before continuing.' }
    $manifestHash = (Get-FileHash -LiteralPath 'pyproject.toml' -Algorithm SHA256).Hash
    $stampPath = Join-Path $PSScriptRoot '.venv\local-dependencies.sha256'
    $previousHash = if (Test-Path -LiteralPath $stampPath) { (Get-Content -LiteralPath $stampPath -Raw).Trim() } else { '' }
    # Check declared dependency versions and editable import before deciding to install.
    @'
import importlib.metadata as m, tomllib
from pip._vendor.packaging.requirements import Requirement
import app
with open('pyproject.toml', 'rb') as manifest:
    requirements = [Requirement(v) for v in tomllib.load(manifest)['project']['dependencies']]
assert all(m.version(v.name) in v.specifier for v in requirements)
'@ | & $pythonPath - *> $null
    $dependenciesReady = $LASTEXITCODE -eq 0
    if (-not $dependenciesReady -or ($previousHash -and $previousHash -ne $manifestHash)) {
        Write-Host 'Installing required dependencies (internet needed on first setup)...'
        # Package sources may contain credentials; report actionable errors, not pip output.
        & $pythonPath -m pip install -e . *> $null
        if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check internet access, Python version and your pip configuration, then rerun.' }
    }
    Set-Content -LiteralPath $stampPath -Value $manifestHash
    if (-not (Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env' }
    if ($CheckOnly) { & $pythonPath -m app.local_start --port $Port --migrate-only }
    else { & $pythonPath -m app.local_start --port $Port }
    exit $LASTEXITCODE
} catch {
    Write-Host "Startup error: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
