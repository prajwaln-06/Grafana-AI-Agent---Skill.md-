# Manual QA helpers for the agent + simulator (Windows PowerShell 5.1 compatible).
#
# Load once per terminal:   . .\scripts\qa_helpers.ps1
#
#   Ask-Agent "question"                      ask the agent, print a readable answer
#   Ask-Agent "question" -Raw                 print only the raw backend JSON (what the frontend gets)
#   Show-Raw [-AsSent]                         raw backend JSON of the LAST Ask-Agent answer
#   Prom ... -Raw  /  OS ... -Raw              Prometheus / OpenSearch native response, untouched
#   Recheck                                    re-run the agent's own last queries directly (same window)
#   Prom 'promql' [-Range -Minutes 30 -Step 60]   ask Prometheus directly (the ground truth)
#   OS 'index-pattern' 'json-dsl'              ask OpenSearch directly (the ground truth)
#   SimEvents                                  what the simulator actually injected (the answer key)
#
# Override the endpoints with $env:AGENT_URL / PROM_URL / OS_URL / SIM_URL if yours differ.

function _envOr([string]$name, [string]$default) {
    $v = [Environment]::GetEnvironmentVariable($name)
    if ([string]::IsNullOrEmpty($v)) { return $default } else { return $v }
}
$script:AgentUrl = _envOr "AGENT_URL" "http://localhost:8100"
$script:PromUrl  = _envOr "PROM_URL"  "http://localhost:9090"
$script:OsUrl    = _envOr "OS_URL"    "http://localhost:9250"
$script:SimUrl   = _envOr "SIM_URL"   "http://localhost:8010"
$script:Py       = _envOr "PY"        "C:\Users\csepr\Downloads\Promethues-Opensearch\.venv\Scripts\python.exe"
$script:RawFile  = Join-Path $env:TEMP "last_agent_response.json"
# Tables are cut to the terminal width so the VS Code terminal never wraps (and mis-redraws) them.
function _w { try { return [Math]::Max(80, $Host.UI.RawUI.WindowSize.Width - 1) } catch { return 200 } }

function _hdr([string]$t) { Write-Host ""; Write-Host ("=" * 78) -ForegroundColor DarkGray; Write-Host $t -ForegroundColor Cyan; Write-Host ("=" * 78) -ForegroundColor DarkGray }
function _kv([string]$k, $v) { if ($null -ne $v -and "$v" -ne "") { Write-Host ("{0,-16}" -f $k) -NoNewline -ForegroundColor Yellow; Write-Host " $v" } }
function _json($o) { if ($null -eq $o) { return "" }; if ($o -is [string]) { return $o }; return ($o | ConvertTo-Json -Depth 30 -Compress) }
function _short([string]$s, [int]$n = 110) { if ($null -eq $s) { return "" }; $s = $s -replace "\s+", " "; if ($s.Length -gt $n) { return $s.Substring(0, $n - 3) + "..." }; return $s }
function _labels($l, [string[]]$skip = @("__name__", "cluster", "job", "instance")) {
    if ($null -eq $l) { return "{}" }
    $parts = @(); foreach ($p in $l.PSObject.Properties) { if ($skip -notcontains $p.Name) { $parts += "$($p.Name)=$($p.Value)" } }
    if ($parts.Count -eq 0) { return "{}" }; return ($parts -join ", ")
}
function _num($v) { if ($null -eq $v) { return "null" }; return [math]::Round([double]$v, 3) }

