## 0. Purpose and How To Use This Document

This file is a **one-stop reference for constructing PromQL queries** — not a fixed list of the only questions this agent can answer. Each skill below has two layers:

1. **Routing metadata** (Purpose / Trigger Examples / Do Not Use / Known Information / Expected Behaviour) — decides **whether this skill applies** to the user's question at all.
2. **Technical cookbook** (metric type, labels, example queries, gotchas) — decides **how to build the actual query**, once routing has already picked this skill.

**Important scope note:** the cookbook examples in each skill are *teaching examples*, not an exhaustive whitelist. Combined with Section 1 (Fundamentals), the agent should construct queries for questions no single example covers, by recombining fundamentals and cookbook patterns. Whether this generalization actually works reliably on novel combinations is what Phase 2 verification tests — this document states the intent, it does not by itself guarantee the outcome.

### 0.1 Skill Directory

| Skill | Topic | Use when the question is about... |
|---|---|---|
| CPU Usage (Infra) | Machine-level CPU | A whole server/machine's CPU |
| Memory Usage (Infra) | Machine-level RAM | A whole server/machine's memory |
| Process CPU Usage | App/service CPU | One specific process or service's CPU |
| Process Memory Usage | App/service RAM | One specific process or service's memory |

### 0.2 Construction Procedure (applies to every skill below)

1. Match the question to a skill using its Purpose/Trigger Examples — and check it isn't excluded by that skill's Do Not Use list.
2. Check the metric's **type** (Counter vs. Gauge, in Known Information) — this determines which functions are even valid (Section 1.1).
3. Find the cookbook example structurally closest to the question.
4. Adapt it — swap in the real instance/label/time window/threshold from the question. Never return an unmodified example if the specifics differ.
5. Combine multiple cookbook patterns if the question asks for more than one capability at once (e.g., "top 3 *and* compare to last week").
6. Sanity-check the unit and rough magnitude of the result against Known Information before presenting it.

---

## 1. PromQL Fundamentals (apply to all skills below)

### 1.1 Metric Types — and why the type changes everything

| Type | What it means | How you're allowed to query it |
|---|---|---|
| **Counter** | A value that only ever goes up (resets to 0 only on restart). Represents a *total accumulated so far*. | **Never use the raw value directly across time.** Always wrap in `rate()`, `irate()`, or `increase()` first. |
| **Gauge** | A value that can go up or down freely — a *snapshot right now*. | Can be used directly, or with `avg_over_time()`, `max_over_time()`, `deriv()`, `predict_linear()`, etc. |

### 1.2 Instant Vector vs. Range Vector

- `metric_name{label="x"}` → an **instant vector**: one value per series, at one point in time.
- `metric_name{label="x"}[5m]` → a **range vector**: every sample in the last 5 minutes, per series — must be fed into a function like `rate()` or `avg_over_time()` to reduce back to a usable value.

### 1.3 Core Functions Reference

| Function | Use on | What it does |
|---|---|---|
| `rate(x[5m])` | Counter | Average per-second rate of increase — the standard way to turn a Counter into a usable "speed" |
| `irate(x[5m])` | Counter | Same idea, using only the last two points — more reactive, noisier |
| `increase(x[5m])` | Counter | Total increase over the window |
| `avg_over_time(x[1h])` | Gauge / computed expr | Smoothed average over the window |
| `max_over_time(x[1h])` / `min_over_time(x[1h])` | Gauge | Worst/best point during the window |
| `deriv(x[30m])` | Gauge | Per-second rate of change (slope) |
| `predict_linear(x[1h], 3600)` | Gauge | Projects the current trend forward N seconds — used for forecasting |
| `quantile_over_time(0.95, x[1h])` | Gauge | 95th-percentile value seen during the window |

### 1.4 Aggregation Operators

| Operator | What it does |
|---|---|
| `sum by (label) (...)` / `avg by (label) (...)` | Combine values, grouped by a label |
| `max by (label) (...)` / `min by (label) (...)` | Highest/lowest value per group |
| `topk(N, ...)` / `bottomk(N, ...)` | The N highest/lowest series |
| `count by (label) (...)` | How many series exist per group |

### 1.5 Time and Vector-Matching Modifiers

