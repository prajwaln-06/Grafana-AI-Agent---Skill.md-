# 📋 OpenSearch Log Observability: Testing Catalog
### Dedicated Exclusively to OpenSearch Log Retrieval, Guardrails & Telemetry

This guide provides test queries and expected dashboard deflections specifically for **OpenSearch log operations** (`controllerlog-*`, `log_iml-*`, `slingshot_crayfabrichealth-*`).

*(For Prometheus metric queries, PromQL alerts, and GPU ratios, refer to [MONITORING_EXAMPLES.md](file:///e:/Grafana-AI/MONITORING_EXAMPLES.md)).*

---

## ⏱️ Signal 1: Latency (`opensearch_fetch` Breakdown)
* **Target Panels:** `P90 Pipeline Step Duration` & `P99 Query Latency`

### Test 1: Fetch Live System Controller Logs (`controllerlog-*`)
```powershell
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/query" `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"question": "Show me the recent system logs for node-00"}'
```
* **Resolved Index Pattern:** `controllerlog-*`
* **Execution Status:** `success`
* **Dashboard Deflection:**
  * **`P90 Pipeline Step Duration`:** Plots a purple/blue bar for **`opensearch_fetch`** directly next to `llm_generation`.
  * **`P99 Query Latency`:** Stays in **GREEN** (`< 4.0s`).
  * **`Blank Chart Rate`:** Stays at **`0%` (GREEN)**.

---

## 📈 Signal 2: Traffic (Log Token Ingestion)
* **Target Panels:** `User Request Throughput (QPS)` & `AI Token Burn Velocity`

### Test 2: Ingest Cluster Hardware Log Documents (`log_iml-*`)
```powershell
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/query" `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"question": "Show the recent iLO and BMC event records across the cluster"}'
```
* **Resolved Index Pattern:** `log_iml-*`
* **Execution Status:** `success`
* **Dashboard Deflection:**
  * **`User Request Throughput (QPS)`:** Spikes upward with incoming query volume.
  * **`AI Token Burn Velocity`:** Speedometer jumps higher as raw log lines are passed into the LLM context.
  * **`Token Consumption Velocity`:** Shows an elevated curve for `prompt` tokens relative to `completion` tokens.

---

## 🚨 Signal 3: Errors & Guardrails (OpenSearch Log Safety)

### Test 3A: Empty Search Result Detection (Blank Chart Rate Panel)
* **Target Panel:** `Blank Chart Rate` (SLA: < 3%)
```powershell
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/query" `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"question": "Are any Slingshot fabric switches in a degraded state?"}'
```
* **Why This Triggers:** All switches in the simulator are currently `HEALTHY`. OpenSearch finds `0` degraded switch documents (`count = 0`), so the backend logs `execution_status: "empty_result"`.
* **Dashboard Deflection:**
  * **`Blank Chart Rate`:** Spikes into **RED** (`> 3%`), alerting operators that an empty result set was returned from OpenSearch.

---

### Test 3B: Prohibited Text Grouping Guardrail & Multi-Turn Clarification
* **Goal:** Prove the **DSL Guard** blocks unsafe high-cardinality text grouping (`never_group`), requests user clarification, and successfully resolves the query upon clarification.

#### Step 1: Send Prohibited Text Grouping Query (Creates Active Clarification Session)
```powershell
$resp = Invoke-RestMethod -Uri "http://localhost:8000/api/v1/query" `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"question": "In controller logs, group all entries by Body"}'

# Store the active session ID
$sessionId = $resp.session_id
Write-Host "Active Clarification Session ID: $sessionId"
```
* **Why This Triggers:** In [`index_registry.json`](file:///e:/Grafana-AI/app/opensearch_catalog/index_registry.json#L178), `controllerlog-*` enforces `"never_group": ["Body"]` to protect the cluster from memory exhaustion.
* **Execution Result:**
  * Returns `status: "declined"` with `reason: "parameter_requires_clarification"`.
  * Returns a valid `session_id` (e.g. `s-ef90fac5-...`).
  * Suggests grouping by a structured field such as `Resource.service.name`.
* **Dashboard Deflection:**
  * **`Active In-Memory Chat Sessions`:** Increments from `0` to `1` (retains conversation context in RAM awaiting clarification).
  * **`OpenSearch Cluster`:** Zero malformed aggregation queries reach port `9600`.

#### Step 2: Submit Clarification (Executes Query & Frees Session Memory)
```powershell
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/query" `
  -Method Post `
  -ContentType "application/json" `
  -Body (@{
      question = "Show error count grouped by Resource.service.name over the last 1 hour";
      session_id = $sessionId
  } | ConvertTo-Json)
```
* **Execution Result:**
  * OpenSearch returns grouped service buckets (e.g. `hmpad`, `kernel`, `hmscd`).
  * Returns `session_id: null`.
* **Dashboard Deflection:**
  * **`Active In-Memory Chat Sessions`:** Context is cleared from RAM and drops back to `0`.

---

### Test 3C: Unapproved Index Safe Refusal
* **Goal:** Prove the agent safely refuses unapproved or non-existent log indices without fabricating endpoints.
```powershell
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/query" `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"question": "Show me user authentication attempts from the audit_vault index"}'
```
* **Execution Result:** `audit_vault` is not in [`index_registry.json`](file:///e:/Grafana-AI/app/opensearch_catalog/index_registry.json). Returns `status: "unmapped"`.
* **Dashboard Deflection:**
  * **`AI Hallucination Rate`:** Stays at **`0%` (GREEN)** because the agent safely declined rather than inventing a fake endpoint.

---

## 🔋 Signal 4: Saturation & PII Redaction
* **Target Panels:** `Backend Container RSS Memory` & `In-Flight Concurrent Queries`

* **Automatic Log PII Sanitization:**
  [`app/opensearch_catalog/redaction.py`](file:///e:/Grafana-AI/app/opensearch_catalog/redaction.py) automatically masks usernames (`user_****`) and IP addresses (`10.100.x.x`) in all returned documents before delivery.
* **Dashboard Deflection:**
  * **`Backend Container RSS Memory`:** Reflects memory consumed during document parsing and regex redaction, remaining well below the **1.5 GB SLA limit**.
  * **`In-Flight Concurrent Queries`:** Increments to `1` during execution and drops back to `0`.

---

## 🔄 Synchronized Incident Correlation Walkthrough

1. **Inject Incident (Incident Controller on Port `9500`):**
   ```powershell
   Invoke-RestMethod -Uri "http://localhost:9500/events" `
     -Method Post `
     -ContentType "application/json" `
     -Body '{"type": "gpu_thermal_throttle", "node": "node-00", "duration_s": 240}'
   ```
2. **Observe Hardware Error Log in OpenSearch Dashboards:**
   * Open [`http://localhost:5601`](http://localhost:5601) $\rightarrow$ Discover $\rightarrow$ `log_iml-*` $\rightarrow$ Query: `sim.event_id: *`.
   * Displays matching iLO hardware alert:
     `System Overheating (Accelerator 0) ... Location node-00`
3. **Query the AI Agent for Unified Root Cause Diagnosis:**
   ```powershell
   Invoke-RestMethod -Uri "http://localhost:8000/api/v1/query" `
     -Method Post `
     -ContentType "application/json" `
     -Body '{"question": "Diagnose the hardware status on node-00"}'
   ```
   * The AI Agent queries Prometheus metrics (temperature spike) and OpenSearch logs (iLO hardware fault), correlating both in the same execution cycle.
