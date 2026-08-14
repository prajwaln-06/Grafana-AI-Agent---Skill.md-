<#
.SYNOPSIS
    Build and start the prom-simulator Docker stack.
.DESCRIPTION
    This is the ONLY command you need to bring the simulator up. No Python,
    pip, or host-side setup required -- everything runs inside Docker.
#>
$ErrorActionPreference = "Stop"

Write-Host "==> Building and starting prom-simulator..." -ForegroundColor Cyan
docker compose up -d --build

if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "docker compose up failed -- see the output above." -ForegroundColor Red
    Write-Host "Common causes: Docker Desktop isn't running, or a port (9090/9200-9203/9500/9600) is already in use." -ForegroundColor Yellow
    exit 1
}

Write-Host ""
Write-Host "Containers are starting. On first run this can take 1-2 minutes" -ForegroundColor Yellow
Write-Host "(OpenSearch's JVM startup is the slow part)." -ForegroundColor Yellow
Write-Host ""
Write-Host "Next:" -ForegroundColor Cyan
Write-Host "  .\health.ps1    # check every service is up and has real data"
Write-Host "  .\validate.ps1  # full smoke test incl. a real incident + correlation check"