# ---- buckets (agent normalised shape and raw OpenSearch shape) -----------------
function _flattenAgentBuckets($buckets, [string]$prefix = "") {
    foreach ($b in $buckets) {
        $key = if ($prefix) { "$prefix | $($b.key)" } else { "$($b.key)" }
        if ($b.sub_buckets) { _flattenAgentBuckets $b.sub_buckets $key }
        else {
            $m = if ($b.metrics) { _json $b.metrics } else { "" }
            [pscustomobject]@{ Key = $key; Count = $b.doc_count; Metrics = $m }
        }
    }
}
function _flattenOsAggs($aggs, [string]$prefix = "") {
    foreach ($p in $aggs.PSObject.Properties) {
        $a = $p.Value
        if ($a -and $a.PSObject.Properties["buckets"]) {
            foreach ($b in $a.buckets) {
                $k = if ($b.key_as_string) { $b.key_as_string } else { "$($b.key)" }
                $key = if ($prefix) { "$prefix | $k" } else { $k }
                $sub = $false
                foreach ($q in $b.PSObject.Properties) { if ($q.Value -and $q.Value.PSObject -and $q.Value.PSObject.Properties["buckets"]) { $sub = $true } }
                if ($sub) {
                    $inner = New-Object psobject
                    foreach ($q in $b.PSObject.Properties) { if ($q.Value -and $q.Value.PSObject -and $q.Value.PSObject.Properties["buckets"]) { $inner | Add-Member -NotePropertyName $q.Name -NotePropertyValue $q.Value } }
                    _flattenOsAggs $inner $key
                } else { [pscustomobject]@{ Agg = $p.Name; Key = $key; Count = $b.doc_count } }
            }
        } elseif ($a -and $a.PSObject.Properties["value"]) {
            [pscustomobject]@{ Agg = $p.Name; Key = "(value)"; Count = $a.value }
        }
    }
}

# ---- agent ---------------------------------------------------------------------
function _showExecution($ex) {
    if ($null -eq $ex) { Write-Host "  (no execution block: the query was not run)" -ForegroundColor DarkYellow; return }
    $color = if ($ex.execution_status -eq "success") { "Green" } else { "Red" }
    Write-Host ("{0,-16}" -f "execution") -NoNewline -ForegroundColor Yellow; Write-Host " $($ex.execution_status)" -ForegroundColor $color
    _kv "error" $ex.error
    _kv "time window" (_json $ex.resolved_time_range)
    if ($ex.metadata -and $ex.metadata.count) { _kv "total matched" "$($ex.metadata.count.value) ($($ex.metadata.count.relation))" }
    if ($ex.truncated) { _kv "truncated" "yes (original $($ex.original_count))" }
    if ($ex.analysis) { _kv "analysis" (_json $ex.analysis) }
    if ($ex.metadata -and $ex.metadata.text_search_note) {
        Write-Host ("{0,-16}" -f "NOTE") -NoNewline -ForegroundColor Yellow; Write-Host " $($ex.metadata.text_search_note)" -ForegroundColor DarkYellow
    }

    switch ($ex.result_type) {
        "series" {
            Write-Host "`n  Series ($($ex.series_count)):" -ForegroundColor Cyan
            $ex.series | ForEach-Object {
                $vals = @($_.points | Where-Object { $null -ne $_.value } | ForEach-Object { [double]$_.value })
                $last = $_.points | Select-Object -Last 1
                $name = if ($_.legend_label) { $_.legend_label } else { _labels $_.labels }
                if ($vals.Count -gt 0) { $st = $vals | Measure-Object -Minimum -Maximum -Average } else { $st = $null }
                [pscustomobject]@{
                    Series = $name; Points = @($_.points).Count
                    Min = if ($st) { _num $st.Minimum } else { "-" }; Avg = if ($st) { _num $st.Average } else { "-" }
                    Max = if ($st) { _num $st.Maximum } else { "-" }; Last = _num $last.value; LastAt = $last.timestamp
                }
            } | Format-Table -AutoSize | Out-String -Width (_w) | Write-Host
        }
        "buckets" {
            Write-Host "`n  Buckets ($($ex.bucket_count)):" -ForegroundColor Cyan
            _flattenAgentBuckets $ex.buckets | Format-Table -AutoSize | Out-String -Width (_w) | Write-Host
        }
        "hits" {
            Write-Host "`n  Rows ($($ex.hit_count) shown of $($ex.total_hits)):" -ForegroundColor Cyan
            $ex.hits | ForEach-Object {
                $src = if ($_.fields) { $_.fields } else { $_.resource }
                $who = ""
                if ($src) { foreach ($p in $src.PSObject.Properties) { if ($p.Name -match "host|Switch|Location") { $who = "$($p.Value)"; break } } }
                $text = $_.body
                if (-not $text -and $_.fields) { foreach ($p in $_.fields.PSObject.Properties) { if ($p.Name -match "^(Message|Body|fields\.Value)$") { $text = "$($p.Value)"; break } } }
                [pscustomobject]@{ Time = $_.timestamp; Host = $who; Severity = $_.severity; Text = _short $text 90; Note = (_json $_.analysis) }
            } | Format-Table -AutoSize | Out-String -Width (_w) | Write-Host
        }
    }
}

