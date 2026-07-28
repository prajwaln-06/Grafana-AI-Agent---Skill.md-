---
name: dcgm_exporter
description: DCGM Exporter metrics for monitoring GPU utilization, GPU temperature, framebuffer memory usage, and Tensor Core activity through Prometheus.
data_source: prometheus
version: 1.0
---

# DCGM Exporter Metrics Sub-Skill

## 1. File-Level Routing

### Purpose

This sub-skill interprets user requests related to NVIDIA GPU observability using metrics exposed by the DCGM Exporter.

It covers GPU utilization, GPU temperature, framebuffer memory usage, Tensor Core activity, and other GPU resource measurements represented by the metrics defined in this sub-skill.

### Trigger Examples

Examples of user requests that should route to this sub-skill:

- "Show GPU utilization."
- "How hot is the GPU?"
- "Show GPU temperature."
- "How much GPU memory is being used?"
- "How much GPU memory is available?"
- "Show Tensor Core utilization."
- "Compare GPU utilization across all GPUs."

These examples illustrate intent and are not an exhaustive whitelist.

### Do Not Use

Do not use this sub-skill for:

- CPU, memory, filesystem, or host operating-system metrics -> `node_exporter`
- Application or service logs -> appropriate OpenSearch sub-skill
- Application-specific metrics not exposed by DCGM Exporter

---

## 2. Metric Selection Procedure

After the main skill routes a request to this sub-skill:

1. Identify **all measurements explicitly requested by the user** before selecting a metric or metric composition.

2. Use the Metric Directory to identify the relevant semantic category and candidate metric(s) or derived/composed measurement(s).

3. Verify each candidate using its detailed:
   - Purpose
   - Use When
   - Do Not Use / Confusable With
   - Intent Examples

4. Check whether the requested measurement is:
   - directly represented by a single metric;
   - explicitly requesting multiple independent measurements; or
   - a derived/composed measurement requiring multiple source metrics.

5. Preserve all constraints explicitly provided by the user, such as:
   - GPU
   - device
   - time range
   - comparison scope

6. Do not invent scope constraints that the user did not provide.

7. Select multiple independent metrics **only when the user explicitly requests multiple distinct measurements**.

8. A single requested measurement may legitimately require multiple source metrics when it is defined as a **derived/composed measurement** in this sub-skill. This is not the same as the user requesting multiple independent measurements.

9. Do not convert a vague or underspecified request into a multi-metric request merely to provide a more comprehensive answer.

10. If multiple materially different measurements could plausibly satisfy the request and the user has not indicated which one(s) they want, classify the request as **AMBIGUOUS** and request clarification.

    Ambiguity must not be resolved by:
    - arbitrarily choosing one plausible metric;
    - selecting all plausible metrics;
    - assuming the user wants a comprehensive overview.

11. If no metric or derived/composed measurement defined in this file represents the requested measurement, classify the request as unsupported.

    Do not invent:
    - metric names;
    - labels;
    - measurements;
    - relationships between metrics;
    - query expressions.

12. Once the metric(s), metric-specific semantics, and relevant scope are resolved, defer shared query construction, time handling, aggregation, final structured output, and generic error handling to the main skill.

---

## 3. Metric Directory

Use this directory for initial metric discovery.

**The Metric Directory must be exhaustive: every raw metric supported by this sub-skill must appear here.**

A metric must not exist only in the detailed Metric Definitions without a corresponding Metric Directory entry.

The detailed metric definitions below are authoritative for final metric selection.

| Category | Intent / Measurement | Metric |
|---|---|---|
| GPU | GPU utilization | `DCGM_FI_DEV_GPU_UTIL` |
| Temperature | GPU temperature | `DCGM_FI_DEV_GPU_TEMP` |
| Memory | Used framebuffer memory | `DCGM_FI_DEV_FB_USED` |
| Memory | Available framebuffer memory | `DCGM_FI_DEV_FB_FREE` |
| Tensor Cores | Tensor Core activity | `DCGM_FI_PROF_PIPE_TENSOR_ACTIVE` |

