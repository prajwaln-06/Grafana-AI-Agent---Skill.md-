<#
.SYNOPSIS
    Check that every simulator service is up AND actually has data.
.DESCRIPTION
    "Container is running" does not mean "simulator is usable" -- this
    checks Docker's own HEALTHCHECK status for every service, then
    additionally confirms the two PUBLIC agent-facing endpoints
    (Prometheus :9090, OpenSearch :9600) actually contain simulated data,
    not just that they respond.
#>
$ErrorActionPreference = "Continue"

function Get-ServiceHealth([string]$service) {
    $cid = (docker compose ps -q $service 2>$null)
    if ([string]::IsNullOrWhiteSpace($cid)) { return "NOT RUNNING" }
    $status = docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' $cid 2>$null
    # docker's output can come back as a string[] (one element per line) on
    # some PowerShell versions; Out-String reliably flattens either shape.
    $status = ($status | Out-String).Trim()
    if ([string]::IsNullOrWhiteSpace($status)) { return "UNKNOWN" }
    return $status
}

function Write-Check([string]$name, [string]$status, [bool]$ok) {
    if ($ok) {
        Write-Host ("[OK]   {0,-22} {1}" -f $name, $status) -ForegroundColor Green
    } else {
        Write-Host ("[FAIL] {0,-22} {1}" -f $name, $status) -ForegroundColor Red
    }
}

Write-Host "Simulator Health Check" -ForegroundColor Cyan
Write-Host "----------------------"

$services = @(
    @{ Name = "Incident Controller"; Svc = "incident-controller" },
    @{ Name = "node-00";             Svc = "node-00" },
    @{ Name = "node-01";             Svc = "node-01" },
    @{ Name = "node-02";             Svc = "node-02" },
    @{ Name = "node-03";             Svc = "node-03" },
    @{ Name = "Prometheus";          Svc = "prometheus" },
    @{ Name = "OpenSearch";          Svc = "opensearch" },
    @{ Name = "Log Simulator";       Svc = "logsim" }
)

$allOk = $true
foreach ($s in $services) {
    $status = Get-ServiceHealth $s.Svc
    $ok = ($status -eq "healthy") -or ($status -eq "running")
    Write-Check $s.Name $status $ok
    if (-not $ok) { $allOk = $false }
}

Write-Host ""
Write-Host "Data availability (the two PUBLIC agent endpoints)" -ForegroundColor Cyan
Write-Host "----------------------------------------------------"

try {
    $up = Invoke-RestMethod "http://localhost:9090/api/v1/query?query=up" -TimeoutSec 5
    $fleet = @($up.data.result | Where-Object { $_.metric.job -eq "simulated_fleet" })
    if ($fleet.Count -ge 1) {
        Write-Check "Prometheus contains metrics" ("{0} simulated target(s) up" -f $fleet.Count) $true
    } else {
        Write-Check "Prometheus contains metrics" "reachable, but no simulated_fleet targets yet" $false
        $allOk = $false
    }
} catch {
    Write-Check "Prometheus reachable" "http://localhost:9090 not responding" $false
    $allOk = $false
}

try {
    $indices = Invoke-RestMethod "http://localhost:9600/_cat/indices?format=json" -TimeoutSec 5
    if (@($indices).Count -ge 1) {
        $names = ($indices | ForEach-Object { $_.index }) -join ", "
        Write-Check "OpenSearch contains indices" ("{0}" -f $names) $true
    } else {
        Write-Check "OpenSearch contains indices" "reachable, but no indices yet" $false
        $allOk = $false
    }
} catch {
    Write-Check "OpenSearch reachable" "http://localhost:9600 not responding" $false
    $allOk = $false
}

Write-Host ""
if ($allOk) {
    Write-Host "Simulator is READY" -ForegroundColor Green
    exit 0
} else {
    Write-Host "Simulator is NOT READY yet." -ForegroundColor Yellow
    Write-Host "On first startup this can take 1-2 minutes (OpenSearch JVM boot + index creation)." -ForegroundColor Yellow
    Write-Host "Wait a bit and re-run '.\health.ps1'. If it's still failing after 3+ minutes, run:" -ForegroundColor Yellow
    Write-Host "  docker compose logs opensearch" -ForegroundColor Gray
    Write-Host "  docker compose logs logsim" -ForegroundColor Gray
    exit 1
}