- **`offset 1d`** — shifts the query back in time, for "now vs. yesterday" comparisons: `rate(metric[5m] offset 1d)`.
- **Subquery `[1h:1m]`** — required to run `avg_over_time`/`max_over_time`/`deriv` over an *already-computed expression* (like a percentage formula), not a bare metric.
- **`on()` / `ignoring()` / `group_left` / `group_right`** — used when combining two different metrics in one expression, to control which labels must match.

### 1.6 Global Gotchas

- **Counter resets** on restart are handled correctly by `rate()`/`increase()` — raw subtraction across time is not, and can silently go negative.
- **Rate window sizing:** should be at least 4× the scrape interval, or results get noisy/undefined.
- **Staleness:** a dead target's series goes stale after 5 minutes by default — expect empty results, not zeroes.
- **Extrapolation at edges:** `rate()`/`increase()` estimate slightly beyond the window's exact boundary samples.

---

## 2. Skill: CPU Usage (Infra)

### Routing Metadata

**Purpose:** Answers questions about machine-level CPU usage, utilization, or load across one or more monitored servers.

**Trigger Examples:**
- "Show CPU usage"
- "What's the CPU load on HOST-01"
- "Is CPU usage climbing"
- "Which machine has the highest CPU right now"
- "Was there a CPU spike last night"
- "Will CPU hit 100% soon"

**Do Not Use:**
- Memory-related questions → *Memory Usage (Infra)*
- A specific application/process's CPU (not the whole machine) → *Process CPU Usage*
- Disk, network, or log queries

**Known Information:**
- Metric: `windows_cpu_time_total`
- Type: Counter
- Data Source: Prometheus (`windows_exporter`)
- Unit: % (derived — see cookbook)
- Labels: `instance`, `core`, `mode`

**Expected Behaviour:** Follow the Construction Procedure in Section 0.2 — confirm this skill (not Memory or Process CPU) fits, confirm this is a Counter (needs `rate()`), find the closest cookbook match below, adapt it to the real instance/window/threshold asked, and sanity-check the result lands in a valid 0–100% range.

### Technical Cookbook

**Labels:**
| Label | Meaning | Example values |
|---|---|---|
| `instance` | Which machine reported this | `HOST-01:9182` |
| `core` | Which logical CPU core | `0`, `1`, `2`... |
| `mode` | What the core was doing | `idle`, `user`, `privileged`, `interrupt`, `dpc` |

**1. Current CPU usage %, averaged across all cores, per instance:**
```promql
100 - (avg by (instance) (rate(windows_cpu_time_total{mode="idle"}[5m])) * 100)
```

**2. CPU usage % per individual core:**
```promql
100 - (rate(windows_cpu_time_total{mode="idle"}[5m]) * 100)
```

**3. CPU usage % for one specific machine:**
```promql
100 - (avg by (instance) (rate(windows_cpu_time_total{mode="idle", instance="HOST-01:9182"}[5m])) * 100)
```

**4. Smoothed average CPU usage over the last hour:**
```promql
avg_over_time((100 - (avg by (instance) (rate(windows_cpu_time_total{mode="idle"}[5m])) * 100))[1h:1m])
```

**5. Worst CPU spike in the last 24 hours:**
```promql
max_over_time((100 - (avg by (instance) (rate(windows_cpu_time_total{mode="idle"}[5m])) * 100))[24h:5m])
```

**6. Compare CPU usage now vs. this time yesterday:**
```promql
(100 - (avg by (instance) (rate(windows_cpu_time_total{mode="idle"}[5m])) * 100))
-
(100 - (avg by (instance) (rate(windows_cpu_time_total{mode="idle"}[5m offset 1d])) * 100))
```

**7. Top 5 machines by CPU usage right now:**
```promql
topk(5, 100 - (avg by (instance) (rate(windows_cpu_time_total{mode="idle"}[5m])) * 100))
```

**8. Alert-style — machines sustained above 90% CPU:**
```promql
(100 - (avg by (instance) (rate(windows_cpu_time_total{mode="idle"}[5m])) * 100)) > 90
```

**9. Trend direction — climbing or falling?:**
```promql
deriv((100 - (avg by (instance) (rate(windows_cpu_time_total{mode="idle"}[5m])) * 100))[30m:1m])
```

**10. Forecast — will CPU breach 95% within the next hour?:**
```promql
predict_linear((100 - (avg by (instance) (rate(windows_cpu_time_total{mode="idle"}[5m])) * 100))[30m:1m], 3600) > 95
```

