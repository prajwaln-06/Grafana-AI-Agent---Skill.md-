---
name: node_exporter
purpose: Host-level system observability provided by Node Exporter.
data_source: prometheus
version: 1.0

trigger_keywords:
  - CPU utilization
  - CPU activity
  - context switches
  - interrupts
  - memory usage
  - available memory
  - free memory
  - cached memory
  - buffer memory
  - swap usage
  - swap space

domains:
  - id: cpu
    file: cpu.md
    covers: CPU utilization and CPU scheduling activity

  - id: memory
    file: memory.md
    covers: Physical memory, cache, buffers, and swap usage
---

# Node Exporter — Index

## 1. File-Level Routing

### Purpose

Define the host-level system observability domain covered by Node Exporter and route requests to the appropriate functional domain.

This exporter currently covers the supported Node Exporter metrics implemented in the `cpu` and `memory` domain files.

It does **not** contain detailed metric definitions.

---

### Trigger Examples

Examples of user requests that should route to this exporter:

- "What is the CPU utilization?"
- "How busy is the CPU?"
- "How much CPU time is idle?"
- "Are there a lot of context switches?"
- "How many interrupts are being handled?"
- "How much memory is available?"
- "How much free RAM is there?"
- "How much memory is being used for cache?"
- "How much memory is being used for buffers?"
- "How much swap space is available?"
- "How much total swap space does the system have?"

These examples illustrate intent and are not an exhaustive whitelist.

---

### Do Not Use

Do not use this exporter for:

- GPU utilization, GPU temperature, GPU power, GPU clocks, or other DCGM measurements → `dcgm_exporter`
- Load-average measurements that are not currently defined in this sub-skill implementation.
- Filesystem measurements that are not currently defined in this sub-skill implementation.
- Node Exporter metrics that are not currently defined in this sub-skill implementation.
- Measurements for which no supported Node Exporter metric or derived/composed measurement is defined.

---

## 2. Metric Selection Procedure

After the Main Skill routes a request to this exporter:

1. Identify **all measurements explicitly requested by the user** before selecting a metric or metric composition.

2. Use the **Metric Directory** to identify the relevant functional domain and candidate metric(s).

3. Verify each candidate using its detailed definition in the relevant **domain file**, including:

   - Purpose
   - Use When
   - Do Not Use / Confusable With
   - Intent Examples

4. Check whether the requested measurement is:

   - directly represented by a single metric;
   - explicitly requesting multiple independent measurements; or
   - a derived/composed measurement requiring multiple source metrics.

5. Preserve all constraints explicitly provided by the user, such as:

   - node
   - CPU
   - time range
   - comparison scope

6. Do not invent scope constraints that the user did not provide.

7. Select multiple independent metrics **only when the user explicitly requests multiple distinct measurements**.

8. A single requested measurement may legitimately require multiple source metrics when it is defined as a **derived/composed measurement** in this exporter. This is **not** the same as the user requesting multiple independent measurements.

9. Do not convert a vague or underspecified request into a multi-metric request merely to provide a more comprehensive answer.

10. If multiple materially different measurements could plausibly satisfy the request and the user has not indicated which one(s) they want, classify the request as **AMBIGUOUS** and request clarification.

Ambiguity must not be resolved by:

- arbitrarily choosing one plausible metric;
- selecting all plausible metrics;
- assuming the user wants a comprehensive overview.

11. If no metric or derived/composed measurement defined in this exporter represents the requested measurement, classify the request as **UNSUPPORTED**.

Do not invent:

- metric names;
- labels;
- measurements;
- relationships between metrics;
- query expressions.

12. Once the metric(s), metric-specific semantics, and relevant scope are resolved, defer shared query construction, datasource syntax, time handling, aggregation, output formatting, and generic error handling to the Main Skill.

---

## 3. Metric Directory

Use this directory for initial metric discovery.

The Metric Directory is exhaustive for the metrics currently supported by this Node Exporter sub-skill implementation.

Every supported raw metric must appear here.

