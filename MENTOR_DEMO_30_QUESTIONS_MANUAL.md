# OpenSearch 30-Question Mentor Demonstration Manual (PowerShell)

This manual provides the exact, copy-pastable PowerShell commands for demonstrating all **30 OpenSearch mentor questions** live in the Antigravity IDE terminal:
1. **Through the AI Agent Pipeline** (`POST http://127.0.0.1:8000/api/v1/query` -> Router -> Generator -> Guard -> Canonicalizer -> Validator -> Executor -> OpenSearch).
2. **Expected AI-Generated OpenSearch DSL** for that question.
3. **Direct OpenSearch Database Query** (`POST http://127.0.0.1:9250/<index>/_search`) to independently verify that the AI Agent's numbers, buckets, and rows match OpenSearch directly.

---

## Part 1: One-Time Terminal Setup (Before Starting the Demo)

### Step 1: Verify Docker OpenSearch is Running (`http://127.0.0.1:9250`)
In **Terminal 1**, run:
```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:9250"
```

### Step 2: Start the AI Agent Backend Server (Keep Running in Terminal 1)
In **Terminal 1**, run:
```powershell
cd C:\Users\csepr\Downloads\Promethues-Opensearch
.\.venv\Scripts\python.exe run_server.py
```
Wait until you see:
```text
INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
```
> **Note**: Do not click/select text inside Terminal 1 while it is running, as Windows PowerShell QuickEdit mode pauses the process until you press `Enter`.

### Step 3: Load the Two PowerShell Helper Functions in Terminal 2
Open **Terminal 2** (`+` icon in the terminal panel) and paste this block once:

```powershell
cd C:\Users\csepr\Downloads\Promethues-Opensearch

function Ask-Agent([string]$Question) {
    Write-Host "`n==================================================================" -ForegroundColor Cyan
    Write-Host "AGENT PIPELINE QUESTION: $Question" -ForegroundColor Yellow
    $body = @{ question = $Question } | ConvertTo-Json
    $resp = Invoke-RestMethod -Method POST -Uri "http://127.0.0.1:8000/api/v1/query" -ContentType "application/json" -Body $body -TimeoutSec 120
    $res = $resp.result
    if ($res.mode -eq "multi") {
        Write-Host ("Status: multi | Correlation: {0}" -f ($res.correlation | ConvertTo-Json -Compress)) -ForegroundColor Green
        Write-Host "`n--- 1. AI-Generated Queries ---" -ForegroundColor Magenta
        $res.results | ForEach-Object { @{ index = $_.index; intent = $_.intent; query = $_.query } } | ConvertTo-Json -Depth 12
        Write-Host "`n--- 2. Agent Execution & Correlation Output ---" -ForegroundColor Magenta
        @{ correlation = $res.correlation; results = ($res.results | ForEach-Object { $_.execution }) } | ConvertTo-Json -Depth 10
    } else {
        Write-Host ("Status: {0} | Index: {1} | Intent: {2}" -f $res.status, $res.index, $res.intent) -ForegroundColor Green
        Write-Host "`n--- 1. AI-Generated & Guard-Approved OpenSearch DSL ---" -ForegroundColor Magenta
        $res.query | ConvertTo-Json -Depth 12
        Write-Host "`n--- 2. Agent Execution Output ---" -ForegroundColor Magenta
        if ($res.execution.analysis) {
            @{
                execution_status = $res.execution.execution_status
                total_hits       = $res.execution.metadata.count.value
                analysis         = $res.execution.analysis
                buckets          = $res.execution.buckets
                sample_hits      = ($res.execution.hits | Select-Object -First 5)
            } | ConvertTo-Json -Depth 10
        } else {
            @{
                execution_status = $res.execution.execution_status
                result_type      = $res.execution.result_type
                total_hits       = $res.execution.metadata.count.value
                bucket_count     = $res.execution.bucket_count
                buckets          = $res.execution.buckets
                sample_hits      = ($res.execution.hits | Select-Object -First 5)
            } | ConvertTo-Json -Depth 10
        }
    }
}