**11. Breakdown — what is the CPU actually doing?:**
```promql
sum by (mode) (rate(windows_cpu_time_total{instance="HOST-01:9182"}[5m])) * 100
```

**Gotchas:** Always filter/subtract `mode="idle"` — averaging across all modes without this is meaningless. Per-core values (query #2) can look noisy at short windows — this often correctly reflects real scheduling, not a bug.

**Related Metrics:** `windows_system_processor_queue_length` (thread contention signal).

---

## 3. Skill: Memory Usage (Infra)

### Routing Metadata

**Purpose:** Answers questions about machine-level RAM usage or availability across monitored servers.

**Trigger Examples:**
- "How's memory looking"
- "How much RAM is free on HOST-01"
- "Is any server running low on memory"
- "Will we run out of memory soon"
- "Compare memory usage to yesterday"

**Do Not Use:**
- CPU questions → *CPU Usage (Infra)*
- A specific application/process's memory → *Process Memory Usage*
- Disk space questions

**Known Information:**
- Metrics: `windows_memory_available_bytes` (free RAM), `windows_cs_physical_memory_bytes` (total RAM, denominator only)
- Type: Both Gauges
- Data Source: Prometheus (`windows_exporter`)
- Unit: bytes → GB or %
- Labels: `instance`

**Expected Behaviour:** Confirm this skill fits (not CPU or Process Memory), remember both metrics are Gauges (`deriv`/`predict_linear` apply directly, no `rate()` needed), find the closest cookbook match, adapt it, and sanity-check the % falls between 0–100 or the GB figure is plausible for the machine.

### Technical Cookbook

**Labels:** `instance` — which machine reported this.

**1. Current memory usage %:**
```promql
100 - (windows_memory_available_bytes / windows_cs_physical_memory_bytes * 100)
```

**2. Current free memory, in GB:**
```promql
windows_memory_available_bytes / 1073741824
```

**3. Memory usage % for one specific machine:**
```promql
100 - (windows_memory_available_bytes{instance="HOST-01:9182"} / windows_cs_physical_memory_bytes{instance="HOST-01:9182"} * 100)
```

**4. Smoothed average memory usage over the last hour:**
```promql
avg_over_time((100 - (windows_memory_available_bytes / windows_cs_physical_memory_bytes * 100))[1h:1m])
```

**5. Lowest point of free memory in the last 24 hours:**
```promql
min_over_time(windows_memory_available_bytes[24h])
```

**6. Compare memory usage now vs. this time yesterday:**
```promql
(100 - (windows_memory_available_bytes / windows_cs_physical_memory_bytes * 100))
-
(100 - (windows_memory_available_bytes offset 1d / windows_cs_physical_memory_bytes offset 1d * 100))
```

**7. Top 5 machines by memory usage right now:**
```promql
topk(5, 100 - (windows_memory_available_bytes / windows_cs_physical_memory_bytes * 100))
```

**8. Alert-style — machines with less than 10% memory free:**
```promql
(windows_memory_available_bytes / windows_cs_physical_memory_bytes * 100) < 10
```

**9. Trend — how fast is available memory shrinking?:**
```promql
deriv(windows_memory_available_bytes[30m])
```

**10. Forecast — when will available memory hit zero?:**
```promql
predict_linear(windows_memory_available_bytes[1h], 4*3600) < 0
```

**11. Fleet-wide total memory used, across all machines:**
```promql
sum(windows_cs_physical_memory_bytes) - sum(windows_memory_available_bytes)
```

**Gotchas:** `windows_cs_physical_memory_bytes` is effectively constant per machine — never chart it alone as a "trend." Since these are Gauges, `deriv()`/`predict_linear()` apply directly — unlike the CPU skill, no `rate()` wrapper needed.

**Related Metrics:** `windows_memory_cache_bytes` (reclaimable disk cache, distinct from true pressure).

---

## 4. Skill: Process CPU Usage

### Routing Metadata

**Purpose:** Answers questions about CPU usage of one specific application/process/service — not the whole machine.

**Trigger Examples:**
- "How much CPU is Prometheus using"
- "Which service is eating CPU"
- "Is this process using more than one core"
- "Is the Prometheus process CPU-bound"

**Do Not Use:**
- Whole-machine CPU → *CPU Usage (Infra)*
- Memory questions → *Process Memory Usage*

**Known Information:**
- Metric: `process_cpu_seconds_total`
- Type: Counter
- Data Source: Prometheus (client-library self-metrics — available for any instrumented service; Prometheus itself exposes this under `job="prometheus"`)
- Unit: % (can legitimately exceed 100 for a multi-threaded process using more than one core)
- Labels: `job`, `instance`

**Expected Behaviour:** Confirm this is about one process, not the whole machine. Remember this is a Counter (needs `rate()`). Find the closest cookbook match, adapt it — especially the `job` label, which will differ per environment. If the result exceeds 100%, explain that this is expected for multi-core usage rather than treating it as an error.

### Technical Cookbook

**1. Current CPU usage % of the process:**
```promql
rate(process_cpu_seconds_total{job="prometheus"}[5m]) * 100
```

**2. CPU usage trend over the last hour:**
```promql
avg_over_time((rate(process_cpu_seconds_total{job="prometheus"}[5m]) * 100)[1h:1m])
```

**3. Alert-style — process sustained using more than one full core:**
```promql
rate(process_cpu_seconds_total{job="prometheus"}[5m]) > 1
```

**4. Top 5 jobs by CPU usage, across multiple instrumented services:**
```promql
topk(5, rate(process_cpu_seconds_total[5m]))
```

**5. Raw total CPU-seconds consumed since the process started:**
```promql
process_cpu_seconds_total{job="prometheus"}
```

**Gotchas:** Exceeding 100% is the most common false "bug report" for this metric — it's correct for multi-threaded processes. Confirm the actual `job` label present in your environment rather than assuming `"prometheus"` applies elsewhere.

**Related Metrics:** `process_open_fds`, `process_virtual_memory_bytes`.

---

## 5. Skill: Process Memory Usage

### Routing Metadata

**Purpose:** Answers questions about memory usage of one specific application/process/service — not the whole machine.

**Trigger Examples:**
- "How much memory is Prometheus using"
- "Is there a memory leak in this service"
- "Will this process run out of memory"
- "Has memory usage grown since this morning"

**Do Not Use:**
- Whole-machine memory → *Memory Usage (Infra)*
- CPU questions → *Process CPU Usage*

**Known Information:**
- Metric: `process_resident_memory_bytes`
- Type: Gauge
- Data Source: Prometheus (client-library self-metrics; Prometheus itself exposes this under `job="prometheus"`)
- Unit: bytes → MB
- Labels: `job`, `instance`

**Expected Behaviour:** Confirm this is about one process's memory, not the whole machine or its CPU. Since this is a Gauge, `deriv()`/`predict_linear()` apply directly. Find the closest cookbook match, adapt the `job` label and time window, and flag a sustained positive `deriv()` as a possible leak rather than just reporting the number.

### Technical Cookbook

**1. Current resident memory, in MB:**
```promql
process_resident_memory_bytes{job="prometheus"} / 1048576
```

**2. Memory growth rate (bytes/sec) — the standard leak-detection query:**
```promql
deriv(process_resident_memory_bytes{job="prometheus"}[30m])
```

**3. Compare memory usage now vs. one hour ago:**
```promql
process_resident_memory_bytes{job="prometheus"} - (process_resident_memory_bytes{job="prometheus"} offset 1h)
```

**4. Forecast — will memory exceed 1 GB within the next 2 hours?:**
```promql
predict_linear(process_resident_memory_bytes{job="prometheus"}[1h], 2*3600) > 1073741824
```

**Gotchas:** A steadily positive `deriv()` over a long window (hours) is the standard signal for a leak — a positive `deriv()` over a short window is often just normal short-term fluctuation, not a leak.

**Related Metrics:** `process_virtual_memory_bytes`.

---

## 6. Cross-Skill Guidance

- Infra skills (CPU/Memory) answer *"is the machine under pressure?"* Process skills answer *"which specific process is causing it?"* A thorough "why is the server slow" answer often checks infra first, then drills into the relevant process skill if infra shows pressure.
- All four skills share the exact same *shape* of advanced capability (smoothing, comparison, top-N, threshold, forecast) because they all draw on the same Section 1 Fundamentals — a future 5th skill should follow this same two-layer structure for consistency.
## 0. Purpose and How To Use This Document

This file is a **one-stop reference for constructing PromQL queries** — not a fixed list of the only questions this agent can answer. Each skill below has two layers:

1. **Routing metadata** (Purpose / Trigger Examples / Do Not Use / Known Information / Expected Behaviour) — decides **whether this skill applies** to the user's question at all.
2. **Technical cookbook** (metric type, labels, example queries, gotchas) — decides **how to build the actual query**, once routing has already picked this skill.

**Important scope note:** the cookbook examples in each skill are *teaching examples*, not an exhaustive whitelist. Combined with Section 1 (Fundamentals), the agent should construct queries for questions no single example covers, by recombining fundamentals and cookbook patterns. Whether this generalization actually works reliably on novel combinations is what Phase 2 verification tests — this document states the intent, it does not by itself guarantee the outcome.

### 0.1 Skill Directory

| Skill | Topic | Use when the question is about... |
|---|---|---|
| CPU Usage (Infra) | Machine-level CPU | A whole server/machine's CPU |
| Memory Usage (Infra) | Machine-level RAM | A whole server/machine's memory |
| Process CPU Usage | App/service CPU | One specific process or service's CPU |
| Process Memory Usage | App/service RAM | One specific process or service's memory |

### 0.2 Construction Procedure (applies to every skill below)

1. Match the question to a skill using its Purpose/Trigger Examples — and check it isn't excluded by that skill's Do Not Use list.
2. Check the metric's **type** (Counter vs. Gauge, in Known Information) — this determines which functions are even valid (Section 1.1).
3. Find the cookbook example structurally closest to the question.
4. Adapt it — swap in the real instance/label/time window/threshold from the question. Never return an unmodified example if the specifics differ.
5. Combine multiple cookbook patterns if the question asks for more than one capability at once (e.g., "top 3 *and* compare to last week").
6. Sanity-check the unit and rough magnitude of the result against Known Information before presenting it.

---

## 1. PromQL Fundamentals (apply to all skills below)

### 1.1 Metric Types — and why the type changes everything

| Type | What it means | How you're allowed to query it |
|---|---|---|
| **Counter** | A value that only ever goes up (resets to 0 only on restart). Represents a *total accumulated so far*. | **Never use the raw value directly across time.** Always wrap in `rate()`, `irate()`, or `increase()` first. |
| **Gauge** | A value that can go up or down freely — a *snapshot right now*. | Can be used directly, or with `avg_over_time()`, `max_over_time()`, `deriv()`, `predict_linear()`, etc. |

### 1.2 Instant Vector vs. Range Vector

- `metric_name{label="x"}` → an **instant vector**: one value per series, at one point in time.
- `metric_name{label="x"}[5m]` → a **range vector**: every sample in the last 5 minutes, per series — must be fed into a function like `rate()` or `avg_over_time()` to reduce back to a usable value.

### 1.3 Core Functions Reference

| Function | Use on | What it does |
|---|---|---|
| `rate(x[5m])` | Counter | Average per-second rate of increase — the standard way to turn a Counter into a usable "speed" |
| `irate(x[5m])` | Counter | Same idea, using only the last two points — more reactive, noisier |
| `increase(x[5m])` | Counter | Total increase over the window |
| `avg_over_time(x[1h])` | Gauge / computed expr | Smoothed average over the window |
| `max_over_time(x[1h])` / `min_over_time(x[1h])` | Gauge | Worst/best point during the window |
| `deriv(x[30m])` | Gauge | Per-second rate of change (slope) |
| `predict_linear(x[1h], 3600)` | Gauge | Projects the current trend forward N seconds — used for forecasting |
| `quantile_over_time(0.95, x[1h])` | Gauge | 95th-percentile value seen during the window |

### 1.4 Aggregation Operators

| Operator | What it does |
|---|---|
| `sum by (label) (...)` / `avg by (label) (...)` | Combine values, grouped by a label |
| `max by (label) (...)` / `min by (label) (...)` | Highest/lowest value per group |
| `topk(N, ...)` / `bottomk(N, ...)` | The N highest/lowest series |
| `count by (label) (...)` | How many series exist per group |

### 1.5 Time and Vector-Matching Modifiers

- **`offset 1d`** — shifts the query back in time, for "now vs. yesterday" comparisons: `rate(metric[5m] offset 1d)`.
- **Subquery `[1h:1m]`** — required to run `avg_over_time`/`max_over_time`/`deriv` over an *already-computed expression* (like a percentage formula), not a bare metric.
- **`on()` / `ignoring()` / `group_left` / `group_right`** — used when combining two different metrics in one expression, to control which labels must match.

### 1.6 Global Gotchas

- **Counter resets** on restart are handled correctly by `rate()`/`increase()` — raw subtraction across time is not, and can silently go negative.
- **Rate window sizing:** should be at least 4× the scrape interval, or results get noisy/undefined.
- **Staleness:** a dead target's series goes stale after 5 minutes by default — expect empty results, not zeroes.
- **Extrapolation at edges:** `rate()`/`increase()` estimate slightly beyond the window's exact boundary samples.

---

## 2. Skill: CPU Usage (Infra)

### Routing Metadata

**Purpose:** Answers questions about machine-level CPU usage, utilization, or load across one or more monitored servers.

**Trigger Examples:**
- "Show CPU usage"
- "What's the CPU load on HOST-01"
- "Is CPU usage climbing"
- "Which machine has the highest CPU right now"
- "Was there a CPU spike last night"
- "Will CPU hit 100% soon"

**Do Not Use:**
- Memory-related questions → *Memory Usage (Infra)*
- A specific application/process's CPU (not the whole machine) → *Process CPU Usage*
- Disk, network, or log queries

**Known Information:**
- Metric: `windows_cpu_time_total`
- Type: Counter
- Data Source: Prometheus (`windows_exporter`)
- Unit: % (derived — see cookbook)
- Labels: `instance`, `core`, `mode`

**Expected Behaviour:** Follow the Construction Procedure in Section 0.2 — confirm this skill (not Memory or Process CPU) fits, confirm this is a Counter (needs `rate()`), find the closest cookbook match below, adapt it to the real instance/window/threshold asked, and sanity-check the result lands in a valid 0–100% range.

### Technical Cookbook

**Labels:**
| Label | Meaning | Example values |
|---|---|---|
| `instance` | Which machine reported this | `HOST-01:9182` |
| `core` | Which logical CPU core | `0`, `1`, `2`... |
| `mode` | What the core was doing | `idle`, `user`, `privileged`, `interrupt`, `dpc` |

**1. Current CPU usage %, averaged across all cores, per instance:**
```promql
100 - (avg by (instance) (rate(windows_cpu_time_total{mode="idle"}[5m])) * 100)
```

**2. CPU usage % per individual core:**
```promql
100 - (rate(windows_cpu_time_total{mode="idle"}[5m]) * 100)
```

**3. CPU usage % for one specific machine:**
```promql
100 - (avg by (instance) (rate(windows_cpu_time_total{mode="idle", instance="HOST-01:9182"}[5m])) * 100)
```

**4. Smoothed average CPU usage over the last hour:**
```promql
avg_over_time((100 - (avg by (instance) (rate(windows_cpu_time_total{mode="idle"}[5m])) * 100))[1h:1m])
```

**5. Worst CPU spike in the last 24 hours:**
```promql
max_over_time((100 - (avg by (instance) (rate(windows_cpu_time_total{mode="idle"}[5m])) * 100))[24h:5m])
```

**6. Compare CPU usage now vs. this time yesterday:**
```promql
(100 - (avg by (instance) (rate(windows_cpu_time_total{mode="idle"}[5m])) * 100))
-
(100 - (avg by (instance) (rate(windows_cpu_time_total{mode="idle"}[5m] offset 1d)) * 100))
```

**7. Top 5 machines by CPU usage right now:**
```promql
topk(5, 100 - (avg by (instance) (rate(windows_cpu_time_total{mode="idle"}[5m])) * 100))
```

**8. Alert-style — machines sustained above 90% CPU:**
```promql
(100 - (avg by (instance) (rate(windows_cpu_time_total{mode="idle"}[5m])) * 100)) > 90
```

**9. Trend direction — climbing or falling?**
```promql
deriv((100 - (avg by (instance) (rate(windows_cpu_time_total{mode="idle"}[5m])) * 100))[30m:1m])
```

**10. Forecast — will CPU breach 95% within the next hour?**
```promql
predict_linear((100 - (avg by (instance) (rate(windows_cpu_time_total{mode="idle"}[5m])) * 100))[30m:1m], 3600) > 95
```

**11. Breakdown — what is the CPU actually doing?**
```promql
sum by (mode) (rate(windows_cpu_time_total{instance="HOST-01:9182"}[5m])) * 100
```

**Gotchas:** Always filter/subtract `mode="idle"` — averaging across all modes without this is meaningless. Per-core values (query #2) can look noisy at short windows — this often correctly reflects real scheduling, not a bug.

**Related Metrics:** `windows_system_processor_queue_length` (thread contention signal).

---

## 3. Skill: Memory Usage (Infra)

### Routing Metadata

**Purpose:** Answers questions about machine-level RAM usage or availability across monitored servers.

**Trigger Examples:**
- "How's memory looking"
- "How much RAM is free on HOST-01"
- "Is any server running low on memory"
- "Will we run out of memory soon"
- "Compare memory usage to yesterday"

**Do Not Use:**
- CPU questions → *CPU Usage (Infra)*
- A specific application/process's memory → *Process Memory Usage*
- Disk space questions

**Known Information:**
- Metrics: `windows_memory_available_bytes` (free RAM), `windows_cs_physical_memory_bytes` (total RAM, denominator only)
- Type: Both Gauges
- Data Source: Prometheus (`windows_exporter`)
- Unit: bytes → GB or %
- Labels: `instance`

**Expected Behaviour:** Confirm this skill fits (not CPU or Process Memory), remember both metrics are Gauges (`deriv`/`predict_linear` apply directly, no `rate()` needed), find the closest cookbook match, adapt it, and sanity-check the % falls between 0–100 or the GB figure is plausible for the machine.

### Technical Cookbook

**Labels:** `instance` — which machine reported this.

**1. Current memory usage %:**
```promql
100 - (windows_memory_available_bytes / windows_cs_physical_memory_bytes * 100)
```

**2. Current free memory, in GB:**
```promql
windows_memory_available_bytes / 1073741824
```

**3. Memory usage % for one specific machine:**
```promql
100 - (windows_memory_available_bytes{instance="HOST-01:9182"} / windows_cs_physical_memory_bytes{instance="HOST-01:9182"} * 100)
```

**4. Smoothed average memory usage over the last hour:**
```promql
avg_over_time((100 - (windows_memory_available_bytes / windows_cs_physical_memory_bytes * 100))[1h:1m])
```

**5. Lowest point of free memory in the last 24 hours:**
```promql
min_over_time(windows_memory_available_bytes[24h])
```

**6. Compare memory usage now vs. this time yesterday:**
```promql
(100 - (windows_memory_available_bytes / windows_cs_physical_memory_bytes * 100))
-
(100 - (windows_memory_available_bytes offset 1d / windows_cs_physical_memory_bytes offset 1d * 100))
```

**7. Top 5 machines by memory usage right now:**
```promql
topk(5, 100 - (windows_memory_available_bytes / windows_cs_physical_memory_bytes * 100))
```

**8. Alert-style — machines with less than 10% memory free:**
```promql
(windows_memory_available_bytes / windows_cs_physical_memory_bytes * 100) < 10
```

**9. Trend — how fast is available memory shrinking?**
```promql
deriv(windows_memory_available_bytes[30m])
```

**10. Forecast — when will available memory hit zero?**
```promql
predict_linear(windows_memory_available_bytes[1h], 4*3600) < 0
```

**11. Fleet-wide total memory used, across all machines:**
```promql
sum(windows_cs_physical_memory_bytes) - sum(windows_memory_available_bytes)
```

**Gotchas:** `windows_cs_physical_memory_bytes` is effectively constant per machine — never chart it alone as a "trend." Since these are Gauges, `deriv()`/`predict_linear()` apply directly — unlike the CPU skill, no `rate()` wrapper needed.

**Related Metrics:** `windows_memory_cache_bytes` (reclaimable disk cache, distinct from true pressure).

---

## 4. Skill: Process CPU Usage

### Routing Metadata

**Purpose:** Answers questions about CPU usage of one specific application/process/service — not the whole machine.

**Trigger Examples:**
- "How much CPU is Prometheus using"
- "Which service is eating CPU"
- "Is this process using more than one core"
- "Is the Prometheus process CPU-bound"

**Do Not Use:**
- Whole-machine CPU → *CPU Usage (Infra)*
- Memory questions → *Process Memory Usage*

**Known Information:**
- Metric: `process_cpu_seconds_total`
- Type: Counter
- Data Source: Prometheus (client-library self-metrics — available for any instrumented service; Prometheus itself exposes this under `job="prometheus"`)
- Unit: % (can legitimately exceed 100 for a multi-threaded process using more than one core)
- Labels: `job`, `instance`

**Expected Behaviour:** Confirm this is about one process, not the whole machine. Remember this is a Counter (needs `rate()`). Find the closest cookbook match, adapt it — especially the `job` label, which will differ per environment. If the result exceeds 100%, explain that this is expected for multi-core usage rather than treating it as an error.

### Technical Cookbook

**1. Current CPU usage % of the process:**
```promql
rate(process_cpu_seconds_total{job="prometheus"}[5m]) * 100
```

**2. CPU usage trend over the last hour:**
```promql
avg_over_time((rate(process_cpu_seconds_total{job="prometheus"}[5m]) * 100)[1h:1m])
```

**3. Alert-style — process sustained using more than one full core:**
```promql
rate(process_cpu_seconds_total{job="prometheus"}[5m]) > 1
```

**4. Top 5 jobs by CPU usage, across multiple instrumented services:**
```promql
topk(5, rate(process_cpu_seconds_total[5m]))
```

**5. Raw total CPU-seconds consumed since the process started:**
```promql
process_cpu_seconds_total{job="prometheus"}
```

**Gotchas:** Exceeding 100% is the most common false "bug report" for this metric — it's correct for multi-threaded processes. Confirm the actual `job` label present in your environment rather than assuming `"prometheus"` applies elsewhere.

**Related Metrics:** `process_open_fds`, `process_virtual_memory_bytes`.

---

## 5. Skill: Process Memory Usage

### Routing Metadata

**Purpose:** Answers questions about memory usage of one specific application/process/service — not the whole machine.

**Trigger Examples:**
- "How much memory is Prometheus using"
- "Is there a memory leak in this service"
- "Will this process run out of memory"
- "Has memory usage grown since this morning"

**Do Not Use:**
- Whole-machine memory → *Memory Usage (Infra)*
- CPU questions → *Process CPU Usage*

**Known Information:**
- Metric: `process_resident_memory_bytes`
- Type: Gauge
- Data Source: Prometheus (client-library self-metrics; Prometheus itself exposes this under `job="prometheus"`)
- Unit: bytes → MB
- Labels: `job`, `instance`

**Expected Behaviour:** Confirm this is about one process's memory, not the whole machine or its CPU. Since this is a Gauge, `deriv()`/`predict_linear()` apply directly. Find the closest cookbook match, adapt the `job` label and time window, and flag a sustained positive `deriv()` as a possible leak rather than just reporting the number.

### Technical Cookbook

**1. Current resident memory, in MB:**
```promql
process_resident_memory_bytes{job="prometheus"} / 1048576
```

**2. Memory growth rate (bytes/sec) — the standard leak-detection query:**
```promql
deriv(process_resident_memory_bytes{job="prometheus"}[30m])
```

**3. Compare memory usage now vs. one hour ago:**
```promql
process_resident_memory_bytes{job="prometheus"} - (process_resident_memory_bytes{job="prometheus"} offset 1h)
```

**4. Forecast — will memory exceed 1 GB within the next 2 hours?**
```promql
predict_linear(process_resident_memory_bytes{job="prometheus"}[1h], 2*3600) > 1073741824
```

**Gotchas:** A steadily positive `deriv()` over a long window (hours) is the standard signal for a leak — a positive `deriv()` over a short window is often just normal short-term fluctuation, not a leak.

**Related Metrics:** `process_virtual_memory_bytes`.

---

## 6. Cross-Skill Guidance

- Infra skills (CPU/Memory) answer *"is the machine under pressure?"* Process skills answer *"which specific process is causing it?"* A thorough "why is the server slow" answer often checks infra first, then drills into the relevant process skill if infra shows pressure.
- All four skills share the exact same *shape* of advanced capability (smoothing, comparison, top-N, threshold, forecast) because they all draw on the same Section 1 Fundamentals — a future 5th skill should follow this same two-layer structure for consistency.