---

## 4. Derived / Composed Measurements

Use this section when a **single conceptual measurement requested by the user requires multiple source metrics**.

This is different from a request that explicitly asks for multiple independent measurements.

Only define derived/composed measurements when the relationship is supported by the provided metric/reference information or has otherwise been explicitly verified for the project.

Do not invent relationships between metrics.

This section must always be retained to preserve a consistent sub-skill structure.

No derived/composed measurements are currently defined for this exporter.

---

## 5. Local Fundamentals

This section contains concepts that apply across multiple DCGM Exporter metrics but are not general query-language rules.

Do not duplicate general Counter/Gauge handling, range-vector behavior, aggregation, time-window handling, or shared query-construction rules from the main skill.

### 5.1 Entity Scope and Dimensions

The metrics in this sub-skill describe GPU hardware resources.

When the user explicitly specifies a GPU, GPU index, device, or collection of GPUs, preserve that scope during metric selection.

Examples include:

- a specific GPU;
- multiple GPUs;
- all monitored GPUs.

Do not invent GPU or device constraints that the user did not provide.

Concrete label names should only be documented after they have been verified from the actual datasource/schema.

---

### 5.2 Metric Ambiguity vs Parameter Vagueness

Request clarification only when the **requested measurement itself cannot be determined**.

Examples of metric ambiguity include:

- "Show GPU."
- "Show GPU memory."
- "Show GPU performance."

These requests may correspond to multiple semantically distinct measurements supported by this sub-skill.

By contrast, requests such as:

- "Show GPU utilization."
- "Show GPU temperature."
- "Show Tensor Core utilization."

identify a single measurement even if they omit details such as GPU selection or time range.

Do not classify a request as metric-ambiguous merely because query parameters or scope details were omitted.

If the requested measurement can still be identified confidently, select the appropriate metric and defer handling of missing parameters to the main skill.

---

### 5.3 Confusable Metric Families

#### GPU Activity Measurements

GPU utilization, GPU temperature, and Tensor Core activity describe different aspects of GPU behavior.

These measurements are not interchangeable.

Examples include:

- GPU utilization;
- GPU temperature;
- Tensor Core activity.

A broad request such as:

> "Show GPU."

may require clarification because multiple GPU-related measurements could reasonably satisfy the request.

Where the user's wording clearly identifies GPU utilization or GPU temperature, select only that measurement.

---

### 5.4 Framebuffer Memory Measurements

Framebuffer memory measurements represent different aspects of GPU memory.

This sub-skill currently supports:

- used framebuffer memory;
- available framebuffer memory.

These measurements are semantically distinct.

If the user requests only "GPU memory" without indicating the intended measurement, request clarification rather than assuming which measurement is desired.

---

### 5.5 GPU Compute Activity

GPU utilization and Tensor Core activity measure different aspects of GPU computation.

GPU utilization represents overall GPU activity.

Tensor Core activity specifically represents utilization of Tensor Core execution pipelines.

Requests that explicitly mention Tensor Cores should always select the Tensor Core metric rather than overall GPU utilization.

---

## 6. Metric Definitions

### 6.1 `DCGM_FI_DEV_GPU_UTIL`

**Category:**  
GPU

**Purpose:**  
Measures GPU utilization.

**Type:**  
Gauge

**Unit:**  
Percent

**Use When:**
- The user requests GPU utilization.
- The user wants to know how busy the GPU is.
- The user wants to monitor GPU activity.

**Do Not Use / Confusable With:**
- Tensor Core activity -> `DCGM_FI_PROF_PIPE_TENSOR_ACTIVE`
- GPU temperature -> `DCGM_FI_DEV_GPU_TEMP`

For broader distinctions between GPU-related measurements, see **Local Fundamentals → Confusable Metric Families**.