function _showEntry($e, [string]$title) {
    _hdr $title
    $color = if ($e.status -eq "ok") { "Green" } else { "Red" }
    Write-Host ("{0,-16}" -f "status") -NoNewline -ForegroundColor Yellow; Write-Host " $($e.status)" -ForegroundColor $color
    _kv "data source" $e.data_source
    _kv "family" $e.pattern_id
    _kv "reference" $e.reference_used
    if ($e.measurement_used) { _kv "measurement" "$($e.measurement_used.name) [$($e.measurement_used.type)]" }
    _kv "intent" $e.intent
    _kv "query type" $e.query_type
    _kv "time range" (_json $e.time_range)
    _kv "reason" $e.reason
    _kv "clarification" $e.clarification
    Write-Host "query:" -ForegroundColor Yellow
    if ($e.query -is [string]) { Write-Host "  $($e.query)" -ForegroundColor White } else { Write-Host "  $(_json $e.query)" -ForegroundColor White }
    if ($e.analysis) { _kv "analysis req" (_json $e.analysis) }
    _kv "explanation" $e.explanation
    # What the deterministic guard changed or flagged (word forms added, defaults filled, warnings).
    if ($e.guard_notes) {
        foreach ($a in @($e.guard_notes.applied)) { if ($a) { _kv "system applied" $a } }
        foreach ($w in @($e.guard_notes.warnings)) {
            if ($w) { Write-Host ("{0,-16}" -f "system warning") -NoNewline -ForegroundColor Yellow; Write-Host " $w" -ForegroundColor DarkYellow }
        }
    }
    _showExecution $e.execution
}

# ---- raw HTTP: keep the exact response bytes (what a frontend would receive) ----------
function _utf8([byte[]]$b) { return [Text.Encoding]::UTF8.GetString($b) }
function _getRaw([string]$uri) {
    $resp = Invoke-WebRequest -UseBasicParsing -Uri $uri -TimeoutSec 120
    return (_utf8 $resp.RawContentStream.ToArray())
}
function _postRaw([string]$uri, [string]$json, [int]$timeout = 120) {
    $resp = Invoke-WebRequest -UseBasicParsing -Method Post -Uri $uri -ContentType "application/json; charset=utf-8" `
            -Body ([Text.Encoding]::UTF8.GetBytes($json)) -TimeoutSec $timeout
    return (_utf8 $resp.RawContentStream.ToArray())
}
function _errText($err) { $m = $err.ErrorDetails.Message; if (-not $m) { $m = $err.Exception.Message }; return $m }
# Pretty-print JSON text with Python's json.tool (indentation only: keys, order and values unchanged).
function _pretty([string]$text) {
    $tmp = Join-Path $env:TEMP "qa_pretty.json"
    [IO.File]::WriteAllText($tmp, $text, (New-Object Text.UTF8Encoding $false))
    if (Test-Path $script:Py) { & $script:Py -m json.tool --no-ensure-ascii $tmp | Write-Host }
    else { Write-Host $text }
}

function Ask-Agent([Parameter(Mandatory = $true)][string]$Question, [string]$SessionId, [switch]$Raw) {
    $body = @{ question = $Question }
    if ($SessionId) { $body.session_id = $SessionId }
    _hdr "Q: $Question"
    try { $text = _postRaw "$script:AgentUrl/api/v1/query" ($body | ConvertTo-Json -Compress) 300 }
    catch { Write-Host "REQUEST FAILED: $(_errText $_)" -ForegroundColor Red; return }
    # Save the backend's response byte-for-byte, then parse it for the readable view.
    $global:LastAgentRaw = $text
    [IO.File]::WriteAllText($script:RawFile, $text, (New-Object Text.UTF8Encoding $false))
    $r = $text | ConvertFrom-Json
    $global:LastAgent = $r
    if ($Raw) { _pretty $text; return }
    $res = $r.result
    _kv "mode" $res.mode
    if ($res.mode -eq "multi") {
        $i = 0; foreach ($e in $res.results) { $i++; _showEntry $e "Result $i of $(@($res.results).Count)" }
        if ($res.synthesis) { _hdr "Synthesis"; Write-Host (_json $res.synthesis) }
    } else { _showEntry $res "Result" }
    Write-Host "`nRaw backend JSON for this answer: Show-Raw   (saved byte-for-byte to $script:RawFile)" -ForegroundColor DarkGray
}

