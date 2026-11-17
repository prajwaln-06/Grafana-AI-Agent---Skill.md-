# 📊 Grafana Dashboard Presentation & Spoken Walkthrough
### Dashboard: *AI Agent — 4 Golden Signals Monitoring* (`http://localhost:3000`)

> **How to use this script:**  
> This walkthrough is designed to be presented **immediately after the Codebase Walkthrough** ([`CODEBASE_CHANGES_PRESENTATION.md`](file:///e:/Grafana-AI/CODEBASE_CHANGES_PRESENTATION.md)).  
> Open a browser to **`http://localhost:3000`**, project the Grafana dashboard, and deliver the **🗣️ What to Say** sections below.  
> The script is written in an **objective, professional, pronoun-free format**—explaining metric behavior, threshold mechanics, and architectural connections.

---

## 🎬 1. Transition from Codebase to Dashboard (Seamless Bridge)

### 🗣️ What to Say (Transition Script):
> *"Following the codebase walkthrough—from metric definitions in `app/telemetry.py` to middleware in `run_server.py` and guardrails in `app/pipeline.py`—attention now shifts to the live **Grafana Monitoring Dashboard** at `http://localhost:3000` to observe the system operating in real time.  
>  
> The dashboard structures telemetry strictly across **Google SRE's 4 Golden Signals**:  
> 1. **Signal 1: Latency** (Top row) — powered by `adk_request_duration_seconds` and `adk_pipeline_step_duration_seconds`.  
> 2. **Signal 2: Traffic** (Second row) — powered by token extraction in `app/llm_client.py`.  
> 3. **Signal 3: Errors & AI Quality** (Third row) — powered by the deterministic validator in `app/pipeline.py` and empty-result check in `app/executor.py`.  
> 4. **Signal 4: Saturation** (Bottom row) — powered by in-flight tracking and session filtering in `run_server.py`.  
>  
> The following walkthrough examines how each signal monitors service health."*

---

## ⏱️ ROW 1: Signal 1 — Latency (Response Time & Step Decomposition)

*(Direct attention to the top row of panels).*

### 🗣️ Row Introduction:
> *"The top row tracks Latency. It monitors overall end-to-end response times against the service level agreement and decomposes every request into individual internal pipeline phases."*

---

### Panel 1.1: `p95 Overall Latency (SLA: < 4.0s)`
* **🔗 Code Connection:** Fed by `adk_request_duration_seconds` recorded in [`run_server.py#L318`](file:///e:/Grafana-AI/run_server.py#L318) and defined in [`app/telemetry.py#L65-L66`](file:///e:/Grafana-AI/app/telemetry.py#L65-L66).
* **Widget Type:** Stat Callout (Configured with Thresholds)
* **What it measures:** The 95th percentile of user response time over the rolling 5-minute evaluation window.
* **Under the hood:** `histogram_quantile(0.95, rate(adk_request_duration_seconds_bucket[5m]))`
* **Configured SLA Threshold:** Target is `< 4.0s`.

#### 🗣️ What to Say:
> *"On the top left, this stat panel tracks **p95 Overall Latency**.  
> This panel is directly fed by the request timer middleware in `run_server.py` wrapping `/api/v1/query`. That code records total elapsed user wait time into the latency histogram. The production SLA target is 4.0 seconds. If upstream model delays or network latency push the 95th percentile above 4 seconds, this panel triggers a threshold alert."*

---

### Panel 1.2: `Overall Request Latency Percentiles`
* **🔗 Code Connection:** Fed by the latency histogram buckets defined in [`app/telemetry.py#L66`](file:///e:/Grafana-AI/app/telemetry.py#L66).
* **Widget Type:** Multi-line Timeseries
* **What it measures:** 4 separate distribution curves: `p50` (median), `p90`, `p95` (SLA target), and `p99` (tail latency).

#### 🗣️ What to Say:
> *"Next to it, this timeseries chart plots the latency distribution across four percentiles: p50 for typical response experience, p90, p95, and p99 for edge cases. Comparing p50 against p99 allows identifying tail-latency spikes even when typical median response times appear normal."*

---

### Panel 1.3: `Pipeline Step Breakdown (Catalog vs LLM vs Database)`
* **🔗 Code Connection:** Fed by `time_pipeline_step()` in [`app/pipeline.py#L2729`](file:///e:/Grafana-AI/app/pipeline.py#L2729) (`catalog_search`), [`app/llm_client.py#L76`](file:///e:/Grafana-AI/app/llm_client.py#L76) (`llm_generation`), and [`app/executor.py#L105`](file:///e:/Grafana-AI/app/executor.py#L105) (`prometheus_fetch`).
* **Widget Type:** Stacked Area Timeseries
* **What it measures:** Latency contribution per pipeline stage.

#### 🗣️ What to Say:
> *"This third panel is where the `time_pipeline_step()` context managers provide diagnostic visibility.  
> Rather than treating the agent as a single black box, this chart isolates where time is allocated across three distinct phases: catalog indexing, database fetching, and LLM reasoning. In practice, the local catalog search and Prometheus database queries are sub-second operations, while the cloud LLM generation step represents the primary contributor to overall user wait time. If overall latency rises, this breakdown immediately isolates the responsible stage."*

---

## 🚦 ROW 2: Signal 2 — Traffic (Workload Demand & Token Spend)

*(Scroll down to the second row).*

### 🗣️ Row Introduction:
> *"Moving to the second row: Traffic and Token Spend. This row monitors incoming workload demand alongside AI operational consumption."*

---

### Panel 2.1: `User Request Throughput (QPS)`
* **🔗 Code Connection:** Fed by `adk_requests_total` in [`run_server.py#L319`](file:///e:/Grafana-AI/run_server.py#L319).
* **Widget Type:** Area Timeseries
* **What it measures:** Queries per second sent to `/api/v1/query`, grouped by status (`success`, `client_error`, `server_error`).

#### 🗣️ What to Say:
> *"On the left, this panel tracks **Request Throughput in queries per second**. It segments traffic by HTTP status label, enabling continuous monitoring of workload volume and rapid identification if client-side validation errors or server-side exceptions emerge during traffic surges."*

---

### Panel 2.2: `AI Token Burn Velocity (Target: < 1M/hr)`
* **🔗 Code Connection:** Fed by `adk_llm_tokens_total` extracted from Gemini's `usage_metadata` in [`app/llm_client.py#L127-L135`](file:///e:/Grafana-AI/app/llm_client.py#L127-L135).
* **Widget Type:** Speedometer Gauge
* **What it measures:** Total token consumption rate projected on an hourly basis.
* **Configured Thresholds:** Baseline target `< 500k` | Warning `500k - 1M` | Critical `> 1M tokens/hr`.

#### 🗣️ What to Say:
> *"In the center is the **AI Token Burn Velocity gauge**. In cloud AI architectures, token usage directly determines operating costs.  
> This gauge is driven by the token extraction logic in `app/llm_client.py`, where Gemini's `usage_metadata` is intercepted to compute hourly velocity. If complex prompt expansions or repetitive queries accelerate token consumption toward 1 million tokens per hour, the gauge flags that consumption rate so costs can be proactively managed."*

---

### Panel 2.3: `Token Consumption Velocity (Prompt vs. Completion)`
* **🔗 Code Connection:** Fed by `telemetry.record_tokens(prompt_tokens, completion_tokens)` in [`app/llm_client.py#L130`](file:///e:/Grafana-AI/app/llm_client.py#L130).
* **Widget Type:** Multi-line Timeseries
* **What it measures:** Independent rates for **Prompt Tokens** (user input + catalog context) vs. **Completion Tokens** (generated PromQL output).

#### 🗣️ What to Say:
> *"On the right, this timeseries separates **Prompt tokens** from **Completion tokens**. Prompt token velocity tracks the volume of incoming instructions and reference context sent to the model, while completion token velocity tracks the size of generated outputs. This differential helps evaluate context efficiency and detect prompt bloat."*

---

## 🚨 ROW 3: Signal 3 — Errors & AI Quality (The Key Differentiator!)

*(Scroll down to the third row).*

### 🗣️ Row Introduction:
> *"Row 3 covers Errors and Quality. For AI agents, standard HTTP error codes alone are insufficient because models can return HTTP 200 responses while producing invalid queries or empty data. This row tracks those specific failure modes."*

---

### Panel 3.1: `AI Hallucination Rate (SLA: < 5%)`
* **🔗 Code Connection:** Fed by `_package_validated()` in [`app/pipeline.py#L1664-L1676`](file:///e:/Grafana-AI/app/pipeline.py#L1664-L1676), calling `telemetry.record_validation_result(result.passed)`.
* **Widget Type:** Stat Callout (Configured with Thresholds)
* **What it measures:** Percentage of total queries where generated output violated deterministic schema rules or referenced unverified metric names.
* **Configured SLA Threshold:** Target `< 5%`.

#### 🗣️ What to Say:
> *"This panel monitors the **AI Hallucination Rate**.  
> This connects directly to the validation hook in `app/pipeline.py` inside `_package_validated()`. Whenever the LLM produces a query contract, the deterministic Python validator verifies every metric and label against the verified catalog before any database execution occurs.  
> If the model invents a non-existent metric name—such as fabricating `'host system memory used'` instead of using verified exporter metrics—the validator blocks execution, marks the contract invalid, and increments the invalid query counter. If invalid contracts exceed 5% of traffic, this panel signals that guardrails intervened."*

---

### Panel 3.2: `Blank Chart Rate (SLA: < 3%)`
* **🔗 Code Connection:** Fed by `telemetry.record_empty_result()` in [`app/executor.py#L290`](file:///e:/Grafana-AI/app/executor.py#L290).
* **Widget Type:** Stat Callout (Configured with Thresholds)
* **What it measures:** Percentage of queries that were syntactically valid but returned zero data series from the underlying telemetry storage.
* **Configured SLA Threshold:** Target `< 3%`.

#### 🗣️ What to Say:
> *"Next is the **Blank Chart Rate**.  
> This is powered by the empty-series check in `app/executor.py`. It addresses a silent failure mode: an AI query may be completely valid in syntax, but target a time window or entity with no recorded telemetry data. In standard monitoring, this returns HTTP 200 while the user sees an empty panel.  
> When zero series are returned, an empty-result event is logged. If this rate exceeds the 3% threshold, it alerts engineering to review data retention windows or query scoping."*

---

### Panel 3.3: `AI Provider 429 Quota Errors (1h)`
* **🔗 Code Connection:** Fed by the 429 exception handler in [`app/llm_client.py#L142-L146`](file:///e:/Grafana-AI/app/llm_client.py#L142-L146).
* **Widget Type:** Stat Counter
* **What it measures:** Cumulative HTTP 429 (Rate Limit / Quota Exhaustion) errors from upstream AI providers over the trailing hour.

#### 🗣️ What to Say:
> *"This counter monitors **upstream API quota exhaustion**. In `app/llm_client.py`, any HTTP 429 rate-limiting responses from Google Gemini are caught and recorded. Any non-zero count here informs the team that upstream API throughput limits are being approached or exceeded."*

---

### Panel 3.4: `Alert Proposal Acceptance Rate`
* **🔗 Code Connection:** Fed by `telemetry.record_alert_proposal()` in [`app/agent.py#L79-L96, L176`](file:///e:/Grafana-AI/app/agent.py#L79-L96).
* **Widget Type:** Stat / Timeseries (%)
* **What it measures:** Proportion of AI-suggested alert rules approved by human operators (`confirmed / (confirmed + discarded) * 100`).

#### 🗣️ What to Say:
> *"The fourth panel tracks **Human-in-the-Loop Acceptance**.  
> In `app/agent.py`, telemetry monitors the lifecycle of alert proposals. When the AI proposes an alert rule, it enters a pending review state until an operator confirms or cancels it. This ratio indicates the proportion of AI-generated monitoring recommendations accepted by operators over time."*

---

## 🪣 ROW 4: Signal 4 — Saturation (Memory Fullness & Concurrency Overload)

*(Scroll down to the bottom row).*

### 🗣️ Row Introduction:
> *"The bottom row monitors system saturation: active request concurrency, stateful session memory, and process RAM allocation."*

---

### Panel 4.1: `In-Flight Concurrent Queries (Queue)`
* **🔗 Code Connection:** Fed by `with telemetry.track_in_flight():` in [`run_server.py#L249`](file:///e:/Grafana-AI/run_server.py#L249) and [`app/telemetry.py#L130-L141`](file:///e:/Grafana-AI/app/telemetry.py#L130-L141).
* **Widget Type:** Speedometer Gauge
* **What it measures:** Current number of requests actively executing within the pipeline.

#### 🗣️ What to Say:
> *"On the bottom left, the **In-Flight Concurrent Queries gauge** reflects real-time processing concurrency.  
> This reflects the `track_in_flight()` context manager in `run_server.py`, which increments upon request arrival and decrements upon completion. This gauge provides an immediate visual indicator of whether requests are executing concurrently or queueing under heavy load."*

---

### Panel 4.2: `Active In-Memory Chat Sessions`
* **🔗 Code Connection:** Fed by `_update_active_sessions_gauge()` in [`run_server.py#L233-L242`](file:///e:/Grafana-AI/run_server.py#L233-L242).
* **Widget Type:** Speedometer Gauge
* **What it measures:** Number of multi-turn chat sessions currently held in server RAM awaiting human action.
* **Configured Thresholds:** Baseline `< 10` | Elevated `10 - 20` | Critical `> 20`.

#### 🗣️ What to Say:
> *"In the center is **Active In-Memory Chat Sessions**.  
> Standard query requests are stateless and free memory immediately upon completion. However, when an alert proposal is generated, the agent retains that proposal context in memory until the user confirms or discards it.  
> This is controlled by `_update_active_sessions_gauge()` in `run_server.py`, which specifically filters for sessions with an active `pending_alert_proposal`. Once a user confirms or discards a proposal, the session state is cleared and the gauge decreases accordingly, ensuring memory retention remains bounded."*

---

### Panel 4.3: `Backend Container RSS Memory (Target: < 1.5 GB)`
* **🔗 Code Connection:** Scraped directly from Python's process resident memory (`process_resident_memory_bytes`).
* **Widget Type:** Timeseries & Current MB Stat
* **What it measures:** Physical RAM (Resident Set Size) consumed by the Python backend service.
* **Under the hood:** `process_resident_memory_bytes{job=~"ai.*agent.*"} / 1024 / 1024`
* **Configured Target:** Ceiling `< 1500 MB` (1.5 GB).

#### 🗣️ What to Say:
> *"Finally, on the bottom right is **Backend Container RSS Memory**.  
> This tracks the physical memory footprint of the running Python process relative to the 1.5-gigabyte container limit. Monitoring memory over time ensures the process remains stable without risk of Out-Of-Memory container termination."*

---

## 🏁 5. Wrap-Up & Closing Script (15 Seconds)

### 🗣️ What to Say:
> *"To summarize:  
> Examination of the Python implementation covered telemetry instrumentation, query lifecycle middleware, and validation guardrails.  
> Here in Grafana, those metrics translate into complete operational observability: ensuring the AI agent remains fast, cost-transparent, resilient against hallucinations, and memory-safe.  
>  
> Thank you for attending. The floor is now open for any questions."*