**Relevant Scope / Dimensions:**
- GPU

**Known Labels:**

Not yet verified from the available datasource/schema.

Do not infer or invent label names.

**Intent Examples:**
- "Show GPU utilization."
- "How busy is the GPU?"

**Edge / Confusable Examples:**
- "Show GPU." -> `AMBIGUOUS`
- "How hot is the GPU?" -> `DCGM_FI_DEV_GPU_TEMP`
- "Are Tensor Cores being used?" -> `DCGM_FI_PROF_PIPE_TENSOR_ACTIVE`
- "Show GPU utilization and temperature." -> Select both metrics.

**Metric-Specific Query / Result Semantics:**

The raw Gauge value directly represents the requested measurement.

The resulting value represents GPU utilization as a percentage.

No additional metric-specific transformation is specified by the project reference.

---

### 6.2 `DCGM_FI_DEV_GPU_TEMP`

**Category:**  
Temperature

**Purpose:**  
Measures GPU temperature.

**Type:**  
Gauge

**Unit:**  
Degrees Celsius

**Use When:**
- The user requests GPU temperature.
- The user wants to know how hot the GPU is.
- The user wants to monitor GPU thermal conditions.

**Do Not Use / Confusable With:**
- GPU utilization -> `DCGM_FI_DEV_GPU_UTIL`

For broader distinctions between GPU-related measurements, see **Local Fundamentals → Confusable Metric Families**.

**Relevant Scope / Dimensions:**
- GPU

**Known Labels:**

Not yet verified from the available datasource/schema.

Do not infer or invent label names.

**Intent Examples:**
- "Show GPU temperature."
- "How hot is the GPU?"

**Edge / Confusable Examples:**
- "Show GPU." -> `AMBIGUOUS`
- "How busy is the GPU?" -> `DCGM_FI_DEV_GPU_UTIL`
- "Show GPU temperature and utilization." -> Select both metrics.

**Metric-Specific Query / Result Semantics:**

The raw Gauge value directly represents the requested measurement.

The resulting value represents the current GPU temperature in degrees Celsius.

No additional metric-specific transformation is specified by the project reference.

---

### 6.3 `DCGM_FI_DEV_FB_USED`

**Category:**  
Memory

**Purpose:**  
Measures the amount of framebuffer memory currently in use.

**Type:**  
Gauge

**Unit:**  
MiB

**Use When:**
- The user requests used GPU memory.
- The user wants to know how much framebuffer memory is currently being used.
- The user wants to monitor GPU memory consumption.

**Do Not Use / Confusable With:**
- Available framebuffer memory -> `DCGM_FI_DEV_FB_FREE`

For broader distinctions between framebuffer memory measurements, see **Local Fundamentals → Framebuffer Memory Measurements**.

**Relevant Scope / Dimensions:**
- GPU

**Known Labels:**

Not yet verified from the available datasource/schema.

Do not infer or invent label names.

**Intent Examples:**
- "How much GPU memory is being used?"
- "Show used GPU memory."

**Edge / Confusable Examples:**
- "Show GPU memory." -> `AMBIGUOUS`
- "How much GPU memory is available?" -> `DCGM_FI_DEV_FB_FREE`
- "Show GPU memory usage." -> `DCGM_FI_DEV_FB_USED`
- "Show used and available GPU memory." -> Select both metrics.

**Metric-Specific Query / Result Semantics:**

The raw Gauge value directly represents the requested measurement.

The resulting value represents the amount of framebuffer memory currently in use, expressed in MiB.

No additional metric-specific transformation is specified by the project reference.

---

### 6.4 `DCGM_FI_DEV_FB_FREE`

**Category:**  
Memory

**Purpose:**  
Measures the amount of framebuffer memory currently available.

**Type:**  
Gauge

**Unit:**  
MiB

**Use When:**
- The user requests available GPU memory.
- The user wants to know how much framebuffer memory remains available.
- The user wants to monitor available GPU memory.

