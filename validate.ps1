<#
.SYNOPSIS
    Full simulator smoke test: infrastructure, metrics, logs, and a real
    incident-correlation check across both Prometheus and OpenSearch.
.DESCRIPTION
    This is the definitive "is the simulator actually usable" test. It
    triggers a real gpu_overheating incident via the internal test-control
    endpoint (:9500) and then confirms the SAME incident is independently
    observable through BOTH public agent-facing endpoints:
        Prometheus  http://localhost:9090
        OpenSearch  http://localhost:9600
    At the end it resets incident state so the simulator is left clean.
#>
$ErrorActionPreference = "Continue"

$PROM = "http://localhost:9090"
$OS   = "http://localhost:9600"
$CTRL = "http://localhost:9500"

$results = New-Object System.Collections.Generic.List[Object]
$anyFail = $false

function Add-Result([string]$section, [string]$name, [bool]$ok, [string]$detail = "") {
    $results.Add([PSCustomObject]@{ Section = $section; Name = $name; Ok = $ok; Detail = $detail })
}

function Show-SectionResults {
    foreach ($r in $results) {
        $mark = if ($r.Ok) { "[PASS]" } else { "[FAIL]" }
        $color = if ($r.Ok) { "Green" } else { "Red" }
        Write-Host ("  {0} {1} {2}" -f $mark, $r.Name, $(if ($r.Detail) { "- $($r.Detail)" } else { "" })) -ForegroundColor $color
    }
    if (@($results | Where-Object { -not $_.Ok }).Count -gt 0) { $script:anyFail = $true }
    $results.Clear()
}

function Invoke-PromQL([string]$query) {
    $encoded = [System.Uri]::EscapeDataString($query)
    return Invoke-RestMethod "$PROM/api/v1/query?query=$encoded" -TimeoutSec 10
}

function Test-PromQLNonEmpty([string]$section, [string]$name, [string]$query) {
    try {
        $resp = Invoke-PromQL $query
        $count = @($resp.data.result).Count
        Add-Result $section $name ($count -ge 1) "$count series"
    } catch {
        Add-Result $section $name $false "query failed: $($_.Exception.Message)"
    }
}

function Test-OpenSearchIndexHasDocs([string]$section, [string]$name, [string]$indexPattern) {
    try {
        $resp = Invoke-RestMethod "$OS/$indexPattern/_count" -TimeoutSec 10
        Add-Result $section $name ($resp.count -ge 1) "$($resp.count) docs"
    } catch {
        Add-Result $section $name $false "query failed: $($_.Exception.Message)"
    }
}

Write-Host "====================================" -ForegroundColor Cyan
Write-Host " PROM-SIMULATOR VALIDATION" -ForegroundColor Cyan
Write-Host "====================================" -ForegroundColor Cyan
Write-Host ""

# ---------------------------------------------------------------------
Write-Host "Infrastructure" -ForegroundColor White
try { Invoke-RestMethod "$PROM/-/ready" -TimeoutSec 5 | Out-Null; Add-Result "Infrastructure" "Prometheus reachable" $true }
catch { Add-Result "Infrastructure" "Prometheus reachable" $false $_.Exception.Message }

try { Invoke-RestMethod "$OS/" -TimeoutSec 5 | Out-Null; Add-Result "Infrastructure" "OpenSearch reachable" $true }
catch { Add-Result "Infrastructure" "OpenSearch reachable" $false $_.Exception.Message }

try { $h = Invoke-RestMethod "$CTRL/health" -TimeoutSec 5; Add-Result "Infrastructure" "Incident controller reachable" ($h.status -eq "ok") }
catch { Add-Result "Infrastructure" "Incident controller reachable" $false $_.Exception.Message }

$exporterOk = $true
foreach ($port in 9200, 9201, 9202, 9203) {
    try { Invoke-WebRequest "http://localhost:$port/metrics" -TimeoutSec 5 -UseBasicParsing | Out-Null }
    catch { $exporterOk = $false }
}
Add-Result "Infrastructure" "All 4 exporters reachable" $exporterOk

try {
    $cid = docker compose ps -q logsim 2>$null
    $status = docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' $cid 2>$null
    $status = ($status | Out-String).Trim()
    Add-Result "Infrastructure" "Log simulator running" (($status -eq "healthy") -or ($status -eq "running")) $status
} catch { Add-Result "Infrastructure" "Log simulator running" $false }

