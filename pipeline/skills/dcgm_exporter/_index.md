---
name: dcgm_exporter
purpose: GPU observability provided by the NVIDIA DCGM Exporter.
data_source: prometheus
version: 1.0

trigger_keywords:
  - GPU utilization
  - GPU temperature
  - GPU compute activity
  - GPU power
  - GPU clocks
  - GPU throttling
  - Tensor Core utilization
  - FP32 utilization
  - FP64 utilization
  - FP16 utilization
  - FP32 pipeline
  - FP64 pipeline
  - FP16 pipeline
  - graphics engine activity
  - GPU engine activity
  - SM activity

domains:
  - id: compute
    file: compute.md
    covers: GPU utilization and compute-pipeline activity
  - id: thermal
    file: thermal.md
    covers: GPU temperature, power, and clock operating state
---

# DCGM Exporter — Index

## 1. File-Level Routing

### Purpose

Define the GPU observability domain covered by the DCGM Exporter and route requests to the appropriate functional domain.

This exporter currently covers the supported DCGM metrics implemented in the `compute` and `thermal` domain files.

It does **not** contain detailed metric definitions.

---

### Trigger Examples

Examples of user requests that should route to this exporter:

- "What is the GPU utilization?"
- "How busy is GPU 2?"
- "How active are the GPU compute engines?"
- "How much tensor activity is there?"
- "What is the GPU temperature?"
- "Is the GPU running too hot?"
- "What is the GPU power consumption?"
- "What is the current GPU clock?"
- "Is the GPU being power throttled?"

These examples illustrate intent and are not an exhaustive whitelist.

---

### Do Not Use

Do not use this exporter for:

- Host CPU, host memory, load, swap, filesystem, or other Node Exporter measurements → `node_exporter`
- DCGM metrics that are not currently defined in this sub-skill implementation.
- Measurements for which no supported DCGM metric or derived/composed measurement is defined.

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
   - GPU
   - device
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

The Metric Directory is exhaustive for the metrics currently supported by this DCGM Exporter sub-skill implementation.

Every supported raw metric must appear here.

A metric must not exist only inside a domain file without a corresponding Metric Directory entry.

The detailed metric definitions inside each domain file remain authoritative for final metric selection.

| Domain | Intent / Measurement | Metric | Detail File |
|---|---|---|---|
| compute | Overall GPU utilization | `DCGM_FI_DEV_GPU_UTIL` | `compute.md` |
| compute | Graphics/SM engine active time | `DCGM_FI_PROF_GR_ENGINE_ACTIVE` | `compute.md` |
| compute | Tensor Core activity | `DCGM_FI_PROF_PIPE_TENSOR_ACTIVE` | `compute.md` |
| compute | FP64 pipeline utilization | `DCGM_FI_PROF_PIPE_FP64_ACTIVE` | `compute.md` |
| compute | FP32 pipeline utilization | `DCGM_FI_PROF_PIPE_FP32_ACTIVE` | `compute.md` |
| compute | FP16 pipeline utilization | `DCGM_FI_PROF_PIPE_FP16_ACTIVE` | `compute.md` |
| thermal | GPU core temperature | `DCGM_FI_DEV_GPU_TEMP` | `thermal.md` |
| thermal | GPU memory/HBM temperature | `DCGM_FI_DEV_MEMORY_TEMP` | `thermal.md` |
| thermal | Instantaneous GPU power consumption | `DCGM_FI_DEV_POWER_USAGE` | `thermal.md` |
| thermal | Time spent power throttled | `DCGM_FI_DEV_POWER_VIOLATION` | `thermal.md` |
| thermal | Current GPU SM/core clock | `DCGM_FI_DEV_SM_CLOCK` | `thermal.md` |
| thermal | Current GPU memory clock | `DCGM_FI_DEV_MEM_CLOCK` | `thermal.md` |

The metric names, Prometheus types, measurements, and documented query intents come from the provided DCGM metric reference. 

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

The primary entity represented by the DCGM Exporter metrics is the **GPU**.

DCGM metrics may represent GPUs across multiple nodes.

Verified exporter-wide labels shared by the currently supported metrics:

- `cluster`
- `device`
- `gpu`
- `instance`
- `job`
- `node_id`

Preserve explicitly specified node, GPU, or device scope when provided by the user.

Domain-specific additions belong only inside the corresponding Domain Fundamentals.

---

### 5.2 Metric Ambiguity vs Parameter Vagueness

Request clarification only when the **requested measurement itself cannot be determined**.

Do **not** classify a request as metric-ambiguous merely because optional query parameters were omitted.

Examples of omitted parameters include:

- time range;
- node;
- GPU;
- device;
- aggregation scope.

If the requested measurement is already clear, defer parameter defaults and query construction to the Main Skill.

---

### 5.3 Cross-Domain Semantic Distinctions

Populate this section only when a semantic distinction genuinely applies across multiple functional domains.

The current DCGM implementation does not require any additional cross-domain semantic distinctions beyond the general metric-selection rules already defined above.

> No cross-domain semantic distinctions are currently defined for this exporter.

---

## 6. Guardrails

- Only route to metrics explicitly defined by this exporter.
- Only use derived/composed measurements explicitly defined by this exporter.
- Treat the project metric reference as authoritative.
- Do not fabricate metric names, labels, units, dimensions, relationships, or metric semantics.
- If required information has not been verified against the datasource/schema, mark it as requiring verification rather than guessing.
- Do not assume node, GPU, device, or other scope constraints that the user did not specify.
- Preserve all explicitly requested measurements.
- Multiple plausible metrics do **not** imply the user requested multiple metrics.
- A derived measurement requiring multiple source metrics is distinct from a request for multiple independent measurements.
- If multiple materially different measurements remain plausible, request clarification.
- Treat the Metric Directory as a routing aid only. Final metric selection must always be verified using the detailed metric definition in the relevant domain file.
- Cross-domain semantic concepts belong in this file. Domain-specific semantic concepts belong in the corresponding domain file.
- Defer datasource syntax, query construction, parameter defaults, output formatting, and generic error handling to the Main Skill and Prometheus Fundamentals.
- Do not invent Prometheus label names. Use only labels verified from the actual datasource/schema.