# The exact JSON the backend sent for the LAST Ask-Agent answer (same answer, no second LLM call).
#   Show-Raw            indented for reading (whitespace only; nothing renamed, dropped or reordered)
#   Show-Raw -AsSent    the bytes exactly as sent (one line)
function Show-Raw([switch]$AsSent) {
    if (-not $global:LastAgentRaw) { Write-Host "Run Ask-Agent first." -ForegroundColor Red; return }
    _hdr "RAW backend -> frontend JSON (POST /api/v1/query response)"
    if ($AsSent) { Write-Host $global:LastAgentRaw } else { _pretty $global:LastAgentRaw }
}

# ---- ground truth ----------------------------------------------------------------
function Prom([Parameter(Mandatory = $true)][string]$Query, [switch]$Range, [int]$Minutes = 30, [int]$Step = 60,
              [string]$Start, [string]$End, [string]$At, [switch]$Raw) {
    _hdr "Prometheus: $Query"
    $q = [uri]::EscapeDataString($Query)
    if ($Range) {
        if ($Start -and $End) { $start = [uri]::EscapeDataString($Start); $end = [uri]::EscapeDataString($End) }
        else { $end = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds(); $start = $end - 60 * $Minutes }
        $url = "$script:PromUrl/api/v1/query_range?query=$q&start=$start&end=$end&step=$Step"
    } else {
        $t = if ($At) { "&time=" + [uri]::EscapeDataString($At) } else { "" }
        $url = "$script:PromUrl/api/v1/query?query=$q$t"
    }
    try { $text = _getRaw $url } catch { Write-Host "FAILED: $(_errText $_)" -ForegroundColor Red; return }
    if ($Raw) { _pretty $text; return }          # Prometheus's own response, untouched
    $r = $text | ConvertFrom-Json
    if ($Range) {
        $r.data.result | ForEach-Object {
            $vals = @($_.values | ForEach-Object { [double]$_[1] })
            $st = $vals | Measure-Object -Minimum -Maximum -Average
            $last = $_.values[-1]
            [pscustomobject]@{ Series = _labels $_.metric; Points = $vals.Count; Min = _num $st.Minimum; Avg = _num $st.Average
                               Max = _num $st.Maximum; Last = _num $last[1]; LastAt = [DateTimeOffset]::FromUnixTimeSeconds([long]$last[0]).ToString("u") }
        } | Sort-Object Series | Format-Table -AutoSize | Out-String -Width (_w) | Write-Host
    } else {
        $r.data.result | ForEach-Object { [pscustomobject]@{ Series = _labels $_.metric; Value = _num $_.value[1] } } |
            Sort-Object Series | Format-Table -AutoSize | Out-String -Width (_w) | Write-Host
    }
    if (-not $r.data.result) { Write-Host "  (no series returned)" -ForegroundColor DarkYellow }
}

