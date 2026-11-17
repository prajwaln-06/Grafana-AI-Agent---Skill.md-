# 📋 AI Observability Agent: Golden Signals Testing & Verification Catalog
### Tailored for the 4-Node GPU Cluster Simulator (`prom-simulator`)

This catalog provides the exact test queries and expected dashboard deflections across Google SRE's **4 Golden Signals** (Latency, Traffic, Errors, and Saturation), verified live against the running simulator stack.

---

## 🏗️ Active Simulator Environment Overview

The tests in this guide run against the **4-Node GPU Cluster Simulator** (`E:\prom-simulator-docker\prom-simulator`) running in Docker:

| Component | Target URL | What It Simulates |
|---|---|---|
| **Prometheus** | `http://localhost:9090` | 4 Server Nodes (`node-00` .. `node-03`) with **20 live simulated GPUs** streaming `node_*` and `DCGM_*` metrics |
| **OpenSearch** | `http://localhost:9600` | Full OpenSearch log cluster streaming `heartbeat`, `consolelog-*`, and `syslog-*` |
| **Incident Controller** | `http://localhost:9500` | Scenario injection engine (thermal runaway, memory leaks, disk filling) |
| **AI Agent Backend** | `http://localhost:8000` | The observability query engine answering questions via PromQL & OpenSearch DSL |
| **Grafana Dashboard** | `http://localhost:3000` | The SRE 4 Golden Signals monitoring dashboard |

> **⏱️ Note on Prometheus Scrapes & Rates:**  
> Prometheus scrapes every **10 seconds**, and Grafana rates are computed over a rolling 5-minute window (`[5m]`). After submitting a test request, wait 10–20 seconds before refreshing Grafana (`http://localhost:3000`).

---

## 🧭 The 4 Golden Signals Quick Reference

| Signal | Target Panel on Grafana | What It Measures | Target SLA |
|---|---|---|:---:|
| **Signal 1: Latency** | `P99 Query Latency`, `P90 Step Duration` | Pipeline response time (LLM generation vs database fetch) | `< 4.0s` |
| **Signal 2: Traffic** | `User Request Throughput (QPS)`, `Token Velocity` | Query demand & LLM token consumption | Real-time |
| **Signal 3: Errors** | `AI Hallucination Rate`, `Blank Chart Rate`, `Alert Proposals` | Contract validity, empty charts & safe refusals | `< 5% / < 3%` |
| **Signal 4: Saturation** | `Active In-Memory Sessions`, `In-Flight Queue`, `RSS RAM` | Concurrency queue & Python memory footprint | `< 1.5 GB` |

---

## ⏱️ Signal 1: Latency (Response Time & Step Breakdown)

* **Goal:** Measure single-query end-to-end response time against the SLA (`< 4.0s`) and inspect the pipeline breakdown between **LLM Generation** and **Prometheus / OpenSearch Fetch**.

### 🎮 Test 1A: Live GPU Temperature Query (DCGM)
```powershell
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/query" `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"question": "What is the current temperature across all GPUs?"}'
```
* **Resolved Metric:** `DCGM_FI_DEV_GPU_TEMP` ([`references/dcgm-exporter/thermal.md`](file:///e:/Grafana-AI/skills/references/dcgm-exporter/thermal.md))
* **Execution Status:** `success` (returns live temperatures across all 20 GPUs, e.g. `58.2°C`, `73.4°C`, `80.8°C`).

### 🖥️ Test 1B: Host CPU Load Average Query (Node Exporter)
```powershell
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/query" `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"question": "What is the 5-minute load average on node-00?"}'
```
* **Resolved Metric:** `node_load5{node_id="node-00"}` ([`references/node-exporter/cpu.md`](file:///e:/Grafana-AI/skills/references/node-exporter/cpu.md))
* **Execution Status:** `success` (returns real load values for `node-00`).


```
* **Resolved Backend:** OpenSearch DSL query hitting `syslog-*` and `consolelog-*` indices on port `9600`.
* **Execution Status:** `success` (returns matching log documents).

* **Expected Dashboard Impact (Signal 1):**
  * **P99 Query Latency:** Displays response time in **GREEN** (typically 1.5s – 3.2s, SLA: `< 4.0s`).
  * **P90 Pipeline Step Duration:** Stacked bar chart shows exact seconds spent in `llm_generation` vs `prometheus_fetch` / `opensearch_fetch`.
  * **AI Hallucination Rate:** **`0%` (GREEN)**
  * **Blank Chart Rate:** **`0%` (GREEN)**

