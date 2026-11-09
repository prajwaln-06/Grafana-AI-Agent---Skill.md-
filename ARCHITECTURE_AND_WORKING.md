# 📘 Complete Architecture & System Workflow Guide

This document provides a comprehensive, general, and chronologically ordered explanation of the entire system: how raw metrics are gathered from any computer, how Prometheus fetches and stores them, how the AI Agent processes questions, and how Grafana visualizes the system's health.

---

## 📌 Table of Contents
1. [Architecture & Component Overview](#1-architecture--component-overview)
2. [Section 1: How Raw Data is Collected from Any Computer (The Exporter Concept)](#section-1-how-raw-data-is-collected-from-any-computer)
3. [Section 2: Who Fetches the Data and When Prometheus Starts](#section-2-who-fetches-the-data-and-when-prometheus-starts)
4. [Section 3: How Docker Talks to the Windows Host (`host.docker.internal`)](#section-3-how-docker-talks-to-the-windows-host)
5. [Section 4: How the AI Agent Processes a User Question (On-Demand)](#section-4-how-the-ai-agent-processes-a-user-question)
6. [Section 5: How Self-Monitoring (4 Golden Signals) Works](#section-5-how-self-monitoring-4-golden-signals-works)
7. [Section 6: Terminal Command Walkthrough (Verification)](#section-6-terminal-command-walkthrough)
8. [Section 7: Master Step-to-File Reference Matrix](#section-7-master-step-to-file-reference-matrix)

---

## 1. Architecture & Component Overview

```
┌────────────────────────────────────────────────────────────────────────┐
│ 1. MONITORED INFRASTRUCTURE (Any Server or Computer in the Network)   │
│    • Operating System Kernel tracks CPU, RAM, Disks, and Network       │
│    • Exporter agent (e.g. node_exporter :9100) formats and serves text │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    │ HTTP Scrape (every 15s)
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 2. PROMETHEUS DATABASE (Port 9090)                                     │
│    • Central storage: pulls metrics from all machines every 15 seconds │
│    • Stores historical time-series data on disk (TSDB)                 │
└───────────────────────────────────▲────────────────────────────────────┘
                                    │
                                    │ PromQL Query API (GET /api/v1/query_range)
                                    │
┌───────────────────────────────────┴────────────────────────────────────┐
│ 3. AI AGENT BACKEND (FastAPI on Windows Port 8000)                     │
│    • User sends: "What is the performance in node-1?"                  │
│    • Router searches Metric Catalogs (skills/references/node-exporter/)│
│    • Gemini LLM generates PromQL: node_load1{instance="node-1"}        │
│    • Deterministic Validator checks syntax and safety (Zero AI)        │
│    • Executor fetches live data from Prometheus (:9090)                │
│    • Returns chart-ready JSON to user                                  │
│    • Telemetry layer records Latency, Tokens, Errors, Saturation       │
│    • Exposes internal metrics at: GET http://localhost:8000/metrics    │
└───────────────────────────────────▲────────────────────────────────────┘
                                    │
                                    │ Docker Network Bridge:
                                    │ host.docker.internal:8000/metrics
                                    │
┌───────────────────────────────────┴────────────────────────────────────┐
│ 4. DOCKER MONITORING STACK                                             │
│    • Prometheus Scraper: Pulls Windows metrics every 15 seconds        │
│    • 17 SRE Alert Rules: Evaluates SLA violations                      │
│    • Grafana (:3000): Renders 4 Golden Signals Dashboard               │
└────────────────────────────────────────────────────────────────────────┘
```

---

## Section 1: How Raw Data is Collected from Any Computer

In modern infrastructure monitoring, **metrics are not limited to a single server**. Any computer (Linux servers, Windows workstations, cloud VMs, database instances) generates and exposes raw hardware metrics in the same fundamental way:

### 1. The Hardware & Operating System Level
Every computer's operating system tracks its own physical hardware state in real time:
- **On Linux computers:**
  - The Linux kernel records physical hardware events in memory inside the `/proc` filesystem:
    - `/proc/stat`: Records CPU clock cycles spent across states (`user`, `system`, `idle`, `iowait`).
    - `/proc/meminfo`: Records physical RAM byte allocations, buffers, cache, and swap space.
    - `/proc/loadavg`: Records system load averages over 1, 5, and 15-minute windows.
    - `/proc/diskstats`: Records disk read/write throughput and queue wait times.
- **On Windows computers:**
  - The Windows OS tracks performance via Windows Performance Counters (PerfMon / WMI), recording processor times, available megabytes of RAM, and disk queue lengths.
- **On NVIDIA GPUs:**
  - The NVIDIA NVML driver tracks GPU core clock speeds, temperature (Celsius), power draw (Watts), and memory copy utilization.

### 2. Who Collects It from the Operating System?
👉 **An "Exporter" agent running directly on that computer.**

Because OS kernel files are internal to the computer, they cannot be queried directly over a network. Therefore, a small lightweight agent called an **Exporter** is installed on each machine:

| Computer Type | Exporter Installed | Port | What it Measures |
|---|---|:---:|---|
| **Any Linux Server** | `node_exporter` | `9100` | Linux CPU, RAM, Load average, Disks, Network |
| **Any Windows Server** | `windows_exporter` | `9182` | Windows CPU %, Free memory bytes, Disk queue |
| **Any GPU Machine** | `dcgm-exporter` | `9400` | NVIDIA GPU temperature, power, Tensor Core usage |
| **Our AI Agent (Python)**| `prometheus-client` | `8000` | Query latency, LLM tokens, validation errors, sessions |

### 3. When Does Raw Data Collection Start on the Computer?
👉 **The exact second that computer powers on and boots up.**

1. The computer turns on ➔ The OS boots up.
2. The Exporter agent (e.g. `node_exporter`) is set up as an automatic background service (`systemd` service on Linux, or Windows Service on Windows).
3. The Exporter immediately starts reading the OS counters and formats them into standard Prometheus text format:
   ```text
   # HELP node_cpu_seconds_total Seconds the CPUs spent in each mode.
   # TYPE node_cpu_seconds_total counter
   node_cpu_seconds_total{cpu="0",mode="idle"} 492810.12

   # HELP node_memory_MemAvailable_bytes Memory available for new processes.
   # TYPE node_memory_MemAvailable_bytes gauge
   node_memory_MemAvailable_bytes 16428912640

   # HELP node_load1 1m load average.
   # TYPE node_load1 gauge
   node_load1 1.45
   ```
4. It hosts these numbers over HTTP at: `http://<computer-ip>:<port>/metrics(eg:http://localhost:9182/metrics)`.
5. This collection runs continuously 24/7 on the computer, regardless of whether anyone is querying it.

---

## Section 2: Who Fetches the Data and When Prometheus Starts

### 1. Who Fetches the Data?
👉 **Prometheus.**

The monitored computers do **not** push their data. Instead, **Prometheus acts as the central collector that reaches out and fetches (scrapes) the data over HTTP.**

In [`prometheus/prometheus.yml`](prometheus/prometheus.yml), Prometheus is given the network addresses of all computers to monitor:
```yaml
scrape_configs:
  # Job 1: Collect hardware metrics from all servers in the cluster
  - job_name: "cluster-nodes"
    scrape_interval: 15s
    static_configs:
      - targets:
          - "server-1:9100"
          - "server-2:9100"
          - "database-host:9100"

  # Job 2: Collect telemetry from our AI Agent backend
  - job_name: "ai-agent-backend"
    scrape_interval: 15s
    static_configs:
      - targets: ["host.docker.internal:8000"]
```

---

### 2. When Does Prometheus Start Fetching?
👉 **The exact moment Prometheus is started.**

When you run:
```powershell
docker compose -f docker-compose.monitoring.yml up -d
```

Here is the exact startup sequence:
1. **Container Initialization:** The Prometheus container boots up and initializes its Time-Series Database (TSDB) engine.
2. **Configuration Read:** Prometheus reads `prometheus.yml` and discovers all target addresses.
3. **Time = 0s (Immediate First Scrape):** Prometheus executes its very first HTTP scrape attempt to every single computer in its target list immediately.
4. **Every 15 Seconds Thereafter (The Continuous Loop):**
   - An internal timer fires every 15 seconds (`15s`, `30s`, `45s`, `60s`...).
   - Prometheus sends an HTTP GET request to each target's `/metrics` URL.
   - It parses the response, adds a timestamp, and stores it in its local database.
5. **Handling Inactive Targets (`UP` vs `DOWN`):**
   - If a target computer or application is not running, Prometheus marks its target status as **`DOWN`**.
   - As soon as that target starts up, on the very next 15-second tick, Prometheus connects and transitions it to **`UP`**!

---

## Section 3: How Docker Talks to the Windows Host

A frequent question is: **If Prometheus runs inside Docker, and our Python AI Agent runs on the Windows host, how does Prometheus get the data?**

### The Docker Network Bridge (`host.docker.internal`)
1. **The Isolation Problem:**
   - Inside a Docker container, `localhost` refers only to the container itself.
   - Therefore, Prometheus inside Docker cannot reach `localhost:8000` on your Windows machine.
2. **The Special Address:**
   - Docker on Windows provides a built-in virtual DNS hostname: **`host.docker.internal`**.
3. **The 15-Second Scrape Transaction:**
   - Every 15 seconds, Prometheus in Docker calls:
     ```http
     GET http://host.docker.internal:8000/metrics
     ```
   - Docker's internal networking bridge translates `host.docker.internal:8000` directly to `localhost:8000/metrics` on your Windows machine.
   - Your Python server (`run_server.py`) answers with the current telemetry numbers.
   - Prometheus stores the samples, and Grafana reads them to update your dashboard.

---

## Section 4: How the AI Agent Processes a User Question

While Prometheus continuously collects server hardware data 24/7 in the background, **the AI Agent operates on-demand only when a user asks a question.**

Here is the 5-stage pipeline:

```
User Query: "What is the performance in node-1?"
   │
   ▼
1. INGESTION (run_server.py)
   FastAPI receives POST /api/v1/query.
   Starts high-resolution timer: time.perf_counter().
   Increments concurrency gauge: adk_in_flight_requests.
   │
   ▼
2. ROUTER (app/pipeline.py)
   Compares question against metric catalogs (skills/references/node-exporter/cpu.md).
   Decides: Target datasource is Prometheus, domain is CPU & Load, target host is node-1.
   │
   ▼
3. GENERATOR (app/llm_client.py + Google Gemini)
   Supplies metric schemas to Gemini.
   Gemini writes PromQL: node_load1{instance=~"node-1.*"} with 1h time range.
   Extracts token usage metadata (prompt + completion tokens).
   │
   ▼
4. DETERMINISTIC VALIDATOR (app/pipeline.py)
   Non-AI Python rule engine verifies query against catalog whitelists.
   Validates syntax, labels, and safety boundaries (blocks any mutating commands).
   │
   ▼
5. EXECUTOR (app/executor.py & app/prometheus_client.py)
   Sends HTTP request to Prometheus:
   GET http://localhost:9090/api/v1/query_range?query=node_load1...
   Prometheus returns real timestamped series points originally gathered by node_exporter.
   Executor sanitizes NaN/Infinity values.
   │
   ▼
6. USER RESPONSE
   Packages clean series points and human-readable explanation to client.
```

---

## Section 5: How Self-Monitoring (4 Golden Signals) Works

While the AI Agent is answering questions, it monitors its own health using **Google SRE's 4 Golden Signals**:

```
┌─────────────────────────────────────────────────────────────┐
│                 THE 4 GOLDEN SIGNALS IN CODE                │
├───────────────┬─────────────────────────────────────────────┤
│ 1. LATENCY    │ • adk_request_duration_seconds              │
│               │   (Total query response time in seconds)    │
│               │ • adk_pipeline_step_duration_seconds        │
│               │   (Breakdown: LLM vs Catalog vs Prometheus) │
├───────────────┼─────────────────────────────────────────────┤
│ 2. TRAFFIC    │ • adk_requests_total (Total queries / QPS)  │
│               │ • adk_llm_tokens_total                      │
│               │   (Prompt and completion tokens consumed)   │
├───────────────┼─────────────────────────────────────────────┤
│ 3. ERRORS     │ • adk_query_validation_total (Valid/Invalid)│
│               │ • adk_query_empty_results_total             │
│               │ • adk_llm_api_errors_total (HTTP 429 quota) │
├───────────────┼─────────────────────────────────────────────┤
│ 4. SATURATION │ • adk_in_flight_requests (Active threads)   │
│               │ • adk_active_sessions_gauge (User sessions) │
│               │ • process_resident_memory_bytes (RAM used)  │
└───────────────┴─────────────────────────────────────────────┘
```

1. **Recorded in Python RAM:** [`app/telemetry.py`](app/telemetry.py) increments counters and histograms as code executes.
2. **Exposed on `/metrics`:** [`run_server.py`](run_server.py) serializes these numbers into Prometheus text format when visited.
3. **Scraped by Prometheus:** The Docker container pulls from `host.docker.internal:8000/metrics` every 15s and evaluates the 17 alert rules in [`prometheus/alert_rules.yml`](prometheus/alert_rules.yml).
4. **Visualized in Grafana:** Open `http://localhost:3000` to view [`grafana/dashboards/ai_agent_golden_signals.json`](grafana/dashboards/ai_agent_golden_signals.json). The 4 Golden Signal rows update live with SLA thresholds.

---

## Section 6: Terminal Command Walkthrough

Sequential commands to verify each layer in the terminal:

### 1. Check an Exporter on any Monitored Server:
```bash
# On monitored host:
curl http://localhost:9100/metrics
```

### 2. Start the Prometheus & Grafana Monitoring Stack:
```powershell
docker compose -f docker-compose.monitoring.yml up -d
curl http://localhost:9090/api/v1/targets
```

### 3. Start the AI Agent Backend on Windows:
```powershell
python run_server.py
curl http://localhost:8000/metrics
```

### 4. Submit an On-Demand Question:
```powershell
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/query" `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"question": "What is the performance in node-1?"}'
```

### 5. View Grafana Dashboard:
Open browser: **[http://localhost:3000](http://localhost:3000)** (Login: `admin` / `admin`).

---

## Section 7: Master Step-to-File Reference Matrix

| Step # | Stage | Operation | Exact File / Component |
|:---:|---|---|---|
| **1** | **Kernel Activity** | Tracks CPU ticks and RAM allocations in `/proc` | Monitored Server OS (`/proc/stat`, `/proc/meminfo`) |
| **2** | **Host Export** | Exporter reads OS counters and serves port 9100 | `http://<server-ip>:9100/metrics` |
| **3** | **Continuous Storage** | Prometheus scrapes servers every 15s into TSDB | Prometheus Server (`:9090`) |
| **4** | **Backend Boot** | FastAPI starts up and loads configuration | [`run_server.py`](run_server.py) & [`app/config.py`](app/config.py) |
| **5** | **Catalog Definition**| Documents allowed metrics and query rules | [`skills/references/node-exporter/`](skills/references/node-exporter/) (`cpu.md`, `memory.md`) |
| **6** | **Query Ingestion** | Receives natural language question | [`run_server.py`](run_server.py) (`@app.post("/api/v1/query")`) |
| **7** | **Catalog Matching** | Router matches question to metric catalog | [`app/pipeline.py`](app/pipeline.py) (`run_pipeline()`) |
| **8** | **PromQL Generation** | Gemini generates PromQL & records token usage | [`app/llm_client.py`](app/llm_client.py) (`call_llm_json()`) |
| **9** | **Query Validation** | Python rule engine verifies query safety | [`app/pipeline.py`](app/pipeline.py) (`validate_contract()`) |
| **10**| **Live Data Fetch** | Executor sends HTTP request to Prometheus for target | [`app/executor.py`](app/executor.py) & [`app/prometheus_client.py`](app/prometheus_client.py) |
| **11**| **User Response** | Formats series data and returns to client | Returned from `run_server.py` |
| **12**| **Telemetry Record** | Latency, tokens, requests, and RAM recorded | [`app/telemetry.py`](app/telemetry.py) |
| **13**| **Docker Bridge Scrape**| Prometheus in Docker scrapes Windows host every 15s | [`prometheus/prometheus.yml`](prometheus/prometheus.yml) (`host.docker.internal:8000`) |
| **14**| **Alert Evaluation** | 17 SRE alert rules evaluated against SLAs | [`prometheus/alert_rules.yml`](prometheus/alert_rules.yml) |
| **15**| **Dashboard Render** | 4 Golden Signal rows (13 panels) rendered in UI | [`grafana/dashboards/ai_agent_golden_signals.json`](grafana/dashboards/ai_agent_golden_signals.json) (`:3000`) |
