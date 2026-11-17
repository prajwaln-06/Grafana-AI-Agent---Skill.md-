# 🚀 System Startup & Shutdown Runbook

This guide contains the exact terminal commands to start and stop the entire AI Observability Agent stack, including the 4-Node GPU Cluster Simulator, Prometheus, OpenSearch, Grafana, and the Python backend.

---

## 🟢 How to Start Everything

Ensure **Docker Desktop** is running before executing container commands. Open **PowerShell** and execute the commands in the sequence shown below:

### Step 1: Start the GPU Cluster Simulator (Prometheus, OpenSearch, Nodes)
```powershell
# Navigate into the updated GPU simulator Docker directory
Set-Location "E:\prom-simulator-updated"

# Start Prometheus (:9090), OpenSearch (:9600), OpenSearch Dashboards (:5601), incident controller (:9500), and all 4 GPU nodes
docker compose -f docker-compose.yml -f docker-compose.opensearch.yml up -d --build
```

---

### Step 2: Start the Grafana Monitoring Dashboard
```powershell
# Navigate to the AI Observability Agent project directory
Set-Location "E:\Grafana-AI"

# Start Grafana on port 3000 without starting a duplicate Prometheus (--no-deps)
docker compose -f docker-compose.monitoring.yml up -d --no-deps grafana node-exporter
```

---

### Step 3: Start the AI Agent Backend Server
```powershell
# Navigate to the AI Observability Agent project directory
Set-Location "E:\Grafana-AI"

# Start the Python FastAPI backend server listening on http://localhost:8000
python run_server.py
```

---

## 🔴 How to Shut Down Everything

To stop all background processes and containers:

### Step 1: Stop the Python Backend Server
```powershell
# Stop the Python server process listening on port 8000 (excluding PID 0)
$p = (Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue).OwningProcess | Where-Object { $_ -gt 0 } | Select-Object -Unique; if ($p) { Stop-Process -Id $p -Force }
```
*(Alternatively, press `Ctrl + C` in the terminal where `python run_server.py` is running).*

---

### Step 2: Stop Grafana
```powershell
# Navigate to the AI Agent directory
Set-Location "E:\Grafana-AI"

# Stop and remove the Grafana and local exporter containers
docker compose -f docker-compose.monitoring.yml down
```

---

### Step 3: Stop the GPU Cluster Simulator
```powershell
# Navigate into the updated GPU simulator Docker directory
Set-Location "E:\prom-simulator-updated"

# Stop and remove all simulator containers
docker compose -f docker-compose.yml -f docker-compose.opensearch.yml down
```

---

## 🌐 Web UI & Service Endpoints

Once started, services are accessible via a browser:

| Service | URL | Description | Credentials |
|---|---|---|:---:|
| **Grafana Dashboard** | [`http://localhost:3000`](http://localhost:3000) | AI Agent 4 Golden Signals Dashboard | `admin` / `admin` |
| **Prometheus Web UI** | [`http://localhost:9090`](http://localhost:9090) | Scrape targets, TSDB graphs, and PromQL query explorer | None |
| **Prometheus Targets** | [`http://localhost:9090/targets`](http://localhost:9090/targets) | Live health status of all 4 nodes + AI agent backend | None |
| **OpenSearch REST API** | [`http://localhost:9600`](http://localhost:9600) | Direct OpenSearch cluster search endpoint | None |
| **OpenSearch Dashboards** | [`http://localhost:5601`](http://localhost:5601) | Web UI for searching logs and cluster events | None |
| **Incident Controller UI** | [`http://localhost:9500/docs`](http://localhost:9500/docs) | Swagger UI for injecting failure scenarios | None |
| **AI Agent Health Check** | [`http://localhost:8000/health`](http://localhost:8000/health) | Backend readiness and liveness probe | None |
| **AI Agent API Query** | [`http://localhost:8000/api/v1/query`](http://localhost:8000/api/v1/query) | POST endpoint for natural language queries | None |