function Query-OpenSearch([string]$Index, [string]$DslJson) {
    Write-Host "`n------------------------------------------------------------------" -ForegroundColor DarkCyan
    Write-Host "DIRECT OPENSEARCH -> POST http://127.0.0.1:9250/$Index/_search" -ForegroundColor Yellow
    $raw = Invoke-RestMethod -Method POST -Uri "http://127.0.0.1:9250/$Index/_search" -ContentType "application/json" -Body $DslJson
    [PSCustomObject]@{
        total_hits   = $raw.hits.total.value
        aggregations = $raw.aggregations
        sample_hits  = ($raw.hits.hits | Select-Object -First 5 | ForEach-Object { $_._source })
    } | ConvertTo-Json -Depth 10
}
```

---

## Part 2: All 30 Questions — Pipeline Command, Expected Agent Query & Direct OpenSearch Command

---

### Section 10.1: Fabric Health (`slingshot_crayfabrichealth-*`)

#### 1. [FH-01] Which fabric components reported a non-healthy status in the last 24 h?

**A) Run through AI Agent Pipeline (PowerShell):**
```powershell
Ask-Agent "Which fabric components reported a non-healthy status in the last 24 h?"
```

**Expected Query Generated by Agent:**
```json
{
  "size": 0,
  "track_total_hits": true,
  "timeout": "10s",
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "exists": { "field": "fields.PhysicalSubContext" } }
      ],
      "must_not": [
        { "term": { "fields.PhysicalSubContext.keyword": "HEALTHY" } }
      ]
    }
  },
  "aggs": {
    "by_switch": { "terms": { "field": "fields.Switch.keyword", "size": 50 } }
  }
}
```

**B) Run Direct Query against OpenSearch (PowerShell):**
```powershell
Query-OpenSearch "slingshot_crayfabrichealth-*" '{
  "size": 0,
  "track_total_hits": true,
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "exists": { "field": "fields.PhysicalSubContext" } }
      ],
      "must_not": [
        { "term": { "fields.PhysicalSubContext.keyword": "HEALTHY" } }
      ]
    }
  },
  "aggs": {
    "by_switch": { "terms": { "field": "fields.Switch.keyword", "size": 50 } }
  }
}'
```

---

#### 2. [FH-02] How many CRITICAL events occurred per switch?

**A) Run through AI Agent Pipeline (PowerShell):**
```powershell
Ask-Agent "How many CRITICAL events occurred per switch?"
```

**Expected Query Generated by Agent:**
```json
{
  "size": 0,
  "track_total_hits": true,
  "timeout": "10s",
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "term": { "fields.PhysicalSubContext.keyword": "CRITICAL" } }
      ]
    }
  },
  "aggs": {
    "by_switch": {
      "terms": { "field": "fields.Switch.keyword", "size": 20 },
      "aggs": {
        "by_location": { "terms": { "field": "fields.Location.keyword", "size": 20 } }
      }
    }
  }
}
```

**B) Run Direct Query against OpenSearch (PowerShell):**
```powershell
Query-OpenSearch "slingshot_crayfabrichealth-*" '{
  "size": 0,
  "track_total_hits": true,
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "term": { "fields.PhysicalSubContext.keyword": "CRITICAL" } }
      ]
    }
  },
  "aggs": {
    "by_switch": {
      "terms": { "field": "fields.Switch.keyword", "size": 20 },
      "aggs": {
        "by_location": { "terms": { "field": "fields.Location.keyword", "size": 20 } }
      }
    }
  }
}'
```

---

#### 3. [FH-03] What health states exist besides HEALTHY?

**A) Run through AI Agent Pipeline (PowerShell):**
```powershell
Ask-Agent "What health states exist besides HEALTHY?"
```

**Expected Query Generated by Agent:**
```json
{
  "size": 0,
  "track_total_hits": true,
  "timeout": "10s",
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } }
      ]
    }
  },
  "aggs": {
    "health_states": { "terms": { "field": "fields.PhysicalSubContext.keyword", "size": 20 } }
  }
}
```

**B) Run Direct Query against OpenSearch (PowerShell):**
```powershell
Query-OpenSearch "slingshot_crayfabrichealth-*" '{
  "size": 0,
  "track_total_hits": true,
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } }
      ]
    }
  },
  "aggs": {
    "health_states": { "terms": { "field": "fields.PhysicalSubContext.keyword", "size": 20 } }
  }
}'
```

---

#### 4. [FH-04] Which ports were disabled due to excessive flapping?

**A) Run through AI Agent Pipeline (PowerShell):**
```powershell
Ask-Agent "Which ports were disabled due to excessive flapping?"
```

**Expected Query Generated by Agent:**
```json
{
  "size": 0,
  "track_total_hits": true,
  "timeout": "10s",
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "match_phrase": { "fields.Value": "excessive flapping" } }
      ]
    }
  },
  "aggs": {
    "by_location": { "terms": { "field": "fields.Location.keyword", "size": 20 } }
  }
}
```

**B) Run Direct Query against OpenSearch (PowerShell):**
```powershell
Query-OpenSearch "slingshot_crayfabrichealth-*" '{
  "size": 0,
  "track_total_hits": true,
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "match_phrase": { "fields.Value": "excessive flapping" } }
      ]
    }
  },
  "aggs": {
    "by_location": { "terms": { "field": "fields.Location.keyword", "size": 20 } }
  }
}'
```

---

#### 5. [FH-05] What recommendations were reported for port j104p1?

**A) Run through AI Agent Pipeline (PowerShell):**
```powershell
Ask-Agent "What recommendations were reported for port j104p1?"
```

**Expected Query Generated by Agent:**
*(Dynamically uses `match_phrase` on `fields.Value` when `fields.Port` is absent from the live mapping, or `term` on `fields.Port.keyword` when present)*:
```json
{
  "size": 20,
  "sort": [{ "@timestamp": { "order": "desc" } }],
  "_source": ["@timestamp", "fields.Switch", "fields.Location", "fields.PhysicalContext", "fields.PhysicalSubContext", "fields.Value"],
  "timeout": "10s",
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "match_phrase": { "fields.Value": "j104p1" } }
      ]
    }
  }
}
```

**B) Run Direct Query against OpenSearch (PowerShell):**
```powershell
Query-OpenSearch "slingshot_crayfabrichealth-*" '{
  "size": 20,
  "track_total_hits": true,
  "sort": [{ "@timestamp": { "order": "desc" } }],
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "match_phrase": { "fields.Value": "j104p1" } }
      ]
    }
  }
}'
```

---

#### 6. [FH-06] Which switches reported `linkerrors.linkautorestartexhaustederror`?

**A) Run through AI Agent Pipeline (PowerShell):**
```powershell
Ask-Agent "Which switches reported linkerrors.linkautorestartexhaustederror?"
```

**Expected Query Generated by Agent:**
```json
{
  "size": 0,
  "track_total_hits": true,
  "timeout": "10s",
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "term": { "fields.PhysicalContext.keyword": "linkerrors.linkautorestartexhaustederror" } }
      ]
    }
  },
  "aggs": {
    "by_switch": { "terms": { "field": "fields.Switch.keyword", "size": 20 } }
  }
}
```

**B) Run Direct Query against OpenSearch (PowerShell):**
```powershell
Query-OpenSearch "slingshot_crayfabrichealth-*" '{
  "size": 0,
  "track_total_hits": true,
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "term": { "fields.PhysicalContext.keyword": "linkerrors.linkautorestartexhaustederror" } }
      ]
    }
  },
  "aggs": {
    "by_switch": { "terms": { "field": "fields.Switch.keyword", "size": 20 } }
  }
}'
```

---

#### 7. [FH-07] Which racks/chassis have the most unhealthy events?

**A) Run through AI Agent Pipeline (PowerShell):**
```powershell
Ask-Agent "Which racks/chassis have the most unhealthy events?"
```

**Expected Query Generated by Agent:**
```json
{
  "size": 0,
  "track_total_hits": true,
  "timeout": "10s",
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "exists": { "field": "fields.PhysicalSubContext" } }
      ],
      "must_not": [
        { "term": { "fields.PhysicalSubContext.keyword": "HEALTHY" } }
      ]
    }
  },
  "aggs": {
    "by_rack": {
      "terms": { "field": "fields.Rack.keyword", "size": 20 },
      "aggs": {
        "by_chassis": { "terms": { "field": "fields.Chassis.keyword", "size": 20 } }
      }
    }
  }
}
```

**B) Run Direct Query against OpenSearch (PowerShell):**
```powershell
Query-OpenSearch "slingshot_crayfabrichealth-*" '{
  "size": 0,
  "track_total_hits": true,
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "exists": { "field": "fields.PhysicalSubContext" } }
      ],
      "must_not": [
        { "term": { "fields.PhysicalSubContext.keyword": "HEALTHY" } }
      ]
    }
  },
  "aggs": {
    "by_rack": {
      "terms": { "field": "fields.Rack.keyword", "size": 20 },
      "aggs": {
        "by_chassis": { "terms": { "field": "fields.Chassis.keyword", "size": 20 } }
      }
    }
  }
}'
```

---

#### 8. [FH-08] Show critical events for switch x1000c3r1.

**A) Run through AI Agent Pipeline (PowerShell):**
```powershell
Ask-Agent "Show critical events for switch x1000c3r1."
```

**Expected Query Generated by Agent:**
```json
{
  "size": 20,
  "sort": [{ "@timestamp": { "order": "desc" } }],
  "timeout": "10s",
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "term": { "fields.PhysicalSubContext.keyword": "CRITICAL" } },
        { "prefix": { "fields.Switch.keyword": "x1000c3r1" } }
      ]
    }
  }
}
```

**B) Run Direct Query against OpenSearch (PowerShell):**
```powershell
Query-OpenSearch "slingshot_crayfabrichealth-*" '{
  "size": 20,
  "track_total_hits": true,
  "sort": [{ "@timestamp": { "order": "desc" } }],
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "term": { "fields.PhysicalSubContext.keyword": "CRITICAL" } },
        { "prefix": { "fields.Switch.keyword": "x1000c3r1" } }
      ]
    }
  }
}'
```

---

#### 9. [FH-09] Which ports have recurring faults?

**A) Run through AI Agent Pipeline (PowerShell):**
```powershell
Ask-Agent "Which ports have recurring faults?"
```

**Expected Query Generated by Agent (`recurrence` analysis):**
```json
{
  "size": 0,
  "track_total_hits": true,
  "timeout": "10s",
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "exists": { "field": "fields.PhysicalSubContext" } }
      ],
      "must_not": [
        { "term": { "fields.PhysicalSubContext.keyword": "HEALTHY" } }
      ]
    }
  },
  "aggs": {
    "by_port": {
      "terms": { "field": "fields.Location.keyword", "size": 50 },
      "aggs": {
        "by_fault": { "terms": { "field": "fields.PhysicalContext.keyword", "size": 10 } }
      }
    }
  }
}
```

**B) Run Direct Query against OpenSearch (PowerShell):**
```powershell
Query-OpenSearch "slingshot_crayfabrichealth-*" '{
  "size": 0,
  "track_total_hits": true,
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "exists": { "field": "fields.PhysicalSubContext" } }
      ],
      "must_not": [
        { "term": { "fields.PhysicalSubContext.keyword": "HEALTHY" } }
      ]
    }
  },
  "aggs": {
    "by_port": {
      "terms": { "field": "fields.Location.keyword", "size": 50 },
      "aggs": {
        "by_fault": { "terms": { "field": "fields.PhysicalContext.keyword", "size": 10 } }
      }
    }
  }
}'
```

---

### Section 10.2: Controller Log (`controllerlog-*`)

#### 10. [CL-01] Which controllers reported errors in last 24 h / which hosts have most errors?

**A) Run through AI Agent Pipeline (PowerShell):**
```powershell
Ask-Agent "Which controllers reported errors in last 24 h / which hosts have most errors?"
```

**Expected Query Generated by Agent (`abnormal_severity` expanded by Guard):**
```json
{
  "size": 0,
  "track_total_hits": true,
  "timeout": "10s",
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "terms": { "Severity.keyword": ["alert", "crit", "critical", "emerg", "err", "error"] } }
      ]
    }
  },
  "aggs": {
    "by_host": { "terms": { "field": "Resource.host.name.keyword", "size": 20 } }
  }
}
```

**B) Run Direct Query against OpenSearch (PowerShell):**
```powershell
Query-OpenSearch "controllerlog-*" '{
  "size": 0,
  "track_total_hits": true,
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "terms": { "Severity.keyword": ["alert", "crit", "critical", "emerg", "err", "error"] } }
      ]
    }
  },
  "aggs": {
    "by_host": { "terms": { "field": "Resource.host.name.keyword", "size": 20 } }
  }
}'
```

---

#### 11. [CL-02] Which controllers reported I²C failures, and which bus numbers?

**A) Run through AI Agent Pipeline (PowerShell):**
```powershell
Ask-Agent "Which controllers reported I²C failures, and which bus numbers?"
```

**Expected Query Generated by Agent:**
```json
{
  "size": 20,
  "sort": [{ "@timestamp": { "order": "desc" } }],
  "_source": ["@timestamp", "Resource.host.name", "Resource.service.name", "Severity", "Body"],
  "timeout": "10s",
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "match_phrase": { "Body": "i2c ioctl failed" } }
      ]
    }
  }
}
```

**B) Run Direct Query against OpenSearch (PowerShell):**
```powershell
Query-OpenSearch "controllerlog-*" '{
  "size": 20,
  "track_total_hits": true,
  "sort": [{ "@timestamp": { "order": "desc" } }],
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "match_phrase": { "Body": "i2c ioctl failed" } }
      ]
    }
  }
}'
```

---

#### 12. [CL-03] Are the same errors recurring on a controller?

**A) Run through AI Agent Pipeline (PowerShell):**
```powershell
Ask-Agent "Are the same errors recurring on a controller?"
```

**Expected Query Generated by Agent:**
```json
{
  "size": 0,
  "track_total_hits": true,
  "timeout": "10s",
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "terms": { "Severity.keyword": ["alert", "crit", "critical", "emerg", "err", "error"] } }
      ]
    }
  },
  "aggs": {
    "by_host": {
      "terms": { "field": "Resource.host.name.keyword", "size": 20 },
      "aggs": {
        "by_service": { "terms": { "field": "Resource.service.name.keyword", "size": 10 } }
      }
    }
  }
}
```

**B) Run Direct Query against OpenSearch (PowerShell):**
```powershell
Query-OpenSearch "controllerlog-*" '{
  "size": 0,
  "track_total_hits": true,
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "terms": { "Severity.keyword": ["alert", "crit", "critical", "emerg", "err", "error"] } }
      ]
    }
  },
  "aggs": {
    "by_host": {
      "terms": { "field": "Resource.host.name.keyword", "size": 20 },
      "aggs": {
        "by_service": { "terms": { "field": "Resource.service.name.keyword", "size": 10 } }
      }
    }
  }
}'
```

---

#### 13. [CL-04] Which hosts reported "LinkStatus Down"? / ports down on x1000c2r3b0?

**A) Run through AI Agent Pipeline (PowerShell):**
```powershell
Ask-Agent 'Which hosts reported "LinkStatus Down"? / ports down on x1000c2r3b0?'
```

**Expected Query Generated by Agent:**
```json
{
  "size": 20,
  "sort": [{ "@timestamp": { "order": "desc" } }],
  "_source": ["@timestamp", "Resource.host.name", "Resource.service.name", "Severity", "Body"],
  "timeout": "10s",
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "match_phrase": { "Body": "LinkStatus Down" } },
        { "term": { "Resource.host.name.keyword": "x1000c2r3b0" } }
      ]
    }
  }
}
```

**B) Run Direct Query against OpenSearch (PowerShell):**
```powershell
Query-OpenSearch "controllerlog-*" '{
  "size": 20,
  "track_total_hits": true,
  "sort": [{ "@timestamp": { "order": "desc" } }],
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "match_phrase": { "Body": "LinkStatus Down" } },
        { "term": { "Resource.host.name.keyword": "x1000c2r3b0" } }
      ]
    }
  }
}'
```

---

#### 14. [CL-05] Fabric-manager messages with fewer switches available than expected?

**A) Run through AI Agent Pipeline (PowerShell):**
```powershell
Ask-Agent "Fabric-manager messages with fewer switches available than expected?"
```

**Expected Query Generated by Agent (`text_ratio_shortfall` client-side `n/m` analysis):**
```json
{
  "size": 100,
  "sort": [{ "@timestamp": { "order": "desc" } }],
  "_source": ["@timestamp", "Resource.host.name", "Resource.service.name", "Severity", "Body"],
  "timeout": "10s",
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "terms": { "Resource.service.name.keyword": ["fabric-manager.sh", "fabric-manager"] } },
        { "match_phrase": { "Body": "switches available" } }
      ]
    }
  }
}
```

**B) Run Direct Query against OpenSearch + Client-Side `n < m` Check (PowerShell):**
```powershell
$raw = Invoke-RestMethod -Method POST -Uri "http://127.0.0.1:9250/controllerlog-*/_search" -ContentType "application/json" -Body '{
  "size": 100,
  "track_total_hits": true,
  "sort": [{ "@timestamp": { "order": "desc" } }],
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "terms": { "Resource.service.name.keyword": ["fabric-manager.sh", "fabric-manager"] } },
        { "match_phrase": { "Body": "switches available" } }
      ]
    }
  }
}'
$shortfalls = $raw.hits.hits | Where-Object {
    $_._source.Body -match '(\d+)\s*/\s*(\d+)' -and ([int]$Matches[1] -lt [int]$Matches[2])
}
[PSCustomObject]@{
    total_matched     = $raw.hits.total.value
    rows_scanned      = $raw.hits.hits.Count
    shortfalls_found  = @($shortfalls).Count
    shortfall_samples = ($shortfalls | Select-Object -First 5 | ForEach-Object { $_._source })
} | ConvertTo-Json -Depth 6
```

---

#### 15. [CL-06] Did link-down events occur near fabric availability problems?

**A) Run through AI Agent Pipeline (PowerShell):**
```powershell
Ask-Agent "Did link-down events occur near fabric availability problems?"
```

**Expected Queries Generated by Agent (`mode: "multi"`, `correlation.type: "temporal_proximity"`, `tolerance: "5m"`):**
```json
{
  "mode": "multi",
  "correlation": { "type": "temporal_proximity", "left": 0, "right": 1, "tolerance": "5m" },
  "results": [
    {
      "pattern_id": "controller_log",
      "intent": "rows",
      "query": {
        "size": 50,
        "sort": [{ "@timestamp": { "order": "desc" } }],
        "query": {
          "bool": {
            "filter": [
              { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
              { "match_phrase": { "Body": "LinkStatus Down" } }
            ]
          }
        }
      }
    },
    {
      "pattern_id": "fabric_health",
      "intent": "rows",
      "query": {
        "size": 50,
        "sort": [{ "@timestamp": { "order": "desc" } }],
        "query": {
          "bool": {
            "filter": [
              { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
              { "exists": { "field": "fields.PhysicalSubContext" } }
            ],
            "must_not": [
              { "term": { "fields.PhysicalSubContext.keyword": "HEALTHY" } }
            ]
          }
        }
      }
    }
  ]
}
```

**B) Run Both Direct Queries against OpenSearch (PowerShell):**
```powershell
# Query 1: Controller LinkStatus Down events
Query-OpenSearch "controllerlog-*" '{
  "size": 50,
  "track_total_hits": true,
  "sort": [{ "@timestamp": { "order": "desc" } }],
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "match_phrase": { "Body": "LinkStatus Down" } }
      ]
    }
  }
}'

