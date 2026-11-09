# 🚀 3-Day Execution Plan: AI Agent Prometheus & Grafana Monitoring

> **Project Goal:** Fully instrument the Grafana AI Agent backend with Prometheus telemetry based on [monitor.md](file:///e:/Grafana-AI/monitor.md), configure Prometheus scraping, and deliver an import-ready Grafana dashboard visualizing Google SRE's **4 Golden Signals** (Latency, Traffic, Errors, Saturation).
>
> **Target Completion:** 3 Days  
> **Reference Document:** [`monitor.md`](file:///e:/Grafana-AI/monitor.md)

---

## 📅 Day-by-Day Roadmap Overview

```
┌─────────────────────────────────┐   ┌─────────────────────────────────┐   ┌─────────────────────────────────┐
│              DAY 1              │   │              DAY 2              │   │              DAY 3              │
│  Backend Telemetry & /metrics   │──>│  Prometheus Scrape & Traffic    │──>│  Grafana Dashboard & Alerts     │
│                                 │   │  Validation                     │   │                                 │
│ • app/telemetry.py registry     │   │ • prometheus.yml scrape config  │   │ • Full Grafana Dashboard JSON   │
│ • FastAPI middleware & timing   │   │ • Synthetic traffic test script │   │ • 4 Golden Signal visual rows   │
│ • Pipeline step instrumentation │   │ • Verify counters & histograms  │   │ • Prometheus alert rules        │
│ • GET /metrics endpoint         │   │ • Edge-case validation (429,0)  │   │ • End-to-end verification & doc │
└─────────────────────────────────┘   └─────────────────────────────────┘   └─────────────────────────────────┘
```

---

## 📋 Detailed Task Breakdown

### 🔹 Day 1: Telemetry Instrumentation & Backend Exposure

**Objective:** Implement the metrics registry in Python, wire metrics into request lifecycle and pipeline steps, and expose `GET /metrics`.

#### 1. Dependencies & Metrics Registry (`app/telemetry.py`)
- Add `prometheus-client>=0.20.0` to `requirements.txt`.
- Create `app/telemetry.py` to declare all metrics matching `monitor.md`:
  - **Latency:**
    - `adk_request_duration_seconds` (`Histogram`): Buckets `[0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 4.0, 6.0, 8.0, 10.0, 15.0]`.
    - `adk_pipeline_step_duration_seconds` (`Histogram`, label: `step`): Steps: `catalog_search`, `llm_generation`, `prometheus_fetch`.
  - **Traffic:**
    - `adk_requests_total` (`Counter`, label: `status`): Tracking question outcomes (`success`, `client_error`, `server_error`).
    - `adk_llm_tokens_total` (`Counter`, label: `type`): Tracking prompt vs completion tokens (`prompt_tokens`, `completion_tokens`).
  - **Errors & Quality:**
    - `adk_query_validation_total` (`Counter`, label: `result`): `valid` vs `invalid` (catches hallucinations).
    - `adk_query_empty_results_total` (`Counter`): Counts syntactically valid queries that return 0 data series.
    - `adk_llm_api_errors_total` (`Counter`, label: `status_code`): E.g. `429` (quota) or `500` (upstream outage).
    - `adk_alert_proposals_total` (`Counter`, label: `action`): `proposed`, `confirmed`, `discarded`.
  - **Saturation:**
    - `adk_in_flight_requests` (`Gauge`): Incremented on question arrival, decremented upon delivery.
    - `adk_active_sessions_gauge` (`Gauge`): Tracks count of active sessions retained in RAM.
    - Note: `process_resident_memory_bytes` is exported automatically by `prometheus_client`.

#### 2. FastAPI Integration (`run_server.py`)
- Add `GET /metrics` route (unauthenticated, added to `_PUBLIC_PATHS` alongside `/readyz` so Prometheus can scrape without an API key).
- Implement HTTP middleware to:
  - Increment/decrement `adk_in_flight_requests`.
  - Measure total request latency (`adk_request_duration_seconds`).
  - Record request status in `adk_requests_total`.

#### 3. Pipeline Instrumentation (`app/pipeline.py` & `app/agent.py`)
- Record execution time of catalog search, LLM PromQL generation, and database execution.
- Hook into the query validation check to increment `adk_query_validation_total{result="invalid"}` when hallucinations or illegal metric names are caught.
- Hook into the result handler to increment `adk_query_empty_results_total` when zero series are returned.
- Hook into `/api/v1/alerts/confirm` to record `adk_alert_proposals_total`.

#### Day 1 Deliverables:
- [x] `app/telemetry.py` created and tested.
- [x] `GET /metrics` accessible on `http://localhost:8000/metrics`.
- [x] Automated unit test `tests/test_telemetry.py` asserting all metrics are exposed (9/9 tests passing).

---

### 🔹 Day 2: Prometheus Scraper Configuration & Traffic Validation

**Objective:** Ensure Prometheus collects metrics cleanly every 15s, and run test traffic to verify that every metric responds as designed.

#### 1. Prometheus Scrape Configuration
- Create `prometheus/prometheus.yml` (or update existing simulator compose):
  ```yaml
  global:
    scrape_interval: 15s
    evaluation_interval: 15s

  scrape_configs:
    - job_name: 'ai-agent-backend'
      metrics_path: '/metrics'
      static_configs:
        - targets: ['localhost:8000'] # or host.docker.internal:8000
  ```

#### 2. Synthetic Traffic Generator (`scripts/smoke_test_monitoring.py`)
- Create a test script that exercises all 4 signals by simulating realistic scenarios:
  1. **Healthy queries:** Valid PromQL / OpenSearch questions $\rightarrow$ increments success counters & step histograms.
  2. **Hallucination test:** Queries requesting non-existent metrics $\rightarrow$ triggers `adk_query_validation_total{result="invalid"}`.
  3. **Empty result test:** Valid queries on inactive nodes $\rightarrow$ triggers `adk_query_empty_results_total`.
  4. **Concurrent load:** Burst of 10 parallel queries $\rightarrow$ confirms `adk_in_flight_requests` spikes and returns to 0.
  5. **Alert proposal flow:** Proposing and confirming an alert $\rightarrow$ updates `adk_alert_proposals_total`.

#### 3. Data Integrity Verification
- Query Prometheus API (`/api/v1/query`) to verify TSDB has stored:
  - `rate(adk_requests_total[5m])`
  - `histogram_quantile(0.95, rate(adk_request_duration_seconds_bucket[5m]))`
  - `adk_in_flight_requests`

#### Day 2 Deliverables:
- [x] `prometheus/prometheus.yml` configured and scraping every 15 seconds.
- [x] `prometheus/alert_rules.yml` created with 4 Golden Signal alerts.
- [x] `docker-compose.monitoring.yml` created for 1-click Prometheus + Grafana startup.
- [x] `scripts/smoke_test_monitoring.py` passing and generating telemetry for all 4 signals.
- [x] Automated unit tests `tests/test_smoke_test_monitoring.py` passing (5/5 passed).

---

### 🔹 Day 3: Grafana Dashboard Construction & Alerting Rules

**Objective:** Deliver the visual Grafana dashboard JSON matching `monitor.md` and export standard alert rules.

#### 1. Grafana Dashboard JSON (`grafana/dashboards/ai_agent_golden_signals.json`)
Build a 4-row dashboard structured around the **4 Golden Signals**:

| Section | Panel Name | Visualization | PromQL Query |
| :--- | :--- | :--- | :--- |
| **Row 1: Latency** | 95th Percentile Latency | Stat & Timeseries | `histogram_quantile(0.95, rate(adk_request_duration_seconds_bucket[5m]))` |
| | Pipeline Step Breakdown | Stacked Timeseries | `histogram_quantile(0.95, sum by (le, step) (rate(adk_pipeline_step_duration_seconds_bucket[5m])))` |
| **Row 2: Traffic** | Question Request Rate (QPS) | Timeseries | `sum(rate(adk_requests_total[5m]))` |
| | Token Burn Rate (Tokens/Hr) | Stat (Gauge) | `sum(increase(adk_llm_tokens_total[1h]))` |
| **Row 3: Errors** | Hallucination / Validation Failure Rate | Stat & Line | `sum(rate(adk_query_validation_total{result="invalid"}[5m])) / sum(rate(adk_requests_total[5m])) * 100` |
| | Empty Chart Rate | Stat & Line | `sum(rate(adk_query_empty_results_total[5m])) / sum(rate(adk_requests_total[5m])) * 100` |
| | AI Provider 429 Errors | Stat (Counter) | `sum(rate(adk_llm_api_errors_total{status_code="429"}[1m]))` |
| | Alert Proposal Acceptance Rate | Bar Gauge | `sum(adk_alert_proposals_total{action="confirmed"}) / sum(adk_alert_proposals_total{action="proposed"}) * 100` |
| **Row 4: Saturation** | In-Flight Concurrent Queries | Gauge | `adk_in_flight_requests` |
| | Active Sessions in Memory | Gauge | `adk_active_sessions_gauge` |
| | Container RAM Usage (MB) | Timeseries | `process_resident_memory_bytes / 1024 / 1024` |

#### 2. Prometheus Alerting Rules (`prometheus/alert_rules.yml`)
Export the alerting rules defined in `monitor.md`:
1. **AI Agent High Latency:** `p95 > 4.0s` for `5m`.
2. **AI Agent Token Burn Spike:** `sum(increase(adk_llm_tokens_total[1h])) > 1,000,000` for `1h`.
3. **AI Agent Upstream Quota Exhausted:** `sum(rate(adk_llm_api_errors_total{status_code="429"}[1m])) > 0` for `1m`.
4. **AI Agent High In-Memory Session Count:** `adk_active_sessions_gauge > 5000` for `10m`.

#### 3. Dashboard Provisioning Configuration
- Create `grafana/provisioning/dashboards/dashboard.yml` for automated loading on Grafana startup.
- Document manual 1-click import instructions (via Grafana UI: Dashboards $\rightarrow$ Import JSON).

#### Day 3 Deliverables:
- [x] Production-ready `grafana/dashboards/ai_agent_golden_signals.json`.
- [x] Auto-provisioning configs `grafana/provisioning/datasources/datasource.yml` and `dashboard.yml`.
- [x] Prometheus alert rules file `prometheus/alert_rules.yml` (17 rules).
- [x] Automated unit test suite `tests/test_grafana_dashboard.py` (6/6 tests passing).
- [x] End-to-end verification walkthrough proving live metrics render in Grafana.

---

## 🎯 Final Definition of Done (DoD)
1. **Endpoint Live:** Calling `curl http://localhost:8000/metrics` returns all `adk_*` metrics in standard Prometheus text format.
2. **Prometheus Scraping:** Prometheus scrapes `ai-agent-backend` with status `UP`.
3. **Grafana Working:** The dashboard imports into Grafana with zero errors and renders live charts for Latency, Traffic, Errors, and Saturation.
4. **Alerts Defined:** Alert rules in Prometheus match the exact thresholds from `monitor.md`.

