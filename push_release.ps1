param(
    [string]$RepoPath = "C:\Users\chowl\Zomboid\Workshop\Simple-Moozic-Builder-Git"
)

$ErrorActionPreference = "Stop"

Write-Host "Clearing proxy environment variables for this session..."
$env:ALL_PROXY = $null
$env:HTTP_PROXY = $null
$env:HTTPS_PROXY = $null
$env:GIT_HTTP_PROXY = $null
$env:GIT_HTTPS_PROXY = $null

Write-Host "Git status:"
git -C $RepoPath status --short --branch

Write-Host ""
Write-Host "Pushing current branch..."
git -C $RepoPath push

Write-Host ""
Write-Host "Push complete."
