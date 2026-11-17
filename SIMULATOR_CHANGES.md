# Simulator Infrastructure Modifications & Alerting Setup

## Overview

This document provides a comprehensive record of all configuration modifications made to the simulated infrastructure stack located in `prom-simulator-updated`. All changes are non-breaking, backwards-compatible, and isolated to container orchestration and monitoring configuration.

---

## 1. Port Remapping for Host Port Isolation

To enable concurrent execution of the FastAPI AI agent backend, OpenSearch cluster, and simulator controller without port collisions, the following host port mappings were adjusted in `docker-compose.opensearch.yml`:

| Service | Internal Container Port | Original Host Port | Updated Host Port | Purpose |
| :--- | :--- | :--- | :--- | :--- |
| **`sim-controller`** | `8000` | `8000` | `9500` | Eliminates port collision with the FastAPI AI agent backend (`http://localhost:8000`). Swagger UI is accessible at `http://localhost:9500/docs`. |
| **`opensearch`** | `9200` | `9250` | `9600` | Avoids port range collision with node exporter instances (`9200`–`9203`). REST API is accessible at `http://localhost:9600`. |

### Networking Isolation Note
* Inter-container communication within the Docker network remains unchanged, as services reference internal DNS names (`sim-controller:8000`, `opensearch:9200`, `node-00:9200`).
* No internal application code or exporter configurations required alteration.

---

## 2. Prometheus Scrape Configuration (`prometheus.docker.yml`)

The Prometheus configuration was extended to scrape the AI agent backend metrics and evaluate alert rules without disturbing existing simulated node exporters.

```yaml
global:
  scrape_interval: 10s
  evaluation_interval: 10s

rule_files:
  - "alert_rules.yml"

scrape_configs:
  # Existing simulated fleet scrape jobs (preserved unchanged)
  - job_name: 'simulated_fleet'
    static_configs:
      - targets: ['node-00:9200']
        labels: {node_id: 'node-00', cluster: 'simulated'}
      - targets: ['node-01:9200']
        labels: {node_id: 'node-01', cluster: 'simulated'}
      - targets: ['node-02:9200']
        labels: {node_id: 'node-02', cluster: 'simulated'}
      - targets: ['node-03:9200']
        labels: {node_id: 'node-03', cluster: 'simulated'}

  # Added AI Agent Backend scrape target
  - job_name: 'ai_agent'
    metrics_path: '/metrics'
    static_configs:
      - targets: ['host.docker.internal:8000']
        labels:
          service: 'ai-observability-agent'
          env: 'production'
```

---

## 3. Prometheus Alerting Rules Integration

### 3.1 Docker Compose Volume Mount (`docker-compose.yml`)
The Prometheus service definition was updated to mount the rule definition file into the container:

```yaml
  prometheus:
    image: prom/prometheus:latest
    volumes:
      - ./prometheus.docker.yml:/etc/prometheus/prometheus.yml:ro
      - ./alert_rules.yml:/etc/prometheus/alert_rules.yml:ro
    ports: ["9090:9090"]
    depends_on: [node-00, node-01, node-02, node-03]
```

### 3.2 Alert Rules (`alert_rules.yml`)
The `alert_rules.yml` file contains 16 alerting rules categorized under Google SRE Four Golden Signals:

1. **Service Availability**:
   * `AIAgentServiceDown`: Triggers critical alert if agent scrape fails (`up == 0`) for > 1 minute.
2. **Latency**:
   * `AIAgentHighLatency`: Warning alert when request duration p95 > 4.0s for > 5 minutes.
   * `AIAgentCriticalLatency`: Critical alert when request duration p95 > 8.0s for > 2 minutes.
   * `AIAgentLLMGenerationSlow`: Step duration p95 > 3.0s for > 5 minutes.
   * `AIAgentDatabaseFetchSlow`: Database fetch duration p95 > 1.5s for > 5 minutes.
3. **Traffic**:
   * `AIAgentTokenBurnSpike`: Hourly token burn > 1,000,000 tokens for > 15 minutes.
   * `AIAgentTokenBudgetWarning`: Hourly token burn > 500,000 tokens for > 30 minutes.
   * `AIAgentRequestSurge`: Throughput > 20 QPS for > 2 minutes.
4. **Errors**:
   * `AIAgentHighServerErrorRate`: HTTP 5xx rate > 5% for > 3 minutes.
   * `AIAgentUpstreamQuotaExhausted`: HTTP 429 rate > 0 for > 1 minute.
   * `AIAgentUpstreamProviderOutage`: LLM 5xx rate > 0 for > 2 minutes.
   * `AIAgentHighHallucinationRate`: Schema validation failure rate > 5% for > 5 minutes.
   * `AIAgentHighEmptyResultRate`: Empty query result rate > 3% for > 5 minutes.
5. **Saturation**:
   * `AIAgentHighInMemorySessionCount`: Active in-memory session count > 5,000 for > 10 minutes.
   * `AIAgentCriticalSessionMemoryLeak`: Active in-memory session count > 10,000 for > 5 minutes.
   * `AIAgentConcurrencySaturation`: In-flight requests > 50 for > 2 minutes.
   * `AIAgentProcessMemoryHigh`: Process resident memory (RSS) > 1.5 GB for > 5 minutes.

---

## 4. Backwards Compatibility & Verification

* **Core Code Invariance**: No modifications were made to `generator.py`, `app.py`, node exporter logic, chaos injection schedules, or log generators.
* **Target Health**: All Prometheus scrape targets (`simulated_fleet` and `ai_agent`) remain in `health: "up"` state.
* **Rule Engine**: All 16 rules evaluate with `health: "ok"` accessible via `http://localhost:9090/api/v1/rules`.
