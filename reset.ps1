<#
.SYNOPSIS
    Clear all active/future incident scenarios without restarting Docker.
.DESCRIPTION
    Calls the incident controller's internal reset endpoint. This clears
    scenario state on BOTH sides -- the metric effects exporter.py applies
    AND any already-scheduled-but-not-yet-fired OpenSearch log events in
    logsim -- so a reset genuinely returns the simulator to a clean slate
    for the next test, without touching containers, Prometheus, or
    OpenSearch data.

    This hits an INTERNAL test-control endpoint (localhost:9500), not one
    of the two public agent-facing endpoints (9090 / 9600).
#>
$ErrorActionPreference = "Stop"

Write-Host "==> Resetting simulator incident state..." -ForegroundColor Cyan
try {
    $resp = Invoke-RestMethod -Method POST -Uri "http://localhost:9500/scenarios/reset" -TimeoutSec 5
    Write-Host ("Reset complete (epoch {0}). All active/future scenarios cleared." -f $resp.reset_epoch) -ForegroundColor Green
    Write-Host "Containers, Prometheus, and OpenSearch were not touched -- only incident state was cleared." -ForegroundColor Gray
} catch {
    Write-Host "Could not reach the incident controller at http://localhost:9500." -ForegroundColor Red
    Write-Host "Is the stack running? Try '.\health.ps1' first." -ForegroundColor Red
    exit 1
}
