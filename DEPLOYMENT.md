# 🚀 Production Deployment Guide: AI Observability Agent & Golden Signals Stack

This guide details the exact steps to deploy the complete **AI Observability Agent**, **Prometheus**, **Grafana**, **Node Exporter**, and optional **DCGM Exporter (GPU)** and **OpenSearch** stack in a production environment.

---

## 🏛️ System Architecture in Production

```
┌────────────────────────────────────────────────────────────────────────┐
│ 1. CLUSTER HARDWARE & TELEMETRY LAYER                                  │
│    • Linux Server Hosts ──────> node-exporter (:9100)                  │
│    • NVIDIA GPU Nodes    ──────> dcgm-exporter (:9400) [GPU hosts only] │
│    • Switch Fabric/BMC   ──────> OpenSearch (:9200)   [Log storage]    │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    │ Scrapes :9100, :9400, :8000
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 2. CENTRAL OBSERVABILITY STACK                                         │
│    • Prometheus (:9090): Central TSDB scraper + 17 SRE Alert Rules     │
│    • Grafana (:3000): 4 Golden Signals Dashboard (Auto-Provisioned)     │
└───────────────────────────────────▲────────────────────────────────────┘
                                    │
                                    │ PromQL & OpenSearch REST Queries
                                    │
┌───────────────────────────────────┴────────────────────────────────────┐
│ 3. AI AGENT BACKEND (FastAPI on Port 8000)                             │
│    • Translates natural language into verified PromQL & OpenSearch DSL │
│    • Deterministic Validator guarantees zero hallucinated metrics      │
│    • Emits Google SRE 4 Golden Signals to GET /metrics                │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 📋 1. Prerequisites Checklist

* **Docker & Docker Compose:** Docker Engine v24+ and Docker Compose v2+ installed.
* **Python Runtime:** Python 3.10+ (if running the agent directly on the host or inside a container).
* **Gemini API Key:** `GEMINI_API_KEY` set with access to `gemini-3.5-flash-lite` (or custom model).
* **NVIDIA GPU Nodes (Optional):** NVIDIA Container Toolkit installed if running DCGM Exporter for GPUs.
* **OpenSearch Cluster (Optional):** OpenSearch 2.x cluster accessible via network if querying syslog/switch logs.

---

## ⚙️ 2. Environment Configuration (`.env`)

Create or update your production `.env` file in the project root:

```ini
# =============================================================================
# AI Agent Core Settings
# =============================================================================
GEMINI_API_KEY=your_production_gemini_api_key_here
GEMINI_MODEL=gemini-3.5-flash-lite
API_KEY=your_secure_api_key_here

# =============================================================================
# Datasource Backends
# =============================================================================
PROMETHEUS_URL=http://localhost:9090
OPENSEARCH_URL=http://opensearch-cluster:9200
OPENSEARCH_ENABLED=true

# =============================================================================
# Grafana & Alert Proposals
# =============================================================================
GRAFANA_URL=http://localhost:3000
GRAFANA_SERVICE_ACCOUNT_TOKEN=glsa_your_token_here
GRAFANA_DEFAULT_FOLDER_UID=alerts
ALERT_RULE_CREATION_ENABLED=true
```

---

## 🚢 3. Deployment Modes

### Mode A: Standard Deployment (CPU Cluster / Default)
Runs **Prometheus**, **Grafana**, and **Node Exporter**:

```bash
docker compose -f docker-compose.monitoring.yml up -d
```
* **Prometheus:** `http://localhost:9090` (Scraping agent, Prometheus, and Node Exporter on `:9100`)
* **Grafana:** `http://localhost:3000` (Auto-loaded 4 Golden Signals dashboard)
* **Node Exporter:** `http://localhost:9100/metrics` (Streaming real CPU, RAM, Disk, and Load metrics)

---

### Mode B: GPU-Enabled Deployment (NVIDIA GPU Cluster)
If the host has NVIDIA GPUs and NVIDIA Container Toolkit installed, launch with the `gpu` profile to spin up **DCGM Exporter**:

```bash
docker compose -f docker-compose.monitoring.yml --profile gpu up -d
```
* Spawns `ai_agent_dcgm_exporter` on port `9400` with full GPU passthrough.
* Prometheus automatically scrapes `DCGM_*` metrics (temperature, VRAM, utilization, power).

---

### Mode C: OpenSearch-Enabled Deployment (Log Search Engine)
If you want to run a dedicated local OpenSearch instance for controller syslogs, fabric switch logs, and BMC events:

```bash
docker compose -f docker-compose.monitoring.yml --profile opensearch up -d
```
* Spawns `ai_agent_opensearch` on port `9200` (Search REST API) and port `9600`.
* The AI Agent sends structured DSL queries directly to `http://localhost:9200`.

---

### Mode D: All-in-One Full Stack (Prometheus + Grafana + Node Exporter + DCGM + OpenSearch)
Spins up all observability services simultaneously:

```bash
docker compose -f docker-compose.monitoring.yml --profile all up -d
```

---

### Mode E: Enterprise Multi-Node / Cloud Cluster
In enterprise production deployments where OpenSearch or GPU clusters are already hosted separately:
1. **Existing OpenSearch Cluster:** Set `OPENSEARCH_URL=http://your-enterprise-opensearch:9200` in `.env`. The AI Agent connects directly over the network with optional basic auth (`OPENSEARCH_AUTH_USERNAME`, `OPENSEARCH_AUTH_PASSWORD`).
2. **Kubernetes GPU Nodes:** Deploy `dcgm-exporter` as a standard Kubernetes `DaemonSet` on nodes labeled `nvidia.com/gpu: "true"`. Prometheus scrapes their port `9400`.

---

## 🚀 4. Starting the AI Agent Backend

Start the AI Agent FastAPI service:

```bash
# Production mode with Uvicorn workers:
python run_server.py
```

Or run via production Gunicorn / Uvicorn command:
```bash
uvicorn run_server.py:app --host 0.0.0.0 --port 8000 --workers 4
```

---

## ✅ 5. Post-Deployment Verification Checklist

Run these quick checks to verify end-to-end connectivity:

| Check | Command / URL | Expected Output |
|---|---|---|
| **Agent Health** | `curl http://localhost:8000/health` | `{"status": "ok"}` |
| **Agent Readiness** | `curl http://localhost:8000/readyz` | `{"status": "ready", "skill_name": "..."}` |
| **Telemetry Scrape** | `curl http://localhost:8000/metrics` | Returns Prometheus `adk_*` plain-text metrics |
| **Prometheus Targets** | `http://localhost:9090/targets` | `ai-agent-backend`, `node-exporter` show **UP** |
| **Grafana Dashboard** | `http://localhost:3000` | Golden Signals Dashboard renders live with green status |
| **Automated Tests** | `pytest tests/` | **100% Passing** across all test suites |

