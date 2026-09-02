# 🪐 Grafana AI Agent — Fullstack Observability Assistant

An intelligent, production-ready AI assistant that translates natural language into verified telemetry queries, constructs multi-stage diagnostics, provisions live Grafana dashboards and alert rules, and streams real-time cluster health metrics.

Built on **Google ADK**, **FastMCP (Model Context Protocol)**, **FastAPI**, **React 18 + Vite**, and the **SKILL.md v1.4 Observability Specification**.

---

## ⚡ Quick Start (1-Command Run)

### 1. Configure Environment
```bash
cp .env.example .env
```
Open `.env` and insert your **`GEMINI_API_KEY`**.

### 2. Launch Everything Together
```bash
./start.sh
```
* **Backend**: `http://localhost:8008`
* **Frontend UI**: `http://localhost:5173`

👉 Open your browser to **`http://localhost:5173`**!

### 🛑 To Stop Everything
```bash
./stop.sh
```

---

## 🛠️ Manual Setup & Separate Terminals

If you prefer running backend and frontend in separate terminal windows:

### Prerequisites
* Python 3.10+ (with virtual environment)
* Node.js 18+ and npm
* Docker running Prometheus (`:9090`), Grafana (`:3000`), and Loki (`:3100`)

### Backend Setup (Terminal 1)
```bash
# 1. Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Start the API Gateway
python run_server.py
```
* Backend starts on `http://localhost:8008` with auto-reload.

### Frontend Setup (Terminal 2)
```bash
cd ui
npm install
npm run dev
```
* Frontend starts on `http://localhost:5173` with live HMR.

---

## 🌟 Core Features & Operational Capabilities

### 1. Telemetry Querying & Node Comparisons
* **Zero Fabrication & Strict Label Discipline (Principle 9)**: Discovers actual runtime label values (`node_id`, `instance`) from Prometheus rather than guessing.
* **Instant vs. Range Queries**: Generates `instant` top-N evaluations or multi-node historical timeseries.
* **Interactive Charting**: Synchronized dual-line charts, categorical bar charts, and SVG radial gauges.

### 2. Multi-Stage Dependent Reasoning (SKILL.md v1.4)
* Supports chained queries where the second step depends on the output of the first (e.g. *"Which node has the highest CPU utilization right now, and how much memory is available on that node?"*).
* Executes Step 1 $\rightarrow$ extracts the winning entity $\rightarrow$ scopes Step 2 $\rightarrow$ synthesizes both results.

### 3. Live Dashboard Authoring (Grafana FastMCP)
* **Create from Scratch**: Compiles 2+ panel dashboards with automated grid layouts and PromQL expressions.
* **Update Live Dashboards**: Inserts gauges, stats, and logs into existing Grafana dashboards.
* **Interactive Proposals**: Generates proposal cards with live chart previews and JSON diffs before applying changes to Grafana.

### 4. Alert Rule Creation & Provisioning (§12)
* Translates threshold requests into formal Prometheus alert rules (`condition_query`, `operator`, `threshold`, `for_duration`).
* **Human-in-the-Loop Confirmation**: Click **"Confirm / Create Alert"** in the UI to provision directly into Grafana with a live deep link.

### 5. Real-Time Metrics Rail
* Real-time sidebar sparklines monitoring **CPU Busy %**, **Memory Available**, **GPU Temperature**, and **GPU Utilization**.
* 1-click collapse (`✕`) to expand the chat interface to 100% full screen.

---

## 🎬 Tested Demo Prompts

| Feature Area | Prompt | Expected Outcome |
|---|---|---|
| **Multi-Node Comparison** | `plot cpu utilization for node-00 and node-01 over the last hour` | Synchronized side-by-side timeseries chart |
| **Root-Cause Triage (Chained)** | `Which node has the highest CPU utilization right now, and how much memory is available on that node?` | Discovers top node $\rightarrow$ queries memory $\rightarrow$ returns 2-in-1 synthesis |
| **Create Dashboard** | `create a dashboard called Fleet Overview with node_cpu_seconds_total and node_memory_MemAvailable_bytes` | Generates 2-panel dashboard proposal card with "Apply Changes" button |
| **Update Dashboard** | `add a DCGM_FI_DEV_GPU_TEMP gauge to dashboard uid observability-overview` | Appends GPU gauge to live Grafana dashboard |
| **Alert Rule Provisioning** | `alert me if CPU utilization exceeds 90% on node-00 for 5 minutes` | Proposes alert $\rightarrow$ Confirm button creates live Grafana rule |
| **Log Diagnostics** | `show error logs for container log-generator-ai-lab over the last 15 minutes` | Queries Loki and streams structured error events |

---

## 🧪 Testing & Verification

### Run Backend Unit Tests (205 Tests)
```bash
pytest tests/
```
* All 205 tests are deterministic and run in under 3 seconds with zero external dependencies.

### Build Frontend Production Bundle
```bash
cd ui && npm run build
```
* Compiles TypeScript and bundles Vite assets with 0 errors.

---

## 📁 Repository Structure

```text
├── start.sh                 # 1-click startup script (Backend + Frontend)
├── stop.sh                  # 1-click stop script
├── run_server.py            # Main entrypoint launching the unified API Gateway
├── .env.example             # Complete environment variables template
├── app/
│   ├── api/
│   │   ├── main.py          # FastAPI application & router registry
│   │   ├── routes_chat.py   # Unified chat gateway & multi-turn coordinator
│   │   ├── routes_query.py  # Telemetry query endpoint (/api/v1/query)
│   │   └── routes_proposals.py # Dashboard proposal lifecycle
│   ├── pipeline.py          # Router -> Generator -> Validator -> Executor pipeline
│   ├── label_discovery.py   # Live Prometheus runtime label harvester
│   ├── validator.py         # Deterministic schema & PromQL validator
│   ├── executor.py          # Prometheus / Loki / OpenSearch execution engine
│   └── grafana_tools/       # FastMCP Grafana dashboard authoring & compilation
├── skills/
│   ├── SKILL.md             # Observability Query Builder specification (v1.4)
│   └── references/          # Domain reference guides (Node Exporter, DCGM, PromQL)
└── ui/
    ├── src/
    │   ├── App.tsx          # Main application shell & tab navigation
    │   ├── api.ts           # Unified frontend API client
    │   └── components/
    │       ├── AdkAssistantTab.tsx   # Conversational chat UI & metrics rail
    │       ├── ProposalCard.tsx      # Interactive Grafana proposal card
    │       └── TimeSeriesChart.tsx   # Recharts visualization engine (Line, Gauge, Bar)
    └── package.json
```

---

## 🔧 Configuration Reference (`.env`)

| Variable | Default | Description |
|---|---|---|
| `GEMINI_API_KEY` | *(Required)* | Google Gemini / Vertex AI API key |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Model used for Router and Generator passes |
| `PROMETHEUS_URL` | `http://localhost:9090` | Prometheus HTTP endpoint |
| `GRAFANA_URL` | `http://localhost:3000` | Grafana OSS endpoint |
| `GRAFANA_SERVICE_ACCOUNT_TOKEN` | `glsa_...` | Grafana Service Account token with Admin/Editor permissions |
| `ALERT_RULE_CREATION_ENABLED` | `true` | Enables alert rule proposal and provisioning (§12) |
| `DEPENDENT_QUERY_RESOLUTION_ENABLED` | `true` | Enables multi-stage chained query resolution (v1.4) |
| `PIPELINE_TIMEOUT_SECONDS` | `45.0` | Timeout ceiling for LLM and backend query resolution |