---

## 📈 Signal 2: Traffic (Workload Demand & LLM Token Velocity)

* **Goal:** Generate user traffic volume to monitor **Request Throughput (QPS)** spikes and track **AI Token Burn Velocity** (prompt tokens vs. completion tokens).

### 🚀 Traffic Burst Test (3 Concurrent Requests):
```powershell
# Send 3 rapid requests to generate incoming traffic and drive token velocity
1..3 | ForEach-Object {
  Invoke-RestMethod -Uri "http://localhost:8000/api/v1/query" `
    -Method Post `
    -ContentType "application/json" `
    -Body '{"question": "What is the GPU memory used across node-01?"}'
}
```
* **Resolved Metric:** `DCGM_FI_DEV_FB_USED` across the 8 GPUs of `node-01` ([`references/dcgm-exporter/memory.md`](file:///e:/Grafana-AI/skills/references/dcgm-exporter/memory.md)).
* **Expected Dashboard Impact:**
  * **User Request Throughput (QPS):** Timeseries records an instantaneous surge/spike in QPS.
  * **AI Token Burn Velocity:** Speedometer gauge jumps, displaying tokens consumed per second.
  * **Token Consumption Velocity:** Timeseries plots separate curves for `prompt` tokens vs `completion` tokens.
  * **AI Hallucination Rate:** **`0%` (GREEN)**
  * **Blank Chart Rate:** **`0%` (GREEN)**

---

## 🚨 Signal 3: Errors & Quality (Hallucinations, Blank Charts & Safe Refusals)

This signal monitors whether the AI generates safe, valid queries, detects empty backend data, and safely refuses unmapped metrics.

### 🚨 Signal 3A: AI Hallucination & Contract Guardrail Detection
* **Goal:** Prove the **Deterministic Validator** blocks fabricated metric references *before* any backend database is touched.
```powershell
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/query" `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"question": "What is the ratio of GPU memory used to host system memory used on node-00?"}'
```
* **Why This Triggers Guardrail Detection:**
  * The question spans two separate exporter domains (`dcgm-exporter` and `node-exporter`).
  * In synthesizing a compound measurement, the LLM hallucinates `measurement_used.name = "host system memory used"` instead of using verified metric tokens (`node_memory_MemTotal_bytes`, `node_memory_MemAvailable_bytes`).
  * The deterministic validator ([`app/validator.py`](file:///e:/Grafana-AI/app/validator.py)) verifies every metric against confirmed Metric Directories, detects that `"host system memory used"` is not in any opened directory, and rejects it with `status: "validation_failed"` (`explanation: "... looks fabricated"`).
  * Calls `telemetry.record_validation_result(False)` and increments `adk_query_validation_total{result="invalid"}`.
* **Execution Result:**
  * Returns:
    ```json
    {
      "result": {
        "mode": "single",
        "status": "validation_failed",
        "explanation": "results[1]: measurement_used references 'host system memory used', which is not in any Metric Directory this request opened (...) -- looks fabricated."
      },
      "session_id": null
    }
    ```
* **Expected Dashboard Impact:**
  * **AI Hallucination Rate (SLA: < 5%):** Spikes into **`100%` (RED)**, proving the guardrail caught and intercepted the hallucinated metric before hitting Prometheus.
  * **⏱️ Viewing Tip:** Prometheus scrapes every 10 seconds. Wait **15–20 seconds** after executing the command, then click **Refresh** (or press `r`) in Grafana (`http://localhost:3000`) to see the needle deflect into solid RED.

---