A metric must not exist only inside a domain file without a corresponding Metric Directory entry.

The detailed metric definitions inside each domain file remain authoritative for final metric selection.

| Domain | Intent / Measurement | Metric | Detail File |
|---|---|---|---|
| cpu | CPU time spent in different modes | `node_cpu_seconds_total` | `cpu.md` |
| cpu | Total context switches | `node_context_switches_total` | `cpu.md` |
| cpu | Total interrupts handled | `node_intr_total` | `cpu.md` |
| memory | Total physical memory | `node_memory_MemTotal_bytes` | `memory.md` |
| memory | Memory available without swapping | `node_memory_MemAvailable_bytes` | `memory.md` |
| memory | Completely free memory | `node_memory_MemFree_bytes` | `memory.md` |
| memory | Linux page cache | `node_memory_Cached_bytes` | `memory.md` |
| memory | Filesystem buffers | `node_memory_Buffers_bytes` | `memory.md` |
| memory | Total swap space | `node_memory_SwapTotal_bytes` | `memory.md` |
| memory | Available swap space | `node_memory_SwapFree_bytes` | `memory.md` |

The metric names, Prometheus types, measurements, and documented query intents come from the provided Node Exporter metric reference.

---

## 4. Derived / Composed Measurements

> No derived/composed measurements are currently defined for this exporter.

---

## 5. Exporter Fundamentals

This section contains concepts that are true across the **entire exporter**, or across **two or more functional domains**.

If a concept applies only to one functional domain, it belongs inside that domain's **Domain Fundamentals** instead.

Do not duplicate datasource syntax, PromQL/OpenSearch language rules, aggregation behaviour, Counter/Gauge handling, or other shared query-language concepts from the datasource fundamentals.

---

### 5.1 Entity Scope Baseline

The primary entity represented by the currently supported Node Exporter metrics is the **node**.

Verified exporter-wide labels shared by the currently supported metrics:

- `cluster`
- `instance`
- `job`
- `node_id`

These labels are common across the currently supported Node Exporter metrics.

Preserve explicitly specified node or CPU scope when provided by the user.

Domain-specific additions belong only inside the corresponding Domain Fundamentals.

---

### 5.2 Metric Ambiguity vs Parameter Vagueness

Request clarification only when the **requested measurement itself cannot be determined**.

Do **not** classify a request as metric-ambiguous merely because optional query parameters were omitted.

Examples of omitted parameters include:

- time range;
- node;
- CPU;
- aggregation scope.

If the requested measurement is already clear, defer parameter defaults and query construction to the Main Skill.

---

### 5.3 Cross-Domain Semantic Distinctions

Populate this section only when a semantic distinction genuinely applies across multiple functional domains.

The current Node Exporter implementation does not require any additional cross-domain semantic distinctions beyond the general metric-selection rules already defined above.

> No cross-domain semantic distinctions are currently defined for this exporter.

---

## 6. Guardrails

- Only route to metrics explicitly defined by this exporter.
- Only use derived/composed measurements explicitly defined by this exporter.
- Treat the project metric reference as authoritative.
- Do not fabricate metric names, labels, units, dimensions, relationships, or metric semantics.
- If required information has not been verified against the datasource/schema, mark it as requiring verification rather than guessing.
- Do not assume node, CPU, or other scope constraints that the user did not specify.
- Preserve all explicitly requested measurements.
- Multiple plausible metrics do **not** imply the user requested multiple metrics.
- A derived measurement requiring multiple source metrics is distinct from a request for multiple independent measurements.
- If multiple materially different measurements remain plausible, request clarification.
- Treat the Metric Directory as a routing aid only. Final metric selection must always be verified using the detailed metric definition in the relevant domain file.
- Cross-domain semantic concepts belong in this file. Domain-specific semantic concepts belong in the corresponding domain file.
- Defer datasource syntax, query construction, parameter defaults, output formatting, and generic error handling to the Main Skill and Prometheus Fundamentals.
- Do not invent Prometheus label names. Use only labels verified from the actual datasource/schema.