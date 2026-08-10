---
name: dcgm_exporter
purpose: GPU observability provided by the DCGM Exporter.
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
  - pipeline utilization
  - compute pipeline
  - precision pipeline
  - FP32
  - FP64
  - FP16

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
- "What is the FP32 pipeline utilization?"
- "How much FP64 or FP16 workload is running?"
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

5. Classify a measurement as derived/composed only when its derivation is explicitly established in the relevant reference information. Treat a raw metric according to its documented measurement semantics.

6. Preserve all constraints explicitly provided by the user, such as:

   * node
   * GPU
   * device
   * time range
   * comparison scope

7. Apply only scope constraints explicitly provided by the user or established by the available reference information.

8. Select multiple independent metrics **only when the user explicitly requests multiple distinct measurements**.

9. A single requested measurement may legitimately require multiple source metrics when it is defined as a **derived/composed measurement** in this exporter. This is **not** the same as the user requesting multiple independent measurements.

10. Keep a vague or underspecified request limited to the measurement that can be established from the user's wording and available reference information. Treat additional measurements as separate requests only when the user explicitly requests them.

11. If multiple materially different measurements could plausibly satisfy the request and the user has not indicated which one(s) they want, classify the request as **AMBIGUOUS** and request clarification.

Resolve ambiguity by requesting clarification when:

* multiple materially different measurements remain plausible;
* the user's wording does not establish which measurement they want.

Do not resolve ambiguity by:

* arbitrarily choosing one plausible metric;
* selecting all plausible metrics;
* assuming the user wants a comprehensive overview.

12. If no metric or derived/composed measurement defined in this exporter represents the requested measurement, classify the request as **UNSUPPORTED**.

Use only:

* metric names defined by this exporter;
* labels verified in the available datasource/schema or reference information;
* measurements established by the available skill/reference information;
* metric relationships established by the available skill/reference information;
* query expressions supported by the applicable query-construction rules.

13. Once the metric(s), metric-specific semantics, and relevant scope are resolved, defer shared query construction, datasource syntax, time handling, aggregation, output formatting, and generic error handling to the Main Skill.

---

## 3. Metric Directory

Use this directory for initial metric discovery.

The Metric Directory is exhaustive for the metrics currently supported by this DCGM Exporter sub-skill implementation.

Every supported raw metric must appear here.

Every supported metric must have a corresponding Metric Directory entry in addition to its detailed definition in the relevant domain file.

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

Keep datasource syntax, PromQL/OpenSearch language rules, aggregation behaviour, Counter/Gauge handling, and other shared query-language concepts in the appropriate datasource fundamentals. Reference those fundamentals rather than duplicating them here.

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

Request clarification when the **requested measurement itself cannot be determined**.

Treat omitted optional query parameters as parameter vagueness rather than metric ambiguity.

Examples of omitted parameters include:

* time range;
* node;
* GPU;
* device;
* aggregation scope.

If the requested measurement is already clear, defer parameter defaults and query construction to the Main Skill.

---

### 5.3 Cross-Domain Semantic Distinctions

Populate this section only when a semantic distinction genuinely applies across multiple functional domains.

The current DCGM implementation does not require any additional cross-domain semantic distinctions beyond the general metric-selection rules already defined above.

> No cross-domain semantic distinctions are currently defined for this exporter.

---

## 6. Guardrails

* Route requests only to metrics explicitly defined by this exporter.
* Use derived/composed measurements only when they are explicitly defined by this exporter.
* Treat the project metric reference as authoritative for metric names, measurements, semantics, and documented relationships.
* Use only metric names, labels, units, dimensions, relationships, and metric semantics established in the available skill/reference information or verified against the datasource/schema.
* When required information has not been verified against the datasource/schema, mark it as requiring verification rather than guessing.
* Apply only node, GPU, device, or other scope constraints explicitly provided by the user or established by the available reference information.
* Preserve every measurement explicitly requested by the user.
* Treat multiple plausible metrics as candidate interpretations of a request, rather than as evidence that the user requested multiple metrics.
* Treat a derived measurement requiring multiple source metrics as distinct from a request for multiple independent measurements.
* When multiple materially different measurements remain plausible, request clarification.
* Treat the Metric Directory as a routing aid only. Verify final metric selection using the detailed metric definition in the relevant domain file.
* Keep cross-domain semantic concepts in this file and domain-specific semantic concepts in the corresponding domain file.
* Defer datasource syntax, query construction, parameter defaults, output formatting, and generic error handling to the Main Skill and Prometheus Fundamentals.