function OS([Parameter(Mandatory = $true)][string]$Index, [Parameter(Mandatory = $true)][string]$Body, [switch]$Raw) {
    _hdr "OpenSearch: $Index"
    try { $text = _postRaw "$script:OsUrl/$Index/_search?track_total_hits=true" $Body }
    catch { Write-Host "FAILED: $(_errText $_)" -ForegroundColor Red; return }
    if ($Raw) { _pretty $text; return }          # OpenSearch's own response, untouched
    $r = $text | ConvertFrom-Json
    _kv "total matched" "$($r.hits.total.value) ($($r.hits.total.relation))"
    if ($r.aggregations) { _flattenOsAggs $r.aggregations | Format-Table -AutoSize | Out-String -Width (_w) | Write-Host }
    if ($r.hits.hits.Count -gt 0) {
        $r.hits.hits | ForEach-Object {
            $s = $_._source
            $who = @($s.host, $s.Resource.host.name, $s.fields.Switch) | Where-Object { $_ } | Select-Object -First 1
            $sev = @($s.Severity, $s.fields.PhysicalSubContext) | Where-Object { $_ } | Select-Object -First 1
            $txt = @($s.Message, $s.Body, $s.fields.Value) | Where-Object { $_ } | Select-Object -First 1
            [pscustomobject]@{ Time = $s.'@timestamp'; Host = $who; Severity = $sev; Text = _short "$txt" 90 }
        } | Format-Table -AutoSize | Out-String -Width (_w) | Write-Host
    }
}

# Re-run the agent's OWN last queries directly against the backends, over the window it resolved.
# If these numbers differ from Ask-Agent's output, the agent's execution/normalisation is wrong.
# If they match but disagree with SimEvents / your independent query, the agent built the wrong query.
$script:FamilyIndex = @{ fabric_health = "slingshot_crayfabrichealth-*"; controller_log = "controllerlog-*"; ilo_events = "log_iml-*" }
function Recheck {
    if (-not $global:LastAgent) { Write-Host "Run Ask-Agent first." -ForegroundColor Red; return }
    $res = $global:LastAgent.result
    $entries = if ($res.mode -eq "multi") { @($res.results) } else { @($res) }
    foreach ($e in $entries) {
        if ($e.status -ne "ok" -and $e.status -ne "panic_mode_best_effort") { continue }
        $tr = if ($e.execution) { $e.execution.resolved_time_range } else { $null }
        if ($e.data_source -eq "prometheus") {
            if ($tr -and $tr.start) { Prom $e.query -Range -Start $tr.start -End $tr.end -Step ([int]$tr.step_seconds) }
            elseif ($tr -and $tr.instant) { Prom $e.query -At $tr.instant }
            else { Prom $e.query }
        } elseif ($e.data_source -eq "opensearch") {
            $body = _json $e.query
            if ($body -match "named_condition") { Write-Host "`nSkipping $($e.pattern_id): its query uses a named_condition the agent expands server-side; use the independent query instead." -ForegroundColor DarkYellow; continue }
            if ($e.analysis) { Write-Host "`nNote: $($e.pattern_id) is a '$($e.analysis.type)' analysis; this re-runs only its FIRST query, not the follow-ups." -ForegroundColor DarkYellow }
            OS $script:FamilyIndex[$e.pattern_id] $body
        }
    }
}

function SimEvents([int]$LastMinutes = 120) {
    _hdr "Simulator events (answer key), last $LastMinutes min"
    $cut = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - 60 * $LastMinutes
    $ev = Invoke-RestMethod "$script:SimUrl/events?state=all"     # default is open-only; chaos events end quietly
    if ($ev.PSObject.Properties["events"]) { $ev = $ev.events }
    @($ev) | Where-Object { $_.start_ts -ge $cut -or $_.end_ts -ge $cut } | Sort-Object start_ts | ForEach-Object {
        [pscustomobject]@{ Id = $_.id; Type = $_.type; Node = $_.node; Gpu = $_.gpu; State = $_.state; Source = $_.source
            StartUTC = [DateTimeOffset]::FromUnixTimeMilliseconds([long]($_.start_ts * 1000)).ToString("u")
            EndUTC = [DateTimeOffset]::FromUnixTimeMilliseconds([long]($_.end_ts * 1000)).ToString("u"); Params = _json $_.params }
    } | Format-Table -AutoSize | Out-String -Width (_w) | Write-Host
}

Write-Host "Loaded: Ask-Agent, Recheck, Prom, OS, SimEvents  (agent=$script:AgentUrl prom=$script:PromUrl os=$script:OsUrl sim=$script:SimUrl)" -ForegroundColor DarkGray
