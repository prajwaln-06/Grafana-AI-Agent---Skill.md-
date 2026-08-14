<#
.SYNOPSIS
    Stop the prom-simulator Docker stack.
.DESCRIPTION
    Stops and removes the containers/network (docker compose down). Built
    images are left cached, so the next '.\start.ps1' is fast. No OpenSearch
    data volume is defined by design (this is a disposable test simulator),
    so state doesn't persist across this anyway -- use '.\reset.ps1' instead
    if you just want to clear active incidents without stopping anything.
#>
$ErrorActionPreference = "Stop"

Write-Host "==> Stopping prom-simulator..." -ForegroundColor Cyan
docker compose down

if ($LASTEXITCODE -eq 0) {
    Write-Host "Stopped. Run '.\start.ps1' to bring it back up." -ForegroundColor Green
} else {
    Write-Host "docker compose down reported an error -- see output above." -ForegroundColor Red
    exit 1
}