# Query 2: Non-healthy Fabric Health events
Query-OpenSearch "slingshot_crayfabrichealth-*" '{
  "size": 50,
  "track_total_hits": true,
  "sort": [{ "@timestamp": { "order": "desc" } }],
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "exists": { "field": "fields.PhysicalSubContext" } }
      ],
      "must_not": [
        { "term": { "fields.PhysicalSubContext.keyword": "HEALTHY" } }
      ]
    }
  }
}'
```

---

#### 16. [CL-07] Which services generate the most error logs?

**A) Run through AI Agent Pipeline (PowerShell):**
```powershell
Ask-Agent "Which services generate the most error logs?"
```

**Expected Query Generated by Agent:**
```json
{
  "size": 0,
  "track_total_hits": true,
  "timeout": "10s",
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "terms": { "Severity.keyword": ["alert", "crit", "critical", "emerg", "err", "error"] } }
      ]
    }
  },
  "aggs": {
    "by_service": { "terms": { "field": "Resource.service.name.keyword", "size": 20 } }
  }
}
```

**B) Run Direct Query against OpenSearch (PowerShell):**
```powershell
Query-OpenSearch "controllerlog-*" '{
  "size": 0,
  "track_total_hits": true,
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "terms": { "Severity.keyword": ["alert", "crit", "critical", "emerg", "err", "error"] } }
      ]
    }
  },
  "aggs": {
    "by_service": { "terms": { "field": "Resource.service.name.keyword", "size": 20 } }
  }
}'
```

---

#### 17. [CL-08] Show all hmpad errors for x1000c6s7b1.

**A) Run through AI Agent Pipeline (PowerShell):**
```powershell
Ask-Agent "Show all hmpad errors for x1000c6s7b1."
```

**Expected Query Generated by Agent:**
```json
{
  "size": 20,
  "sort": [{ "@timestamp": { "order": "desc" } }],
  "_source": ["@timestamp", "Resource.host.name", "Resource.service.name", "Severity", "Body"],
  "timeout": "10s",
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "term": { "Resource.service.name.keyword": "hmpad" } },
        { "term": { "Resource.host.name.keyword": "x1000c6s7b1" } },
        { "terms": { "Severity.keyword": ["alert", "crit", "critical", "emerg", "err", "error"] } }
      ]
    }
  }
}
```

**B) Run Direct Query against OpenSearch (PowerShell):**
```powershell
Query-OpenSearch "controllerlog-*" '{
  "size": 20,
  "track_total_hits": true,
  "sort": [{ "@timestamp": { "order": "desc" } }],
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "term": { "Resource.service.name.keyword": "hmpad" } },
        { "term": { "Resource.host.name.keyword": "x1000c6s7b1" } },
        { "terms": { "Severity.keyword": ["alert", "crit", "critical", "emerg", "err", "error"] } }
      ]
    }
  }
}'
```

---

#### 18. [CL-09] What did hmscd report before and after a link-down event?

**A) Run through AI Agent Pipeline (PowerShell):**
```powershell
Ask-Agent "What did hmscd report before and after a link-down event?"
```

**Expected Query Generated by Agent (`context_window` two-stage analysis):**
```json
{
  "size": 1,
  "sort": [{ "@timestamp": { "order": "desc" } }],
  "timeout": "10s",
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "match_phrase": { "Body": "LinkStatus Down" } }
      ]
    }
  }
}
```

**B) Run Direct Anchor Query against OpenSearch (PowerShell):**
```powershell
Query-OpenSearch "controllerlog-*" '{
  "size": 3,
  "track_total_hits": true,
  "sort": [{ "@timestamp": { "order": "desc" } }],
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "match_phrase": { "Body": "LinkStatus Down" } }
      ]
    }
  }
}'
```

---

#### 19. [CL-10] Show controller logs excluding routine CRON.

**A) Run through AI Agent Pipeline (PowerShell):**
```powershell
Ask-Agent "Show controller logs excluding routine CRON."
```

**Expected Query Generated by Agent:**
```json
{
  "size": 20,
  "sort": [{ "@timestamp": { "order": "desc" } }],
  "_source": ["@timestamp", "Resource.host.name", "Resource.service.name", "Severity", "Body"],
  "timeout": "10s",
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } }
      ],
      "must_not": [
        { "term": { "Resource.service.name.keyword": "CRON" } }
      ]
    }
  }
}
```

**B) Run Direct Query against OpenSearch (PowerShell):**
```powershell
Query-OpenSearch "controllerlog-*" '{
  "size": 20,
  "track_total_hits": true,
  "sort": [{ "@timestamp": { "order": "desc" } }],
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } }
      ],
      "must_not": [
        { "term": { "Resource.service.name.keyword": "CRON" } }
      ]
    }
  }
}'
```

---

### Section 10.3: iLO Event Log (`log_iml-*`)

#### 20. [ILO-01] Who logged into leif-login03-bmc in last 24 h?

**A) Run through AI Agent Pipeline (PowerShell):**
```powershell
Ask-Agent "Who logged into leif-login03-bmc in last 24 h?"
```

**Expected Query Generated by Agent:**
```json
{
  "size": 20,
  "sort": [{ "@timestamp": { "order": "desc" } }],
  "_source": ["@timestamp", "Created", "host", "Severity", "Oem.Hpe.Severity", "Message", "Oem.Hpe.Code", "Oem.Hpe.RecommendedAction"],
  "timeout": "10s",
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "term": { "host.keyword": "leif-login03-bmc" } },
        { "match_phrase": { "Message": "login" } }
      ]
    }
  }
}
```

**B) Run Direct Query against OpenSearch (PowerShell):**
```powershell
Query-OpenSearch "log_iml-*" '{
  "size": 20,
  "track_total_hits": true,
  "sort": [{ "@timestamp": { "order": "desc" } }],
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "term": { "host.keyword": "leif-login03-bmc" } },
        { "match_phrase": { "Message": "login" } }
      ]
    }
  }
}'
```

---

#### 21. [ILO-02] Which BMCs were accessed from 172.24.0.1?

**A) Run through AI Agent Pipeline (PowerShell):**
```powershell
Ask-Agent "Which BMCs were accessed from 172.24.0.1?"
```

**Expected Query Generated by Agent:**
```json
{
  "size": 0,
  "track_total_hits": true,
  "timeout": "10s",
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "match_phrase": { "Message": "172.24.0.1" } }
      ]
    }
  },
  "aggs": {
    "by_bmc": { "terms": { "field": "host.keyword", "size": 20 } }
  }
}
```

**B) Run Direct Query against OpenSearch (PowerShell):**
```powershell
Query-OpenSearch "log_iml-*" '{
  "size": 0,
  "track_total_hits": true,
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "match_phrase": { "Message": "172.24.0.1" } }
      ]
    }
  },
  "aggs": {
    "by_bmc": { "terms": { "field": "host.keyword", "size": 20 } }
  }
}'
```

---

#### 22. [ILO-03] Show all root login/logout events

**A) Run through AI Agent Pipeline (PowerShell):**
```powershell
Ask-Agent "Show all root login/logout events"
```

**Expected Query Generated by Agent:**
```json
{
  "size": 20,
  "sort": [{ "@timestamp": { "order": "desc" } }],
  "_source": ["@timestamp", "Created", "host", "Severity", "Oem.Hpe.Severity", "Message", "Oem.Hpe.Code", "Oem.Hpe.RecommendedAction"],
  "timeout": "10s",
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "terms": { "Oem.Hpe.Code": [1017, 1018] } },
        { "match_phrase": { "Message": "root" } }
      ]
    }
  }
}
```

**B) Run Direct Query against OpenSearch (PowerShell):**
```powershell
Query-OpenSearch "log_iml-*" '{
  "size": 20,
  "track_total_hits": true,
  "sort": [{ "@timestamp": { "order": "desc" } }],
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "terms": { "Oem.Hpe.Code": [1017, 1018] } },
        { "match_phrase": { "Message": "root" } }
      ]
    }
  }
}'
```

---

#### 23. [ILO-04] Browser vs IPMI/RMCP login counts

**A) Run through AI Agent Pipeline (PowerShell):**
```powershell
Ask-Agent "Browser vs IPMI/RMCP login counts"
```

**Expected Query Generated by Agent:**
```json
{
  "size": 0,
  "track_total_hits": true,
  "timeout": "10s",
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } }
      ]
    }
  },
  "aggs": {
    "login_channels": {
      "filters": {
        "filters": {
          "browser": { "match_phrase": { "Message": "Browser login" } },
          "ipmi_rmcp": {
            "bool": {
              "should": [
                { "match_phrase": { "Message": "IPMI" } },
                { "match_phrase": { "Message": "RMCP" } }
              ],
              "minimum_should_match": 1
            }
          }
        }
      }
    }
  }
}
```

**B) Run Direct Query against OpenSearch (PowerShell):**
```powershell
Query-OpenSearch "log_iml-*" '{
  "size": 0,
  "track_total_hits": true,
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } }
      ]
    }
  },
  "aggs": {
    "login_channels": {
      "filters": {
        "filters": {
          "browser": { "match_phrase": { "Message": "Browser login" } },
          "ipmi_rmcp": {
            "bool": {
              "should": [
                { "match_phrase": { "Message": "IPMI" } },
                { "match_phrase": { "Message": "RMCP" } }
              ],
              "minimum_should_match": 1
            }
          }
        }
      }
    }
  }
}'
```

---

#### 24. [ILO-05] Which BMCs have unusually frequent login/logout?

**A) Run through AI Agent Pipeline (PowerShell):**
```powershell
Ask-Agent "Which BMCs have unusually frequent login/logout?"
```

**Expected Query Generated by Agent:**
```json
{
  "size": 0,
  "track_total_hits": true,
  "timeout": "10s",
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "terms": { "Oem.Hpe.Code": [1017, 1018] } }
      ]
    }
  },
  "aggs": {
    "by_bmc": {
      "terms": { "field": "host.keyword", "size": 20 },
      "aggs": {
        "by_code": { "terms": { "field": "Oem.Hpe.Code", "size": 5 } }
      }
    }
  }
}
```

**B) Run Direct Query against OpenSearch (PowerShell):**
```powershell
Query-OpenSearch "log_iml-*" '{
  "size": 0,
  "track_total_hits": true,
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "terms": { "Oem.Hpe.Code": [1017, 1018] } }
      ]
    }
  },
  "aggs": {
    "by_bmc": {
      "terms": { "field": "host.keyword", "size": 20 },
      "aggs": {
        "by_code": { "terms": { "field": "Oem.Hpe.Code", "size": 5 } }
      }
    }
  }
}'
```

---

#### 25. [ILO-06] Any failed login attempts?

**A) Run through AI Agent Pipeline (PowerShell):**
```powershell
Ask-Agent "Any failed login attempts?"
```

**Expected Query Generated by Agent:**
```json
{
  "size": 20,
  "sort": [{ "@timestamp": { "order": "desc" } }],
  "_source": ["@timestamp", "Created", "host", "Severity", "Oem.Hpe.Severity", "Message", "Oem.Hpe.Code", "Oem.Hpe.RecommendedAction"],
  "timeout": "10s",
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        {
          "bool": {
            "should": [
              { "match_phrase": { "Message": "login failure" } },
              { "match_phrase": { "Message": "Failed login" } }
            ],
            "minimum_should_match": 1
          }
        }
      ]
    }
  }
}
```

**B) Run Direct Query against OpenSearch (PowerShell):**
```powershell
Query-OpenSearch "log_iml-*" '{
  "size": 20,
  "track_total_hits": true,
  "sort": [{ "@timestamp": { "order": "desc" } }],
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        {
          "bool": {
            "should": [
              { "match_phrase": { "Message": "login failure" } },
              { "match_phrase": { "Message": "Failed login" } }
            ],
            "minimum_should_match": 1
          }
        }
      ]
    }
  }
}'
```

---

#### 26. [ILO-07] Security & administration events for a BMC

**A) Run through AI Agent Pipeline (PowerShell):**
```powershell
Ask-Agent "Security & administration events for a BMC"
```

**Expected Query Generated by Agent:**
```json
{
  "size": 20,
  "sort": [{ "@timestamp": { "order": "desc" } }],
  "_source": ["@timestamp", "Created", "host", "Severity", "Oem.Hpe.Severity", "Message", "Oem.Hpe.Code", "Oem.Hpe.RecommendedAction"],
  "timeout": "10s",
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "terms": { "Oem.Hpe.Categories.keyword": ["Security", "Administration"] } }
      ]
    }
  }
}
```

**B) Run Direct Query against OpenSearch (PowerShell):**
```powershell
Query-OpenSearch "log_iml-*" '{
  "size": 20,
  "track_total_hits": true,
  "sort": [{ "@timestamp": { "order": "desc" } }],
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "terms": { "Oem.Hpe.Categories.keyword": ["Security", "Administration"] } }
      ]
    }
  }
}'
```

---

#### 27. [ILO-08] Severity other than OK or Informational

**A) Run through AI Agent Pipeline (PowerShell):**
```powershell
Ask-Agent "Severity other than OK or Informational"
```

**Expected Query Generated by Agent (`abnormal_severity` expanded across both `Severity` and `Oem.Hpe.Severity`):**
```json
{
  "size": 0,
  "track_total_hits": true,
  "timeout": "10s",
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        {
          "bool": {
            "should": [
              { "terms": { "Severity.keyword": ["Critical", "Warning"] } },
              { "terms": { "Oem.Hpe.Severity.keyword": ["Caution", "Critical", "Warning"] } }
            ],
            "minimum_should_match": 1
          }
        }
      ]
    }
  },
  "aggs": {
    "by_bmc": {
      "terms": { "field": "host.keyword", "size": 20 },
      "aggs": {
        "by_severity": { "terms": { "field": "Oem.Hpe.Severity.keyword", "size": 10 } }
      }
    }
  }
}
```

**B) Run Direct Query against OpenSearch (PowerShell):**
```powershell
Query-OpenSearch "log_iml-*" '{
  "size": 0,
  "track_total_hits": true,
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        {
          "bool": {
            "should": [
              { "terms": { "Severity.keyword": ["Critical", "Warning"] } },
              { "terms": { "Oem.Hpe.Severity.keyword": ["Caution", "Critical", "Warning"] } }
            ],
            "minimum_should_match": 1
          }
        }
      ]
    }
  },
  "aggs": {
    "by_bmc": {
      "terms": { "field": "host.keyword", "size": 20 },
      "aggs": {
        "by_severity": { "terms": { "field": "Oem.Hpe.Severity.keyword", "size": 10 } }
      }
    }
  }
}'
```

---

#### 28. [ILO-09] Warning/critical events marked unrepaired + recommended actions

**A) Run through AI Agent Pipeline (PowerShell):**
```powershell
Ask-Agent "Warning/critical events marked unrepaired + recommended actions"
```

**Expected Query Generated by Agent:**
```json
{
  "size": 20,
  "sort": [{ "@timestamp": { "order": "desc" } }],
  "_source": ["@timestamp", "Created", "host", "Severity", "Oem.Hpe.Severity", "Message", "Oem.Hpe.Code", "Oem.Hpe.RecommendedAction"],
  "timeout": "10s",
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        {
          "bool": {
            "should": [
              { "terms": { "Severity.keyword": ["Critical", "Warning"] } },
              { "terms": { "Oem.Hpe.Severity.keyword": ["Caution", "Critical", "Warning"] } }
            ],
            "minimum_should_match": 1
          }
        },
        { "term": { "Oem.Hpe.Repaired": false } }
      ]
    }
  }
}
```

**B) Run Direct Query against OpenSearch (PowerShell):**
```powershell
Query-OpenSearch "log_iml-*" '{
  "size": 20,
  "track_total_hits": true,
  "sort": [{ "@timestamp": { "order": "desc" } }],
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        {
          "bool": {
            "should": [
              { "terms": { "Severity.keyword": ["Critical", "Warning"] } },
              { "terms": { "Oem.Hpe.Severity.keyword": ["Caution", "Critical", "Warning"] } }
            ],
            "minimum_should_match": 1
          }
        },
        { "term": { "Oem.Hpe.Repaired": false } }
      ]
    }
  }
}'
```

---

#### 29. [ILO-10] Event codes recurring most per host

**A) Run through AI Agent Pipeline (PowerShell):**
```powershell
Ask-Agent "Event codes recurring most per host"
```

**Expected Query Generated by Agent:**
```json
{
  "size": 0,
  "track_total_hits": true,
  "timeout": "10s",
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } }
      ]
    }
  },
  "aggs": {
    "by_host": {
      "terms": { "field": "host.keyword", "size": 20 },
      "aggs": {
        "by_code": { "terms": { "field": "Oem.Hpe.Code", "size": 10 } }
      }
    }
  }
}
```

**B) Run Direct Query against OpenSearch (PowerShell):**
```powershell
Query-OpenSearch "log_iml-*" '{
  "size": 0,
  "track_total_hits": true,
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } }
      ]
    }
  },
  "aggs": {
    "by_host": {
      "terms": { "field": "host.keyword", "size": 20 },
      "aggs": {
        "by_code": { "terms": { "field": "Oem.Hpe.Code", "size": 10 } }
      }
    }
  }
}'
```

---

#### 30. [ILO-11] How long after creation were events ingested?

**A) Run through AI Agent Pipeline (PowerShell):**
```powershell
Ask-Agent "How long after creation were events ingested?"
```

**Expected Query Generated by Agent (`timestamp_delta` client-side `@timestamp - Created` analysis without Painless scripts):**
```json
{
  "size": 100,
  "sort": [{ "@timestamp": { "order": "desc" } }],
  "_source": ["@timestamp", "Created", "host", "Severity", "Oem.Hpe.Severity", "Message", "Oem.Hpe.Code", "Oem.Hpe.RecommendedAction"],
  "timeout": "10s",
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } }
      ]
    }
  }
}
```

**B) Run Direct Query against OpenSearch + Client-Side `@timestamp - Created` Lag Calculation (PowerShell):**
```powershell
$raw = Invoke-RestMethod -Method POST -Uri "http://127.0.0.1:9250/log_iml-*/_search" -ContentType "application/json" -Body '{
  "size": 100,
  "track_total_hits": true,
  "sort": [{ "@timestamp": { "order": "desc" } }],
  "_source": ["@timestamp", "Created", "host", "Message"],
  "query": {
    "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } }
      ]
    }
  }
}'
$deltas = $raw.hits.hits | ForEach-Object {
    $ingested = [datetimeoffset]::Parse($_._source.'@timestamp')
    $created  = [datetimeoffset]::Parse($_._source.Created)
    ($ingested - $created).TotalSeconds
} | Sort-Object
[PSCustomObject]@{
    sampled_rows   = $deltas.Count
    min_seconds    = $deltas[0]
    median_seconds = $deltas[[int]($deltas.Count / 2)]
    max_seconds    = $deltas[-1]
} | ConvertTo-Json
```
