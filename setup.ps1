$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Venv = Join-Path $Root '.venv'
$Env:HF_HOME = Join-Path $Root '.hf-cache'

if (-not (Test-Path (Join-Path $Venv 'Scripts\python.exe'))) {
    $HostPython = Get-Command python -ErrorAction SilentlyContinue
    if (-not $HostPython) { throw 'python was not found on PATH' }
    & $HostPython.Source -m venv $Venv
}

$Python = Join-Path $Venv 'Scripts\python.exe'
& $Python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'pip upgrade failed' }
& $Python -m pip install -e ".[dev,whisper]"
if ($LASTEXITCODE -ne 0) { throw 'project dependency installation failed' }
Write-Host "ready: $Python"
Write-Host "HF_HOME: $Env:HF_HOME"
Write-Host "first run downloads the selected faster-whisper model into .hf-cache"
