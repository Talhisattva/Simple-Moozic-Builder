param(
    [Parameter(Mandatory = $true)]
    [string]$Version,
    [string]$DistRoot = ".\dist\SimpleMoozicBuilder",
    [string]$OutDir = ".\dist",
    [string]$FfmpegSource = ".\ffmpeg"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $DistRoot)) {
    throw "Dist root not found: $DistRoot"
}

New-Item -ItemType Directory -Path $OutDir -Force | Out-Null

$zipLite = Join-Path $OutDir ("SimpleMoozicBuilder-v{0}-lite.zip" -f $Version)
$zipCompat = Join-Path $OutDir ("SimpleMoozicBuilder-v{0}-compat-ffmpeg.zip" -f $Version)

if (Test-Path $zipLite) { Remove-Item $zipLite -Force }
if (Test-Path $zipCompat) { Remove-Item $zipCompat -Force }

$stageRoot = Join-Path $env:TEMP ("smb_release_" + [guid]::NewGuid().ToString("N"))
$stageLite = Join-Path $stageRoot "lite\SimpleMoozicBuilder"
$stageCompat = Join-Path $stageRoot "compat\SimpleMoozicBuilder"
New-Item -ItemType Directory -Path $stageLite -Force | Out-Null
New-Item -ItemType Directory -Path $stageCompat -Force | Out-Null

Copy-Item -Path (Join-Path $DistRoot "*") -Destination $stageCompat -Recurse -Force
Copy-Item -Path (Join-Path $DistRoot "*") -Destination $stageLite -Recurse -Force

$liteFfmpeg = Join-Path $stageLite "ffmpeg"
if (Test-Path $liteFfmpeg) {
    Remove-Item $liteFfmpeg -Recurse -Force
}

if (Test-Path $FfmpegSource) {
    $compatFfmpeg = Join-Path $stageCompat "ffmpeg"
    if (Test-Path $compatFfmpeg) {
        Remove-Item $compatFfmpeg -Recurse -Force
    }
    Copy-Item -Path $FfmpegSource -Destination $stageCompat -Recurse -Force
} else {
    Write-Warning "ffmpeg source folder not found: $FfmpegSource (compat build will not include ffmpeg)"
}

Compress-Archive -Path (Join-Path $stageLite "..\*") -DestinationPath $zipLite -Force
Compress-Archive -Path (Join-Path $stageCompat "..\*") -DestinationPath $zipCompat -Force

Remove-Item $stageRoot -Recurse -Force

Write-Host "Created:"
Write-Host " - $zipLite"
Write-Host " - $zipCompat"
