$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Venv = Join-Path $Root '.venv'

if (-not (Test-Path (Join-Path $Venv 'Scripts\python.exe'))) {
    $HostPython = Get-Command python -ErrorAction SilentlyContinue
    if (-not $HostPython) { throw 'python was not found on PATH' }
    & $HostPython.Source -m venv $Venv
}

$Python = Join-Path $Venv 'Scripts\python.exe'
& $Python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'pip upgrade failed' }
& $Python -m pip install -e "$Root[web,whisper,dev]"
if ($LASTEXITCODE -ne 0) { throw 'project dependency installation failed' }
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Warning 'ffmpeg was not found on PATH; install FFmpeg before analyzing or rendering'
}
Write-Host "ready: $Python"
Write-Host 'the first transcription downloads the selected faster-whisper model into the Hugging Face cache'
Write-Host 'next: .\install-desktop.ps1 puts a SmartCut shortcut on your desktop'
