# Terminal run: mentor demo

Five questions: 2 Prometheus, 2 OpenSearch, 1 mixed. Every question has three labelled parts:

| Part | What it shows | Command |
|---|---|---|
| **A. Agent answer (readable)** | The agent's answer as tables | `Ask-Agent "..."` |
| **B. Raw backend → frontend JSON** | The exact JSON the backend sends the frontend for **that same answer** | `Show-Raw` |
| **C. Verification** | Whether the answer is right, checked against Prometheus/OpenSearch directly | `Recheck`, `Prom` / `OS`, `SimEvents` |

All commands are Windows PowerShell. Times printed by the tools are **UTC** (IST = UTC + 5:30).

---

## 0. Before the demo

### 0.1 Check the simulator stack is up

```powershell
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
```

| Service | Host port |
|---|---|
| Prometheus | `localhost:9090` |
| OpenSearch (REST API) | `localhost:9250` |
| OpenSearch Dashboards | `localhost:5601` |
| Simulator controller (Swagger at `/docs`) | `localhost:8010` |
| node-00 … node-03 exporters | `localhost:9200` … `9203` |
| Agent backend | `localhost:8100` (started in 0.3) |

If anything is missing:

```powershell
cd C:\Users\csepr\Downloads\Promethues-Opensearch\prom-simulator\prom-simulator
docker compose -f docker-compose.yml -f docker-compose.opensearch.yml up -d --build
```

### 0.2 Turn off random events (do this at least 1 hour before the demo)

The controller injects a random event about every 10 minutes. Turn that off so the only events in the data are the ones you inject.

Restarting the controller clears its event list, but the OpenSearch data stays. That's why this needs to happen at least an hour early: older random events will have aged out of every question's time window by then.

```powershell
cd C:\Users\csepr\Downloads\Promethues-Opensearch\prom-simulator\prom-simulator
$env:CHAOS_ENABLED="false"
docker compose -f docker-compose.yml -f docker-compose.opensearch.yml up -d sim-controller
```

### 0.3 Terminal 1: start the agent backend

```powershell
cd C:\Users\csepr\Downloads\Promethues-Opensearch\work\Grafana-AI-Agent---Skill.md--feat-opensearch-integration
$env:AGENT_PORT="8100"
& C:\Users\csepr\Downloads\Promethues-Opensearch\.venv\Scripts\python.exe run_server.py
```

Leave it running. Port 8100 is used because 8000 belongs to another stack on this machine.

### 0.4 Terminal 2: load the helpers and check every endpoint

Make this terminal wide: maximise the panel. Narrow terminals wrap the tables.

```powershell
cd C:\Users\csepr\Downloads\Promethues-Opensearch\work\Grafana-AI-Agent---Skill.md--feat-opensearch-integration
Set-ExecutionPolicy -Scope Process Bypass -Force
. .\scripts\qa_helpers.ps1

Invoke-RestMethod http://localhost:8100/readyz                    # agent
Invoke-RestMethod http://localhost:9090/-/ready                   # Prometheus
(Invoke-RestMethod http://localhost:9250/_cluster/health).status  # OpenSearch: green or yellow
```

### 0.5 Inject the test events (about 5 minutes before you start)

These events are the answer key. Every question below is checked against them.

```powershell
$u = 'http://localhost:8010/events'
Invoke-RestMethod -Method Post $u -ContentType application/json -Body '{"type":"cpu_spike","node":"node-00","duration_s":1200}'
Invoke-RestMethod -Method Post $u -ContentType application/json -Body '{"type":"gpu_ecc_error","node":"node-02","gpu":3,"kind":"sbe","count":40,"duration_s":180}'
Invoke-RestMethod -Method Post $u -ContentType application/json -Body '{"type":"fabric_link_down","node":"node-03","duration_s":600}'
```

Wait **3 minutes** (Prometheus scrapes about once a minute). Then show the answer key:

```powershell
SimEvents
```

Use this mapping when reading the answers:

| Prometheus node | iLO host (`log_iml`) | Fabric switch |
|---|---|---|
| node-00 | `leif-login01-bmc` | `x1000c4r1b0` |
| node-01 | `leif-login02-bmc` | `x1000c6r3b0` |
| node-02 | `leif-login03-bmc` | `x1000c4r5b0` |
| node-03 | `fmn01-bmc` | `x1000c2r7b0` |

### How every answer is judged

