param(
    [switch]$InstallFfmpeg
)

$ErrorActionPreference = "Stop"

function Require-Command {
    param([string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command not found: $Name"
    }
}

Require-Command "python"

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repoRoot

if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment in .venv ..."
    python -m venv .venv
}

$py = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    throw "Virtual environment python not found: $py"
}

Write-Host "Upgrading pip ..."
& $py -m pip install --upgrade pip

Write-Host "Installing Python dependencies from requirements.txt ..."
& $py -m pip install -r requirements.txt

if ($InstallFfmpeg) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "Installing ffmpeg via winget ..."
        winget install --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements --silent
    } else {
        Write-Warning "winget not available. Install ffmpeg manually or keep local ffmpeg binaries."
    }
}

Write-Host ""
Write-Host "Setup complete."
Write-Host "Activate venv: .\\.venv\\Scripts\\Activate.ps1"
Write-Host "Run app:       python .\\simple_moozic_builder_ui.py"