**Do Not Use / Confusable With:**
- Used framebuffer memory -> `DCGM_FI_DEV_FB_USED`

For broader distinctions between framebuffer memory measurements, see **Local Fundamentals → Framebuffer Memory Measurements**.

**Relevant Scope / Dimensions:**
- GPU

**Known Labels:**

Not yet verified from the available datasource/schema.

Do not infer or invent label names.

**Intent Examples:**
- "How much GPU memory is available?"
- "Show available GPU memory."

**Edge / Confusable Examples:**
- "Show GPU memory." -> `AMBIGUOUS`
- "How much GPU memory is used?" -> `DCGM_FI_DEV_FB_USED`
- "Show free and used GPU memory." -> Select both metrics.

**Metric-Specific Query / Result Semantics:**

The raw Gauge value directly represents the requested measurement.

The resulting value represents the amount of framebuffer memory currently available, expressed in MiB.

No additional metric-specific transformation is specified by the project reference.

---

### 6.5 `DCGM_FI_PROF_PIPE_TENSOR_ACTIVE`

**Category:**  
Tensor Cores

**Purpose:**  
Measures Tensor Core activity.

**Type:**  
Gauge

**Unit:**  
Percent

**Use When:**
- The user requests Tensor Core utilization.
- The user wants to know whether Tensor Cores are active.
- The user wants to monitor Tensor Core workloads.

**Do Not Use / Confusable With:**
- Overall GPU utilization -> `DCGM_FI_DEV_GPU_UTIL`

For broader distinctions between GPU compute measurements, see **Local Fundamentals → GPU Compute Activity**.

**Relevant Scope / Dimensions:**
- GPU

**Known Labels:**

Not yet verified from the available datasource/schema.

Do not infer or invent label names.

**Intent Examples:**
- "Show Tensor Core utilization."
- "Are the Tensor Cores being used?"

**Edge / Confusable Examples:**
- "Show Tensor Core activity." -> `DCGM_FI_PROF_PIPE_TENSOR_ACTIVE`
- "Show GPU utilization." -> `DCGM_FI_DEV_GPU_UTIL`
- "Show GPU." -> `AMBIGUOUS`
- "Show Tensor Core activity and GPU utilization." -> Select both metrics.

**Metric-Specific Query / Result Semantics:**

The raw Gauge value directly represents the requested measurement.

The resulting value represents Tensor Core activity as a percentage.

No additional metric-specific transformation is specified by the project reference.

---

## 7. Sub-Skill Guardrails

- Only select raw metrics explicitly defined in this sub-skill.

- Only use derived/composed measurements explicitly defined in this sub-skill.

- Treat the metric names, types, units, and metric facts supplied by the project reference as authoritative for this implementation.

- Do not silently replace supplied metric metadata with assumptions from general domain knowledge.

- Do not fabricate metric names, labels, units, dimensions, relationships, or metric semantics.

- If required information is absent from the supplied reference and has not been verified against the datasource/schema, mark it as requiring verification rather than guessing.

- Do not assume scope constraints that the user did not specify.

- Preserve all explicitly requested measurements.

- Multiple plausible metrics do **not** imply that the user requested multiple metrics.

- A derived measurement requiring multiple source metrics is distinct from a request for multiple independent measurements.

- If multiple materially different measurements remain plausible, request clarification.

- Do not treat missing optional query parameters as metric ambiguity when the requested measurement is already clear.

- Treat the Metric Directory as a routing aid; verify final metric selection using the detailed metric definitions.

- Use Local Fundamentals for semantic distinctions shared across groups of related metrics instead of unnecessarily duplicating them in every metric definition.

- Defer shared query-construction rules to the main skill.

- Defer default handling for missing query parameters to the main skill.

- Defer final structured-output formatting to the main skill.

- Defer generic malformed-input, adversarial-input, and cross-cutting error handling to the main skill.