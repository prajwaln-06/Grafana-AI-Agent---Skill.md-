# 📊 AI Agent Monitoring with Prometheus: Complete 4 Golden Signals Architecture & Runbook

> **Meeting Objective & Architecture Guide:** A complete, presentation-ready guide explaining how we monitor the Grafana AI Agent using Prometheus. Covers Google SRE's **4 Golden Signals** (Latency, Traffic, Errors, Saturation) in simple, accessible language for engineering managers, DevOps teams, and stakeholders, backed by production PromQL queries and a complete 17-rule alerting suite.
>
> **Reference Standards:**
> - Google SRE Book — [*Monitoring Distributed Systems* (Chapter 6)](https://sre.google/sre-book/monitoring-distributed-systems/)
> - Prometheus Exposition Format (OpenMetrics standard)
> - Grafana Production Dashboards & Alerts

---

## 💡 Slide 1: Why Traditional Monitoring Fails for an AI Agent

A standard web service fails loudly: if the database is dead or code crashes, it immediately returns an **HTTP 500 error** and sends a pager alert.

**An AI Agent fails quietly without crashing:**
* 🎭 **It hallucinates metrics:** The AI can invent a metric name that does not exist (`server_magic_cpu_usage`) and still return an HTTP 200 OK.
* 📭 **It returns blank charts:** The query syntax is valid, but it returns **0 data points** (empty chart) because of an incorrect label filter or targeting inactive nodes.
* 🐢 **It runs slow unnoticed:** The AI model might take 10+ seconds to generate an answer, causing engineers to abandon the tool during live production incidents.
* 💸 **It burns API credits:** An accidental prompt loop or retry storm can burn **thousands of dollars in AI API tokens** in minutes.
* 💥 **It causes memory crashes:** Every chat conversation is kept in memory for follow-up context. If abandoned tabs are not expired, server RAM fills up, causing a sudden container crash (**Out-Of-Memory kill**).

```
Traditional Service:   Crash / 500 Error  ──>  Pager fires immediately 🚨
AI Agent Service:      Fake PromQL / 200  ──>  User sees blank graph, no alert fired! ⚠️
                       (Requires 4 Golden Signals domain telemetry to catch)
```

---

## ⚙️ Slide 2: Prometheus Collection Architecture & Pipeline Lifecycle

The AI Agent runs as a web server (FastAPI on Port 8000). Every 15 seconds, the Prometheus scraper pulls a snapshot of its internal numbers:

```
+---------------------------------------------------------------------------------+
|                               PROMETHEUS SERVER                                 |
|           (Pulls metrics every 15 seconds via HTTP GET /metrics)                 |
+---------------------------------------+-----------------------------------------+
                                        |
                             HTTP GET   |   HTTP Response
                          /metrics      |   (Plain-text Prometheus metrics)
                                        v
+---------------------------------------------------------------------------------+
|                                AI AGENT BACKEND                                 |
|                             (FastAPI on Port 8000)                              |
|                                                                                 |
|   1. Tracks user questions, response times, token spend, and errors             |
|   2. Formats all counters, gauges, and histograms into Prometheus format        |
|   3. Exposes live numbers on: http://localhost:8000/metrics                     |
+---------------------------------------------------------------------------------+
```

### 1. Step-by-Step Request Lifecycle & Telemetry Hooks

Here is the exact metric collection sequence when an engineer asks: *"Show CPU on worker-1"*:

```
[ Engineer asks: "Show CPU on worker-1" ]
        │
        ├──> 1. Question arrives:
        │       • +1 added to active requests running right now: `adk_in_flight_requests`
        │       • Starts stopwatch for total end-to-end request latency
        │
        ├──> 2. Step 1 — Metric Catalog Search (~0.2s):
        │       • Agent searches OpenSearch / metadata index to locate candidate metrics
        │       • Timed: `adk_pipeline_step_duration_seconds{step="catalog_search"}`
        │
        ├──> 3. Step 2 — AI PromQL Synthesis (~3.0s):
        │       • AI model (Gemini) generates the PromQL query string
        │       • Timed: `adk_pipeline_step_duration_seconds{step="llm_generation"}`
        │       • Tokens counted: `adk_llm_tokens_total{type="prompt_tokens|completion_tokens"}`
        │       • Catches quota errors: `adk_llm_api_errors_total{status_code="429"}`
        │
        ├──> 4. Step 3 — Hallucination & Syntax Validation (~0.05s):
        │       • Checks whether AI wrote a real, permitted metric or hallucinated
        │       • Recorded: `adk_query_validation_total{result="valid|invalid"}`
        │
        ├──> 5. Step 4 — Database Query Execution (~1.5s):
        │       • Executes verified query on Prometheus or OpenSearch
        │       • Timed: `adk_pipeline_step_duration_seconds{step="prometheus_fetch"}`
        │       • If chart is empty (0 series): `adk_query_empty_results_total` incremented
        │
        ├──> 6. Step 5 — In-Memory Conversation Retention:
        │       • Stores chat context in RAM for conversational follow-ups
        │       • Monitored: `adk_active_sessions_gauge` (prevents RAM leaks)
        │
        └──> 7. Response Delivery:
                • Records total wait time: `adk_request_duration_seconds` (p95)
                • Records status: `adk_requests_total{status="success|client_error|server_error"}`
                • Decrements active requests by 1: `adk_in_flight_requests`
```

---

## 🏆 Slide 3: The 4 Golden Signals Applied to the AI Agent

Google SRE defines the **4 Golden Signals** as the foundational pillars of system health:

```
+-------------------------------------------------------------------------------------------------+
|                               THE 4 GOLDEN SIGNALS OF THE AI AGENT                              |
+-----------------------+-----------------------+------------------------+------------------------+
|      1. LATENCY       |      2. TRAFFIC       |       3. ERRORS        |     4. SATURATION      |
|   How long does the   |   How many queries    |    How often does the  |   How close is the     |
|   agent take to       |   and tokens is the   |    agent fail, time    |   agent backend to     |
|   answer?             |   agent handling?     |    out, or hallucinate?|   running out of RAM?  |
+-----------------------+-----------------------+------------------------+------------------------+
```

---

### ⏱️ Signal 1: Latency (Response Time & Step Decomposition)

**Definition:**
Latency represents the total elapsed duration from when an engineer submits a prompt until the response or visual graph is rendered on screen.

**Service Level Agreement (SLA):**
* **Target SLA:** Responses delivered within **2.0 to 4.0 seconds** (`p95 < 4.0s`).
* **Warning Threshold:** `p95 > 4.0s` for 5 minutes.
* **Critical Threshold:** `p95 > 8.0s` for 2 minutes (imminent user abandonment / timeout).
* **Pipeline Decomposition Breakdown:**
  1. `catalog_search` (< 0.5s): Metric directory matching.
  2. `llm_generation` (< 3.0s): Upstream LLM PromQL synthesis.
  3. `prometheus_fetch` / `opensearch_fetch` (< 1.5s): Database time-series query execution.

> 💡 **Why Measure p95 (95th Percentile) Instead of Average?**
> Averages hide severe spikes: if 99 engineers wait 1.0s and 1 engineer hangs for 90s, the *average* is ~1.9s (looks healthy), but users are experiencing outages. **p95** guarantees that 95 out of 100 requests finish within the threshold.

#### Telemetry Metrics:
| Metric Name | Type | Target Threshold | Recorded When | Business Impact |
|---|:---:|:---:|---|---|
| `adk_request_duration_seconds` | **Histogram** | p95 `< 4.0s` | At response delivery | Total user wait time; prevents tool abandonment during incidents. |
| `adk_pipeline_step_duration_seconds` | **Histogram** | LLM `< 3.0s`, DB `< 1.5s` | At step completion | Pinpoints whether delay stems from Catalog, Gemini LLM, or Prometheus DB. |

#### Production PromQL Queries:
```promql
# 1. 95th Percentile Overall Request Latency
histogram_quantile(0.95, rate(adk_request_duration_seconds_bucket[5m]))

# 2. Pipeline Step Latency Breakdown (Find the Bottleneck)
histogram_quantile(0.95, sum by (le, step) (rate(adk_pipeline_step_duration_seconds_bucket[5m])))
```

#### Alerting Rules (Prometheus):
```yaml
# Warning: Overall Latency SLA breached
- alert: AIAgentHighLatency
  expr: histogram_quantile(0.95, rate(adk_request_duration_seconds_bucket[5m])) > 4.0
  for: 5m
  labels: { severity: warning, signal: latency }
  annotations:
    summary: "AI Agent response latency is degrading (p95 > 4.0s)"

# Critical: Severe Latency / Hanging requests
- alert: AIAgentCriticalLatency
  expr: histogram_quantile(0.95, rate(adk_request_duration_seconds_bucket[5m])) > 8.0
  for: 2m
  labels: { severity: critical, signal: latency }
  annotations:
    summary: "AI Agent response latency is CRITICAL (p95 > 8.0s)"

# Warning: Upstream LLM slow
- alert: AIAgentLLMGenerationSlow
  expr: histogram_quantile(0.95, sum by (le) (rate(adk_pipeline_step_duration_seconds_bucket{step="llm_generation"}[5m]))) > 3.0
  for: 5m
  labels: { severity: warning, signal: latency }
  annotations:
    summary: "Upstream LLM query generation is slow (> 3.0s)"

# Warning: Database fetch slow
- alert: AIAgentDatabaseFetchSlow
  expr: histogram_quantile(0.95, sum by (le) (rate(adk_pipeline_step_duration_seconds_bucket{step=~"prometheus_fetch|opensearch_fetch"}[5m]))) > 1.5
  for: 5m
  labels: { severity: warning, signal: latency }
  annotations:
    summary: "Database fetch execution is slow (> 1.5s)"
```

---

### 🚦 Signal 2: Traffic (Workload Demand, Surges & Token Cost)

**Definition:**
Traffic measures the operational load and throughput handled by the AI Agent service.

**Operational Dimensions:**
1. **User Request Traffic (QPS):** Volume of questions engineers submit over time. Measures user adoption and peak incident demand.
2. **Token Traffic (Cost Velocity):** Volume of AI prompt & completion tokens processed per hour. Prevents runaway cost spikes from looping scripts or retry storms.

#### Telemetry Metrics:
| Metric Name | Type | Target Threshold | Recorded When | Business Impact |
|---|:---:|:---:|---|---|
| `adk_requests_total` | **Counter** | Normal baseline | After each question | Measures incoming request volume and success vs. error proportions. |
| `adk_llm_tokens_total` | **Counter** | `< 1,000,000 / hr` | After each LLM call | Tracks token velocity to enforce budget governance and stop runaway billing. |

#### Production PromQL Queries:
```promql
# 1. Incoming Questions Per Second (QPS)
sum(rate(adk_requests_total[5m]))

# 2. Token Burn Rate (Tokens Per Hour)
sum(increase(adk_llm_tokens_total[1h]))

# 3. Token Breakdown: Prompt Tokens vs. Completion Tokens
sum by (type) (rate(adk_llm_tokens_total[5m]))
```

#### Alerting Rules (Prometheus):
```yaml
# Critical: Runaway Token Burn Spike
- alert: AIAgentTokenBurnSpike
  expr: sum(increase(adk_llm_tokens_total[1h])) > 1000000
  for: 15m
  labels: { severity: critical, signal: traffic }
  annotations:
    summary: "Runaway AI token burn spike (> 1M tokens/hr)"

# Warning: Token Budget Velocity Elevated
- alert: AIAgentTokenBudgetWarning
  expr: sum(increase(adk_llm_tokens_total[1h])) > 500000
  for: 30m
  labels: { severity: warning, signal: traffic }
  annotations:
    summary: "High AI token consumption velocity (> 500k tokens/hr)"

# Warning: Traffic Surge / Loop Storm
- alert: AIAgentRequestSurge
  expr: sum(rate(adk_requests_total[5m])) > 20
  for: 2m
  labels: { severity: warning, signal: traffic }
  annotations:
    summary: "Abnormal surge in user questions (> 20 QPS)"
```

---

### 🚨 Signal 3: Errors (Failures, Hallucinations & Empty Results)

**Definition:**
Error telemetry tracks failures across both standard web infrastructure and AI-specific domain intelligence.

**The 5 Failure Modes of the AI Agent:**
1. **HTTP 5xx Server Errors:** Unhandled exceptions or internal pipeline crashes.
2. **AI Provider Rate Limits (HTTP 429):** Quota exhaustion on the Gemini/OpenAI API.
3. **Upstream AI Outages (HTTP 5xx from LLM):** External AI model downtime.
4. **AI Hallucinations / Syntax Errors:** Synthesized PromQL queries with fabricated metric names caught by the validator.
5. **Empty Results (Blank Charts):** Syntactically valid queries that return 0 data series to the user.

#### Telemetry Metrics:
| Metric Name | Type | Target Threshold | Recorded When | Business Impact |
|---|:---:|:---:|---|---|
| `adk_requests_total{status="server_error"}` | **Counter** | `< 1%` | When request fails | Standard 500/502/504 crash detection. |
| `adk_llm_api_errors_total` | **Counter** | `0` (Zero tolerance) | When LLM call fails | Detects API quota blocks (429) or upstream cloud outages (5xx). |
| `adk_query_validation_total` | **Counter** | `< 5%` invalid rate | After validation check | Catches **hallucinations** before queries run on the database. |
| `adk_query_empty_results_total` | **Counter** | `< 3%` empty rate | After DB query returns | Catches silent failures where the user receives a blank chart. |
| `adk_alert_proposals_total` | **Counter** | `> 80%` accepted | When user acts on alert | Evaluates quality and trust of AI-recommended alert thresholds. |

#### Production PromQL Queries:
```promql
# 1. HTTP 5xx Server Error Percentage
(sum(rate(adk_requests_total{status="server_error"}[5m])) / sum(rate(adk_requests_total[5m]))) * 100

# 2. AI Provider Rate Limit Hits (HTTP 429)
sum(rate(adk_llm_api_errors_total{status_code="429"}[1m]))

# 3. AI Hallucination Rate (% Invalid Queries)
(sum(rate(adk_query_validation_total{result="invalid"}[5m])) / sum(rate(adk_requests_total[5m]))) * 100

# 4. Blank Chart Rate (% Zero-Series Queries)
(sum(rate(adk_query_empty_results_total[5m])) / sum(rate(adk_requests_total[5m]))) * 100
```

#### Alerting Rules (Prometheus):
```yaml
# Critical: HTTP 5xx Server Error Rate > 5%
- alert: AIAgentHighServerErrorRate
  expr: (sum(rate(adk_requests_total{status="server_error"}[5m])) / sum(rate(adk_requests_total[5m]))) * 100 > 5
  for: 3m
  labels: { severity: critical, signal: errors }
  annotations:
    summary: "AI Agent HTTP 5xx server error rate > 5%"

# Critical: AI Provider Quota Exhausted (HTTP 429)
- alert: AIAgentUpstreamQuotaExhausted
  expr: sum(rate(adk_llm_api_errors_total{status_code="429"}[1m])) > 0
  for: 1m
  labels: { severity: critical, signal: errors }
  annotations:
    summary: "AI Provider API quota exhausted (HTTP 429)"

# Critical: AI Provider Cloud Outage (HTTP 5xx)
- alert: AIAgentUpstreamProviderOutage
  expr: sum(rate(adk_llm_api_errors_total{status_code=~"5.*"}[1m])) > 0
  for: 2m
  labels: { severity: critical, signal: errors }
  annotations:
    summary: "AI Provider cloud service outage (HTTP 5xx)"

# Warning: High Hallucination Rate (> 5%)
- alert: AIAgentHighHallucinationRate
  expr: (sum(rate(adk_query_validation_total{result="invalid"}[5m])) / sum(rate(adk_requests_total[5m]))) * 100 > 5
  for: 5m
  labels: { severity: warning, signal: errors }
  annotations:
    summary: "AI Hallucination rate exceeded 5%"

# Warning: Blank Charts / Empty Results (> 3%)
- alert: AIAgentHighEmptyResultRate
  expr: (sum(rate(adk_query_empty_results_total[5m])) / sum(rate(adk_requests_total[5m]))) * 100 > 3
  for: 5m
  labels: { severity: warning, signal: errors }
  annotations:
    summary: "AI Agent returning blank charts (> 3% empty results)"
```

---

### 🪣 Signal 4: Saturation (Memory Fullness & Concurrency Overload)

**Definition:**
Saturation measures how close the AI Agent is to resource exhaustion—specifically memory (RAM) and concurrent worker thread capacity.

**Operational Risk & Mitigation:**
* **Conversation Context in RAM:** The agent preserves prior messages in memory for conversational multi-turn follow-ups.
* **Leak Risk:** Forgotten or abandoned browser sessions leave conversations allocated in RAM.
* **Out-Of-Memory (OOM) Kill:** If container memory reaches the OS cgroup limit, the kernel terminates the process, causing service downtime.

#### Telemetry Metrics:
| Metric Name | Type | Target Threshold | Recorded When | Business Impact |
|---|:---:|:---:|---|---|
| `adk_in_flight_requests` | **Gauge** | `< 50` concurrent | Session start (+1) / finish (-1) | Tracks queries running right now; high values indicate worker starvation. |
| `adk_active_sessions_gauge` | **Gauge** | `< 5,000` sessions | Session touch / TTL expiry | Monitors chat sessions retained in RAM to prevent memory accumulation. |
| `process_resident_memory_bytes` | **Gauge** | `< 1.5 GB` | OS process scrape | Tracks physical container RAM usage (RSS) to avert OOM container kills. |

#### Production PromQL Queries:
```promql
# 1. In-Flight Concurrent Requests
adk_in_flight_requests

# 2. Active In-Memory Chat Sessions
adk_active_sessions_gauge

# 3. Process Resident Set Size (RSS) Memory in Megabytes
process_resident_memory_bytes{job="ai-agent-backend"} / 1024 / 1024
```

#### Alerting Rules (Prometheus):
```yaml
# Warning: High In-Memory Session Count (> 5,000)
- alert: AIAgentHighInMemorySessionCount
  expr: adk_active_sessions_gauge > 5000
  for: 10m
  labels: { severity: warning, signal: saturation }
  annotations:
    summary: "High session context accumulation in memory (> 5,000)"

# Critical: Severe Session Memory Leak (> 10,000)
- alert: AIAgentCriticalSessionMemoryLeak
  expr: adk_active_sessions_gauge > 10000
  for: 5m
  labels: { severity: critical, signal: saturation }
  annotations:
    summary: "CRITICAL: Chat session memory accumulation (> 10,000)"

# Warning: Worker Concurrency Queue Saturation (> 50 in-flight)
- alert: AIAgentConcurrencySaturation
  expr: adk_in_flight_requests > 50
  for: 2m
  labels: { severity: warning, signal: saturation }
  annotations:
    summary: "High concurrent question queueing (> 50 in-flight)"

# Warning: Process RAM Limit Approaching (> 1.5 GB RSS)
- alert: AIAgentProcessMemoryHigh
  expr: process_resident_memory_bytes{job="ai-agent-backend"} > 1610612736
  for: 5m
  labels: { severity: warning, signal: saturation }
  annotations:
    summary: "AI Agent process memory usage high (> 1.5 GB)"
```

---

### 💓 Signal 0: Service Availability (Liveness Heartbeat)

In addition to the 4 signals, service liveness must be monitored at the scraper level:

```yaml
# Critical: AI Agent backend crashed or stopped
- alert: AIAgentServiceDown
  expr: up{job="ai-agent-backend"} == 0
  for: 1m
  labels: { severity: critical, signal: availability }
  annotations:
    summary: "AI Agent backend service is DOWN"
    description: "Prometheus cannot scrape http://localhost:8000/metrics for over 1 minute."
```

---

## 📋 Slide 4: Master Alert Matrix (17 Rules)

The table below maps all 17 alert rules implemented in [`prometheus/alert_rules.yml`](file:///e:/Grafana-AI/prometheus/alert_rules.yml) directly to the 4 Golden Signals:

| # | Alert Rule Name | Golden Signal | Severity | Threshold Condition | For Duration | Operational Triage Action |
|:---:|---|:---:|:---:|---|:---:|---|
| **1** | `AIAgentServiceDown` | **Availability** | 🔴 Critical | `up == 0` | 1m | Restart agent container; check container crash logs. |
| **2** | `AIAgentHighLatency` | **Latency** | 🟡 Warning | `p95(request_duration) > 4.0s` | 5m | Check step duration to isolate LLM vs database. |
| **3** | `AIAgentCriticalLatency` | **Latency** | 🔴 Critical | `p95(request_duration) > 8.0s` | 2m | Investigate upstream API hangs or database lockups. |
| **4** | `AIAgentLLMGenerationSlow` | **Latency** | 🟡 Warning | `p95(llm_generation) > 3.0s` | 5m | Check Gemini API latency; consider smaller model. |
| **5** | `AIAgentDatabaseFetchSlow` | **Latency** | 🟡 Warning | `p95(db_fetch) > 1.5s` | 5m | Optimize Prometheus/OpenSearch queries and indexes. |
| **6** | `AIAgentTokenBurnSpike` | **Traffic** | 🔴 Critical | `tokens > 1,000,000 / hr` | 15m | Investigate runaway prompt loops or retry storms. |
| **7** | `AIAgentTokenBudgetWarning` | **Traffic** | 🟡 Warning | `tokens > 500,000 / hr` | 30m | Review API budget and high-volume consumers. |
| **8** | `AIAgentRequestSurge` | **Traffic** | 🟡 Warning | `requests > 20 QPS` | 2m | Identify looping clients or automated scripts. |
| **9** | `AIAgentHighServerErrorRate` | **Errors** | 🔴 Critical | `5xx error rate > 5%` | 3m | Inspect FastAPI traceback logs for runtime crashes. |
| **10** | `AIAgentUpstreamQuotaExhausted` | **Errors** | 🔴 Critical | `HTTP 429 errors > 0` | 1m | Request Gemini quota increase or enable backup API key. |
| **11** | `AIAgentUpstreamProviderOutage` | **Errors** | 🔴 Critical | `HTTP 5xx from LLM > 0` | 2m | Check Google Cloud Status dashboard; failover provider. |
| **12** | `AIAgentHighHallucinationRate` | **Errors** | 🟡 Warning | `invalid validation > 5%` | 5m | Refine prompt instructions and catalog embeddings. |
| **13** | `AIAgentHighEmptyResultRate` | **Errors** | 🟡 Warning | `empty charts > 3%` | 5m | Review query label matching and node inventory. |
| **14** | `AIAgentHighInMemorySessionCount` | **Saturation** | 🟡 Warning | `active_sessions > 5,000` | 10m | Check session TTL cleanup worker. |
| **15** | `AIAgentCriticalSessionMemoryLeak`| **Saturation** | 🔴 Critical | `active_sessions > 10,000` | 5m | Force session purge to avoid container OOM kill. |
| **16** | `AIAgentConcurrencySaturation` | **Saturation** | 🟡 Warning | `in_flight > 50` | 2m | Scale out backend replicas or increase thread pool. |
| **17** | `AIAgentProcessMemoryHigh` | **Saturation** | 🟡 Warning | `RSS RAM > 1.5 GB` | 5m | Check for Python memory leaks; recycle container. |

---

## 🛠️ Slide 5: How to Run & Verify the Complete Monitoring Stack

### 1. Start the Stack (1-Click Docker Compose)
```powershell
# Launches Prometheus (Port 9090) and Grafana (Port 3000)
docker compose -f docker-compose.monitoring.yml up -d
```

### 2. Verify Prometheus Scraper & Metrics
```powershell
# Test live agent /metrics endpoint directly
curl http://localhost:8000/metrics

# Run the automated smoke test script
python scripts/smoke_test_monitoring.py
```

### 3. Run Automated Unit Tests
```powershell
# Validate all telemetry metrics and alerting rules
pytest tests/test_telemetry.py tests/test_smoke_test_monitoring.py -v
```

---

### Executive Summary:
By implementing all **4 Golden Signals** across **10 custom telemetry metrics**, **15-second Prometheus scraping**, and **17 targeted alert rules**, the engineering team ensures complete visibility into the AI Agent. We catch hallucinations, empty charts, quota exhaustion, memory leaks, and latency degradation **before** they impact production users.