### 🚨 Signal 3B: Blank Chart / Empty Results Detection
* **Goal:** Prove that when a query is 100% syntactically valid and non-hallucinated, executes against Prometheus, but returns 0 data series, the **Blank Chart Rate** detector catches it.
```powershell
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/query" `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"question": "What was the 5-minute load average 3 days ago?"}'
```
* **Why This Triggers Blank Chart Detection on the Simulator:**
  * Resolves to valid metric `node_load5` with `time: "now-3d"` (`status: "ok"` $\rightarrow$ **0% Hallucination**).
  * Because the simulator was started today, Prometheus has 0 data points stored from 3 days ago.
  * Prometheus returns `{"result": []}` (empty series).
  * The executor flags `execution_status: "empty_result"` and increments `adk_query_empty_results_total`.
* **Expected Dashboard Impact:**
  * **AI Hallucination Rate:** Stays at **`0%` (GREEN)** (the query was valid and non-hallucinated).
  * **Blank Chart Rate (SLA: < 3%):** Spikes to **`14.3% – 16.7%` (RED)**, alerting SREs that the dashboard rendered a blank chart with no data points.

---

### 🚨 Signal 3C: Unmapped / Unsupported Metric Safe Refusal
* **Goal:** Prove the AI safely refuses unknown metrics rather than hallucinating fake Prometheus metric names.
```powershell
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/query" `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"question": "What is the GPU fan speed?"}'
```
* **Execution Result:**
  * Returns `status: "unmapped"` indicating no supported metric exists in the catalog.
  * No queries are sent to Prometheus.
* **Expected Dashboard Impact:**
  * **AI Hallucination Rate:** Stays at **`0%` (GREEN)** (no hallucination occurred; the agent safely declined).

---

## 🔋 Signal 4: Saturation (Active Chat Sessions, Queue & RAM)

This signal tracks system resource saturation: in-flight concurrent requests, stateful in-memory chat sessions, and Python RAM consumption.

### 🔋 Signal 4A: Active In-Memory Chat Sessions & Alert Lifecycle (Accept / Decline)
* **Default State:** Standard queries are **stateless single-turn requests** that free memory immediately (`session_id: null`). Sessions remain active only when an alert proposal awaits human confirmation or cancellation.

#### Step 1: Propose an Alert Rule (Creates Active Session)
```powershell
$resp = Invoke-RestMethod -Uri "http://localhost:8000/api/v1/query" `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"question": "Alert me if GPU temperature on node-01 exceeds 85 degrees for 5 minutes"}'

# Store the session ID from the response
$sessionId = $resp.session_id
Write-Host "Active Session ID: $sessionId"
```
* **Expected Dashboard Impact (Step 1):**
  * **Active In-Memory Chat Sessions:** Increments (gauge moves up from `0` to `1`).
  * **Alert Proposal Acceptance Rate:** Proposed counter increments (`telemetry.record_alert_proposal("proposed")`).

---

#### Step 2A: Accept / Confirm Proposal (Creates Rule in Grafana)
```powershell
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/alerts/confirm" `
  -Method Post `
  -ContentType "application/json" `
  -Body (@{ session_id = $sessionId; confirm = $true } | ConvertTo-Json)
```

* **Expected Result:**
  * Returns `{"status": "created", "rule_uid": "...", "deeplink": "..."}`.
  * **Alert Proposal Acceptance Rate:** Increments confirmed counter (`telemetry.record_alert_proposal("confirmed")`), driving the percentage upward.
  * **Active In-Memory Chat Sessions:** Memory is freed and gauge drops back to `0`.

---

#### Step 2B: Decline / Discard Proposal (Cancels Proposal & Frees Memory)
```powershell
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/alerts/confirm" `
  -Method Post `
  -ContentType "application/json" `
  -Body (@{ session_id = $sessionId; confirm = $false } | ConvertTo-Json)
```
* **Expected Result:**
  * Returns `{"status": "discarded"}`.
  * **Alert Proposal Acceptance Rate:** Increments discarded counter (`telemetry.record_alert_proposal("discarded")`).
  * **Active In-Memory Chat Sessions:** Memory is freed and gauge drops back to `0`.

> **💡 Terminal Tip:** If executing in a separate PowerShell session where `$sessionId` is not set, replace `$sessionId` with the literal session string returned in Step 1 (e.g. `"s-16fc0c6f-1a88-4529-85f4-a402e7b0861a"`).

---

### 🔋 Signal 4B: In-Flight Concurrency & Memory RSS
* **Goal:** Monitor concurrency spikes and physical RAM usage of the Python backend.
```powershell
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/query" `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"question": "What is the SM clock speed across all GPUs?"}'
```
* **Resolved Metric:** `DCGM_FI_DEV_SM_CLOCK` across all 20 GPUs ([`references/dcgm-exporter/thermal.md`](file:///e:/Grafana-AI/skills/references/dcgm-exporter/thermal.md)).
* **Expected Dashboard Impact:**
  * **In-Flight Concurrent Queries (Queue):** Records instantaneous request processing concurrency.
  * **Backend Container RSS Memory:** Plots physical RAM bytes used by the Python process (typically **~59 MB**, SLA: `< 1.5 GB`).