| Check | Command | Right if |
|---|---|---|
| 1. Did the agent run the query? | read part A | `status ok` and `execution success` |
| 2. Did it report the results faithfully? | `Recheck` | Numbers are **exactly** the same as part A |
| 3. Did it build the right query? | read the `query:` line in part A | Right metric or index, filters, time window and grouping |
| 4. Is it the true answer? | the independent `Prom` / `OS` query, plus `SimEvents` | Same numbers, consistent with the injected events |

If check 2 fails, the agent's execution is broken. If check 2 passes but check 4 fails, the model built the wrong query.

---

## 1. Prometheus question 1: current CPU per node

### A. Agent answer (readable)

```powershell
Ask-Agent "What is the current CPU utilization percentage of each node?"
```

### B. Raw backend → frontend JSON

```powershell
Show-Raw
```

### C. Verification

```powershell
Recheck
Prom '100 - avg by (node_id) (rate(node_cpu_seconds_total{mode="idle"}[1m])) * 100'
```

**Right answer:** node-00 is about **95%** (the CPU spike is active). node-01 to node-03 are about **30–40%**.

`Recheck` must match part A exactly. The independent `Prom` query must match too, as long as the agent's query uses the same `[1m]` window. If the agent used `[5m]`, change `[1m]` to `[5m]` above and compare again.

Optional: Prometheus's own raw response:

```powershell
Prom '100 - avg by (node_id) (rate(node_cpu_seconds_total{mode="idle"}[1m])) * 100' -Raw
```

---

## 2. Prometheus question 2: GPU ECC errors on node-02

### A. Agent answer (readable)

```powershell
Ask-Agent "How many single-bit ECC errors did each GPU on node-02 record in the last 30 minutes?"
```

### B. Raw backend → frontend JSON

```powershell
Show-Raw
```

### C. Verification

```powershell
Recheck
Prom 'max_over_time(DCGM_FI_DEV_ECC_SBE_VOL_TOTAL{node_id="node-02"}[30m]) - min_over_time(DCGM_FI_DEV_ECC_SBE_VOL_TOTAL{node_id="node-02"}[30m])'
SimEvents
```

**Right answer:** GPU **3** = **40**, which is the `count` of the injected `gpu_ecc_error`. If `SimEvents` shows more than one ECC event on node-02 GPU 3 in the last 30 minutes, the answer is 40 times the number of events. GPUs 0, 1 and 2 = **0**.

If the agent used `increase(...)`, a value slightly above 40 (for example 41.x) is still right: `increase()` estimates the edges of the window. It's wrong if GPU 3 shows 0, or if the errors appear on another GPU.

Optional: the raw counter values straight from Prometheus:

```powershell
Prom 'DCGM_FI_DEV_ECC_SBE_VOL_TOTAL{node_id="node-02"}' -Raw
```

---

## 3. OpenSearch question 1: degraded fabric switches

### A. Agent answer (readable)

```powershell
Ask-Agent "Which Slingshot switches reported a DEGRADED health state in the last hour?"
```

### B. Raw backend → frontend JSON

```powershell
Show-Raw
```

### C. Verification

```powershell
Recheck
OS 'slingshot_crayfabrichealth-*' '{"size":0,"query":{"bool":{"filter":[{"range":{"@timestamp":{"gte":"now-1h","lte":"now"}}},{"term":{"fields.PhysicalSubContext.keyword":"DEGRADED"}}]}},"aggs":{"switch":{"terms":{"field":"fields.Switch.keyword","size":50}}}}'
```