Show-SectionResults

# ---------------------------------------------------------------------
Write-Host ""
Write-Host "Metrics" -ForegroundColor White
Test-PromQLNonEmpty "Metrics" "Node metrics available"       "node_load1"
Test-PromQLNonEmpty "Metrics" "GPU metrics available"         "DCGM_FI_DEV_GPU_UTIL"
Test-PromQLNonEmpty "Metrics" "Memory metrics available"      "node_memory_MemAvailable_bytes"
Test-PromQLNonEmpty "Metrics" "Filesystem metrics available"  "node_filesystem_avail_bytes"
Show-SectionResults

# ---------------------------------------------------------------------
Write-Host ""
Write-Host "Logs" -ForegroundColor White
Test-OpenSearchIndexHasDocs "Logs" "syslog documents available"     "syslog-*"
Test-OpenSearchIndexHasDocs "Logs" "consolelog documents available" "consolelog-*"
Test-OpenSearchIndexHasDocs "Logs" "heartbeat documents available"  "heartbeat"
Show-SectionResults

# ---------------------------------------------------------------------
Write-Host ""
Write-Host "Correlation (live incident injection)" -ForegroundColor White

$triggerBody = @{
    scenario_id  = "gpu_overheating"
    node         = "node-02"
    gpu          = 3
    start_after  = 2
    duration     = 45
} | ConvertTo-Json

$injected = $false
try {
    Invoke-RestMethod -Method POST -Uri "$CTRL/scenarios/trigger" -ContentType "application/json" -Body $triggerBody -TimeoutSec 5 | Out-Null
    $injected = $true
} catch {
    $injected = $false
}
Add-Result "Correlation" "Incident injected (gpu_overheating on node-02/gpu3)" $injected

if ($injected) {
    Write-Host "  Waiting ~35s for the metric anomaly and correlated logs to appear..." -ForegroundColor Gray
    Start-Sleep -Seconds 35

    try {
        $resp = Invoke-PromQL 'DCGM_FI_DEV_GPU_TEMP{node_id="node-02",gpu="3"}'
        $val = [double]($resp.data.result[0].value[1])
        Add-Result "Correlation" "Metric anomaly detected (GPU temp elevated)" ($val -gt 65) ("temp={0}" -f $val)
    } catch {
        Add-Result "Correlation" "Metric anomaly detected (GPU temp elevated)" $false $_.Exception.Message
    }

    try {
        $searchBody = @{
            size  = 5
            query = @{
                bool = @{
                    must = @(
                        @{ term  = @{ "Resource.host.name" = "node-02" } },
                        @{ match = @{ Body = "GPU" } }
                    )
                }
            }
        } | ConvertTo-Json -Depth 10
        $resp = Invoke-RestMethod -Method POST -Uri "$OS/syslog-*/_search" -ContentType "application/json" -Body $searchBody -TimeoutSec 10
        $hits = $resp.hits.total.value
        Add-Result "Correlation" "Correlated logs detected" ($hits -ge 1) ("$hits matching doc(s)")
    } catch {
        Add-Result "Correlation" "Correlated logs detected" $false $_.Exception.Message
    }
} else {
    Add-Result "Correlation" "Metric anomaly detected (GPU temp elevated)" $false "skipped -- injection failed"
    Add-Result "Correlation" "Correlated logs detected" $false "skipped -- injection failed"
}

Show-SectionResults

# cleanup so repeated validate.ps1 runs don't pile up incidents
try { Invoke-RestMethod -Method POST -Uri "$CTRL/scenarios/reset" -TimeoutSec 5 | Out-Null } catch {}

Write-Host ""
Write-Host "====================================" -ForegroundColor Cyan
if (-not $anyFail) {
    Write-Host " SIMULATOR READY" -ForegroundColor Green
    Write-Host "====================================" -ForegroundColor Cyan
    exit 0
} else {
    Write-Host " SIMULATOR VALIDATION FAILED -- see [FAIL] lines above" -ForegroundColor Red
    Write-Host "====================================" -ForegroundColor Cyan
    exit 1
}
