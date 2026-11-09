# 📋 AI Agent Golden Signals: Monitoring & Verification Catalog

This guide provides the exact test queries, expected responses, and Grafana dashboard deflections across Google SRE's **4 Golden Signals** (Latency, Traffic, Errors, and Saturation).

---

## 🖥️ System & Hardware Environment Context

| Environment Tier | Target Hardware | Exporters Running | Supported Queries |
|---|---|---|---|
| **Your Local Machine (Dev)** | Windows Host (Intel/AMD CPU + RTX 3050 Laptop GPU) | **Node Exporter (`:9100`)** actively streaming host metrics | **All CPU & Memory queries return live data points.** DCGM queries resolve correctly, but return 0 data series unless DCGM exporter container is started. |
| **GPU Production Cluster** | Linux Server + NVIDIA Data Center / Enterprise GPUs | **Node Exporter (`:9100`)** + **DCGM Exporter (`:9400`)** | **Both CPU and GPU queries return live streaming data points.** |

> **⏱️ Note on Prometheus & Grafana Rolling Windows:**  
> Prometheus scrapes metrics every **15 seconds**, and Grafana rate panels evaluate over a **5-minute rolling window (`[5m]`)**. After executing test requests, wait 15–30 seconds before refreshing your Grafana dashboard (`http://localhost:3000`).

---

## 🧭 The 4 Golden Signals Quick Reference

| Signal | Target Panel on Grafana | What It Measures | Target SLA |
|---|---|---|:---:|
| **Signal 1: Latency** | `P99 Query Latency`, `P90 Step Duration` | Pipeline response time (LLM generation vs backend fetch) | `< 4.0s` |
| **Signal 2: Traffic** | `User Request Throughput (QPS)`, `Token Velocity` | Query demand & LLM token consumption | Real-time |
| **Signal 3: Errors** | `AI Hallucination Rate`, `Blank Chart Rate`, `Alert Proposals` | Contract validity, empty charts & safe refusals | `< 5% / < 3%` |
| **Signal 4: Saturation** | `Active In-Memory Sessions`, `In-Flight Queue`, `RSS RAM` | Concurrency queue & Python memory footprint | `< 1.5 GB` |

---

## ⏱️ Signal 1: Latency (Response Time & Step Breakdown)

* **Goal:** Measure single-query end-to-end response time against the SLA (`< 4.0s`) and verify the pipeline step breakdown between **LLM Generation** and **Prometheus Fetch**.