**Right answer:** **`x1000c2r7b0`** (node-03's switch, from the injected `fabric_link_down`). It must be the only switch, unless `SimEvents` shows other link-down events in the last hour.

The agent may filter on "not HEALTHY" instead of "= DEGRADED". That's equivalent here, because the simulator only writes those two states. The counts must match the independent query.

Optional: OpenSearch's own raw response:

```powershell
OS 'slingshot_crayfabrichealth-*' '{"size":0,"query":{"bool":{"filter":[{"range":{"@timestamp":{"gte":"now-1h","lte":"now"}}},{"term":{"fields.PhysicalSubContext.keyword":"DEGRADED"}}]}},"aggs":{"switch":{"terms":{"field":"fields.Switch.keyword","size":50}}}}' -Raw
```

---

## 4. OpenSearch question 2: iLO events per host and severity

### A. Agent answer (readable)

```powershell
Ask-Agent "How many iLO event log entries did each host log in the last hour, broken down by severity?"
```

### B. Raw backend → frontend JSON

```powershell
Show-Raw
```

### C. Verification

```powershell
Recheck
OS 'log_iml-*' '{"size":0,"query":{"range":{"@timestamp":{"gte":"now-1h","lte":"now"}}},"aggs":{"host":{"terms":{"field":"host.keyword","size":50},"aggs":{"severity":{"terms":{"field":"Severity.keyword","size":10}}}}}}'
```

**Right answer:**
- All 4 BMCs have `OK` entries (routine iLO logins and logouts).
- **`Warning`** entries appear only on **`leif-login01-bmc`** (node-00 CPU spike) and **`leif-login03-bmc`** (node-02 ECC event).
- `leif-login02-bmc` and `fmn01-bmc` have **no** Warning entries.

For an **exact count match**, use the agent's own window. Part A prints `time window {... "start":"...","end":"..."}`. Put those two values in place of `now-1h` / `now`:

```powershell
OS 'log_iml-*' '{"size":0,"query":{"range":{"@timestamp":{"gte":"PASTE_START","lte":"PASTE_END"}}},"aggs":{"host":{"terms":{"field":"host.keyword","size":50},"aggs":{"severity":{"terms":{"field":"Severity.keyword","size":10}}}}}}'
```

---

## 5. Mixed question: Prometheus and OpenSearch together

### A. Agent answer (readable)

```powershell
Ask-Agent "For node-00, show CPU utilization over the last 30 minutes, and the iLO System Overheating events from its BMC leif-login01-bmc in the same period."
```

### B. Raw backend → frontend JSON

```powershell
Show-Raw
```

### C. Verification

```powershell
Recheck
Prom '100 - avg by (node_id) (rate(node_cpu_seconds_total{mode="idle",node_id="node-00"}[1m])) * 100' -Range -Minutes 30
OS 'log_iml-*' '{"size":10,"sort":[{"@timestamp":"desc"}],"query":{"bool":{"filter":[{"range":{"@timestamp":{"gte":"now-30m","lte":"now"}}},{"term":{"host.keyword":"leif-login01-bmc"}},{"match_phrase":{"Message":"System Overheating"}}]}},"aggs":{"host":{"terms":{"field":"host.keyword","size":20}}}}'
SimEvents
```

**Right answer:** `mode multi` with **2 results**:
1. **Prometheus:** node-00 CPU is about 30–40% before the spike's start time (from `SimEvents`), then about **95%** while it's active. The points and the Max must match the `Prom` output.
2. **OpenSearch:** rows **only** from `leif-login01-bmc` saying "System Overheating (... Location CPU ...)":
   - the first row is at the spike's start time;
   - repeats come about every 30 seconds while the spike is active;
   - a "condition cleared" row comes at its end, if it has ended.
   - The total must match the `OS` output.

Both halves line up in time, which is the point of the mixed question.

### Known limitation (worth showing)

The question names the BMC (`leif-login01-bmc`) on purpose. Ask without it:

```powershell
Ask-Agent "For node-00, show CPU utilization over the last 30 minutes and the iLO System Overheating events logged in the same period."
```

The CPU half is right, but the OpenSearch half comes back **empty**. The agent filters the logs on `host = node-00`, and the iLO logs call that machine `leif-login01-bmc`. The agent doesn't yet have a node ↔ log-host mapping. That's the next improvement: a per-deployment mapping file the agent reads before building log filters.

---

## Appendix: raw output options

| Command | Shows |
|---|---|
| `Show-Raw` | The backend's JSON for the last answer, indented (whitespace only: nothing renamed, dropped or reordered) |
| `Show-Raw -AsSent` | The same JSON exactly as sent: one line, byte for byte |
| `notepad $env:TEMP\last_agent_response.json` | The same bytes, saved to a file |
| `Ask-Agent "..." -Raw` | Asks and prints **only** the raw JSON |
| `Prom '...' -Raw` / `OS '...' '...' -Raw` | Prometheus's or OpenSearch's own response, before the agent touches it |

How a frontend calls the backend:

```powershell
curl.exe -s -X POST http://localhost:8100/api/v1/query -H "Content-Type: application/json" -d "{\"question\": \"What is the current CPU utilization percentage of each node?\"}"
```

This is a **new** request, so the model runs again and the wording or numbers can differ slightly from the last `Ask-Agent`. Use `Show-Raw` when you want the raw JSON of the exact answer you just showed.

What the frontend uses from the raw JSON:
- `execution.series[].points` (`timestamp`, `value`) and `legend_label`: line charts
- `execution.buckets[]` / `sub_buckets`: bar and stacked charts
- `execution.hits[]`: log tables
- `execution.resolved_time_range`: the x-axis range
- `execution.metadata.count`: totals