### 🖥️ Standard CPU System (Verified Live on Your Machine)
```powershell
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/query" `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"question": "What is the 5-minute load average across all nodes?"}'
```
* **Resolved Metric:** `node_load5` ([`references/node-exporter/cpu.md`](file:///e:/Grafana-AI/skills/references/node-exporter/cpu.md))
* **Execution Status:** `success` (returns real live load data from Node Exporter, e.g. `0.13`).
* **Expected Dashboard Impact:**
  * **P99 Query Latency:** Displays response time in **GREEN** (typically 1.5s – 3.2s, SLA: `< 4.0s`).
  * **P90 Pipeline Step Duration:** Stacked bar chart shows exact seconds spent in `llm_generation` vs `prometheus_fetch`.
  * **AI Hallucination Rate:** **`0%` (GREEN)**
  * **Blank Chart Rate:** **`0%` (GREEN)**

---

### 🎮 GPU System (NVIDIA DCGM Exporter) *(Requires DCGM Exporter or Simulator on :9400 for live data)*
```powershell
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/query" `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"question": "What is the current temperature across all GPUs?"}' # (Requires DCGM Exporter or Simulator on :9400 for live data)
```
* **Resolved Metric:** `DCGM_FI_DEV_GPU_TEMP` ([`references/dcgm-exporter/thermal.md`](file:///e:/Grafana-AI/skills/references/dcgm-exporter/thermal.md))
* **Behavior by Environment:**
  * **On GPU Server with `dcgm-exporter`:** Returns real GPU temperature values (e.g. `49°C`), maintaining **`0%` Blank Chart Rate (GREEN)**.
  * **On Local Machine (without DCGM container):** The query resolves correctly (`status: "ok"`), but Prometheus returns 0 series, which will increment the Blank Chart counter.

---

## 📈 Signal 2: Traffic (Workload Demand & LLM Token Velocity)

* **Goal:** Generate user traffic volume to monitor **Request Throughput (QPS)** spikes and track **AI Token Burn Velocity** (prompt tokens vs. completion tokens).

### 🖥️ Standard CPU System (Verified Live on Your Machine)
```powershell
# Send 3 rapid requests to generate incoming traffic and drive token velocity
1..3 | ForEach-Object {
  Invoke-RestMethod -Uri "http://localhost:8000/api/v1/query" `
    -Method Post `
    -ContentType "application/json" `
    -Body '{"question": "What is the 15-minute load average?"}'
}
```
* **Resolved Metric:** `node_load15` ([`references/node-exporter/cpu.md`](file:///e:/Grafana-AI/skills/references/node-exporter/cpu.md))
* **Expected Dashboard Impact:**
  * **User Request Throughput (QPS):** Timeseries records an instantaneous surge/spike in QPS.
  * **AI Token Burn Velocity:** Speedometer gauge jumps, displaying tokens consumed per second.
  * **Token Consumption Velocity:** Timeseries plots separate curves for `prompt` tokens vs `completion` tokens.
  * **AI Hallucination Rate:** **`0%` (GREEN)**
  * **Blank Chart Rate:** **`0%` (GREEN)**

---

### 🎮 GPU System (NVIDIA DCGM Exporter) *(Requires DCGM Exporter or Simulator on :9400 for live data)*
```powershell
# Send 3 rapid requests querying GPU framebuffer memory
1..3 | ForEach-Object {
  Invoke-RestMethod -Uri "http://localhost:8000/api/v1/query" `
    -Method Post `
    -ContentType "application/json" `
    -Body '{"question": "What is the framebuffer memory used across all GPUs?"}' # (Requires DCGM Exporter or Simulator on :9400 for live data)
}
```
* **Resolved Metric:** `DCGM_FI_DEV_FB_USED` ([`references/dcgm-exporter/memory.md`](file:///e:/Grafana-AI/skills/references/dcgm-exporter/memory.md))
* **Behavior by Environment:**
  * **On GPU Server with `dcgm-exporter`:** Plots real GPU VRAM usage curves with **`0%` Blank Chart Rate (GREEN)**.
  * **On Local Machine:** Drives token velocity and QPS identically, but reports empty series due to absent DCGM container.

---

## 🚨 Signal 3: Errors & Quality (Hallucinations, Blank Charts & Safe Refusals)

This signal monitors whether the AI generates safe, valid queries, detects empty backend data, and safely refuses unmapped metrics.

### 🚨 Signal 3A: AI Hallucination & Contract Guardrail Detection
* **Goal:** Prove the **Deterministic Validator** blocks malformed or contradictory AI contracts *before* any backend database is touched.

#### 🖥️ Standard CPU System:
```powershell
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/query" `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"question": "Compare CPU utilization, 1-minute load average, and available memory on node-1 over the last hour, and alert me if CPU exceeds 90% for 5 minutes"}'
```

#### 🎮 GPU System *(Guardrail Test - Blocked by Validator)*:
```powershell
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/query" `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"question": "Compare GPU utilization, GPU temperature, and framebuffer memory used on gpu-0 over the last hour, and alert me if GPU temperature exceeds 80 degrees for 5 minutes"}' # (Requires DCGM Exporter or Simulator on :9400 to test beyond validator)
```

* **Execution Result (Both):**
  * Returns `status: "validation_failed"`.
  * Increments `adk_query_validation_total{result="invalid"}`.
* **Expected Dashboard Impact:**
  * **AI Hallucination Rate (SLA: < 5%):** Deflects into **RED**, proving the guardrail caught and blocked the invalid AI contract.

---

### 🚨 Signal 3B: Blank Chart / Empty Results Detection
* **Goal:** Test the **Blank Chart Rate** detector when a query is 100% syntactically valid and non-hallucinated, executes against Prometheus, but returns 0 data series.

#### 🖥️ Standard CPU System (Querying a Non-Existent Instance):
```powershell
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/query" `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"question": "What is the CPU idle time on node-999?"}'
```
* **Why This Works on ANY CPU System:**
  * Resolves to valid metric `node_cpu_seconds_total{instance="node-999", mode="idle"}` (`status: "ok"` $\rightarrow$ **0% Hallucination**).
  * Host `node-999` does not exist in Prometheus $\rightarrow$ returns empty series `[]`.
  * The executor flags `execution_status: "empty_result"` and increments `adk_query_empty_results_total`.

#### 🎮 GPU System *(Blank Chart Test on Dev / Requires Simulator on :9400 for Real Data)*:
```powershell
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/query" `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"question": "What is the GPU utilization across all GPUs?"}' # (Requires DCGM Exporter or Simulator on :9400 for live data; without it, triggers Blank Chart detection)
```
* **Why This Works on Local Machine:**
  * Resolves to valid metric `DCGM_FI_DEV_GPU_UTIL` (`status: "ok"` $\rightarrow$ **0% Hallucination**).
  * In environments without the DCGM exporter running, Prometheus returns 0 series `[]`.

* **Expected Dashboard Impact (Both):**
  * **AI Hallucination Rate:** Stays at **`0%` (GREEN)** (the query was valid and non-hallucinated).
  * **Blank Chart Rate (SLA: < 3%):** Deflects to **`100%` (RED)**, alerting SREs that the dashboard would render a blank graph with no data points.

---

### 🚨 Signal 3C: Unmapped / Unsupported Metric Safe Refusal
* **Goal:** Prove the AI safely refuses unknown metrics rather than hallucinating fake Prometheus metric names.

#### 🖥️ Standard CPU System:
```powershell
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/query" `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"question": "Show disk read throughput"}'
```

#### 🎮 GPU System *(Safe Refusal - Unmapped Metric)*:
```powershell
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/query" `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"question": "What is the GPU fan speed?"}' # (Declined by AI regardless of exporter/simulator)
```

* **Execution Result (Both):**
  * Returns `status: "unmapped"`: *"I could not find a supported metric in the reference catalog..."*.
  * No queries are sent to Prometheus.
* **Expected Dashboard Impact:**
  * **AI Hallucination Rate:** Stays at **`0%` (GREEN)** (no hallucination occurred; the agent safely declined).

---

## 🔋 Signal 4: Saturation (Active Chat Sessions, Queue & RAM)

This signal tracks system resource saturation: in-flight concurrent requests, stateful in-memory chat sessions, and Python RAM consumption.

### 🔋 Signal 4A: Active In-Memory Chat Sessions
* **Why it is 0 by default:** Standard queries are **stateless single-turn requests** that free memory immediately (`session_id: null`). Sessions only stay active when the AI is waiting for user confirmation or clarification.

#### 🖥️ Standard CPU System (Step 1 - Create Active Session):
```powershell
$resp = Invoke-RestMethod -Uri "http://localhost:8000/api/v1/query" `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"question": "Alert me if CPU utilization exceeds 90% for more than 5 minutes"}'

$resp.session_id   # Prints active session ID: s-xxxx
```

#### 🎮 GPU System (Step 1 - Create Active Session) *(Requires DCGM Exporter or Simulator on :9400 for live execution)*:
```powershell
$resp = Invoke-RestMethod -Uri "http://localhost:8000/api/v1/query" `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"question": "Alert me if GPU temperature exceeds 85 degrees for 5 minutes"}' # (Requires DCGM Exporter or Simulator on :9400 for live data)

$resp.session_id   # Prints active session ID: s-xxxx
```

* **Expected Dashboard Impact (Step 1):**
  * **Active In-Memory Chat Sessions:** Deflects from **`0` to `1`**!
  * **Alert Proposal Acceptance Rate:** Counter increments for proposed alert rules.

#### Step 2 - Discard & Free Session Memory Back to 0:
```powershell
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/alerts/confirm" `
  -Method Post `
  -ContentType "application/json" `
  -Body (@{ session_id = $resp.session_id; confirm = $false } | ConvertTo-Json)
```
* **Expected Dashboard Impact (Step 2):**
  * **Active In-Memory Chat Sessions:** Drops immediately back to **`0`**.

---

### 🔋 Signal 4B: In-Flight Concurrency & Memory RSS
* **Goal:** Monitor concurrency spikes and physical RAM usage of the Python backend.

#### 🖥️ Standard CPU System:
```powershell
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/query" `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"question": "How much memory is available on node-1 right now?"}'
```
* **Resolved Metric:** `node_memory_MemAvailable_bytes` ([`references/node-exporter/memory.md`](file:///e:/Grafana-AI/skills/references/node-exporter/memory.md))

#### 🎮 GPU System *(Requires DCGM Exporter or Simulator on :9400 for live data)*:
```powershell
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/query" `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"question": "What is the SM clock speed across all GPUs?"}' # (Requires DCGM Exporter or Simulator on :9400 for live data)
```
* **Resolved Metric:** `DCGM_FI_DEV_SM_CLOCK` ([`references/dcgm-exporter/thermal.md`](file:///e:/Grafana-AI/skills/references/dcgm-exporter/thermal.md))

* **Expected Dashboard Impact (Both):**
  * **In-Flight Concurrent Queries (Queue):** Records instantaneous request processing concurrency.
  * **Backend Container RSS Memory:** Plots physical RAM bytes used by the Python process (target: `< 1.5 GB`).
