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
| GPU | Compute/graphics engine (SM) active time — profiling-based | `DCGM_FI_PROF_GR_ENGINE_ACTIVE` |
| Memory | Memory controller / bandwidth utilization | `DCGM_FI_DEV_MEM_COPY_UTIL` |
| Temperature | Memory (HBM/VRAM) temperature | `DCGM_FI_DEV_MEMORY_TEMP` |
| Power | Instantaneous GPU power draw | `DCGM_FI_DEV_POWER_USAGE` |
| Power | Cumulative time spent power-throttled | `DCGM_FI_DEV_POWER_VIOLATION` |
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

#### GPU Utilization: Overall vs. Profiling-Based Engine Activity

`DCGM_FI_DEV_GPU_UTIL` and `DCGM_FI_PROF_GR_ENGINE_ACTIVE` both describe GPU compute activity but come from different measurement subsystems within DCGM — the former is the general-purpose utilization signal, the latter is a profiling-based measurement of the graphics/SM engine specifically. The precise relationship and relative accuracy between these two signals should be treated as requiring verification against the DCGM reference/schema rather than asserted definitively.

Select `DCGM_FI_PROF_GR_ENGINE_ACTIVE` when the request specifically references the compute/graphics engine, SM activity, or profiling-based utilization. Select `DCGM_FI_DEV_GPU_UTIL` for general "GPU utilization" or "how busy is the GPU" phrasing, consistent with its existing Intent Examples.

A bare request such as "Show GPU utilization." should continue to resolve to `DCGM_FI_DEV_GPU_UTIL` unless the phrasing specifically invokes the compute/graphics engine.

#### GPU Core Temperature vs. Memory Temperature

`DCGM_FI_DEV_GPU_TEMP` measures the GPU core/die temperature. `DCGM_FI_DEV_MEMORY_TEMP` measures the temperature of a different physical component — the HBM/VRAM memory chips. These are not interchangeable and are not the same sensor.

Generic phrasing ("How hot is the GPU?", "Show GPU temperature.") should resolve to `DCGM_FI_DEV_GPU_TEMP`, consistent with its existing Intent Examples. Select `DCGM_FI_DEV_MEMORY_TEMP` only when the request specifically references memory, VRAM, or HBM temperature.

#### GPU Memory: Capacity vs. Bandwidth Utilization

Framebuffer memory measurements (`DCGM_FI_DEV_FB_USED`, `DCGM_FI_DEV_FB_FREE`) describe memory *capacity* — how much space is occupied or free. `DCGM_FI_DEV_MEM_COPY_UTIL` describes memory *bandwidth* — how busy the memory controller/bus is, expressed as a percentage. These are fundamentally different physical quantities that happen to share the word "memory," and must not be treated as interchangeable despite both falling under the Memory category.

A request for "GPU memory" alone remains ambiguous per **Local Fundamentals → Framebuffer Memory Measurements**, and that ambiguity now also includes distinguishing capacity from bandwidth when the request could plausibly mean either.

#### GPU Power Measurements

`DCGM_FI_DEV_POWER_USAGE` measures instantaneous power draw. `DCGM_FI_DEV_POWER_VIOLATION` measures cumulative time spent power-throttled — a materially different measurement (a rate/duration signal, not a power reading). A bare request such as "Show power." should be treated as ambiguous between these two; a request specifically about throttling, limits, or violations should resolve to `DCGM_FI_DEV_POWER_VIOLATION`.
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
- Memory bandwidth/controller utilization (not capacity) -> `DCGM_FI_DEV_MEM_COPY_UTIL`

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
- Memory bandwidth/controller utilization (not capacity) -> `DCGM_FI_DEV_MEM_COPY_UTIL`

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

### 6.6 `DCGM_FI_PROF_GR_ENGINE_ACTIVE`

**Category:**  
GPU

**Purpose:**  
Measures the fraction of time the compute/graphics (SM) engine was active, as a profiling-based measurement.

**Type:**  
Gauge

**Unit:**  
Percent

**Use When:**
- The user requests compute engine utilization specifically.
- The user wants a profiling-based measurement of graphics/SM engine activity.
- The user explicitly distinguishes this from general GPU utilization.

**Do Not Use / Confusable With:**
- General/overall GPU utilization -> `DCGM_FI_DEV_GPU_UTIL`
- Tensor Core activity -> `DCGM_FI_PROF_PIPE_TENSOR_ACTIVE`

For broader distinctions, see **Local Fundamentals → Confusable Metric Families → GPU Utilization: Overall vs. Profiling-Based Engine Activity**.

**Relevant Scope / Dimensions:**
- GPU

**Known Labels:**

Not yet verified from the available datasource/schema.

Do not infer or invent label names.

**Intent Examples:**
- "Show compute engine utilization."
- "How active is the SM/graphics engine?"

**Edge / Confusable Example:**
- "Show GPU utilization." -> `DCGM_FI_DEV_GPU_UTIL`
- "Show GPU." -> `AMBIGUOUS`

**Metric-Specific Query / Result Semantics:**

The raw Gauge value directly represents the requested measurement.

The resulting value represents the fraction of time the compute/graphics engine was active, expressed as a percentage.

No additional metric-specific transformation is specified by the project reference.

---

### 6.7 `DCGM_FI_DEV_MEM_COPY_UTIL`

**Category:**  
Memory

**Purpose:**  
Measures memory controller (bandwidth) utilization.

**Type:**  
Gauge

**Unit:**  
Percent

**Use When:**
- The user requests memory bandwidth utilization.
- The user wants to know how busy the memory controller/bus is.
- The user explicitly distinguishes bandwidth from memory capacity.

**Do Not Use / Confusable With:**
- Used framebuffer memory (capacity, not bandwidth) -> `DCGM_FI_DEV_FB_USED`
- Available framebuffer memory (capacity, not bandwidth) -> `DCGM_FI_DEV_FB_FREE`

For broader distinctions, see **Local Fundamentals → Confusable Metric Families → GPU Memory: Capacity vs. Bandwidth Utilization**.

**Relevant Scope / Dimensions:**
- GPU

**Known Labels:**

Not yet verified from the available datasource/schema.

Do not infer or invent label names.

**Intent Examples:**
- "Show memory bandwidth utilization."
- "How busy is the memory controller?"

**Edge / Confusable Example:**
- "Show GPU memory." -> `AMBIGUOUS`
- "How much GPU memory is used?" -> `DCGM_FI_DEV_FB_USED`

**Metric-Specific Query / Result Semantics:**

The raw Gauge value directly represents the requested measurement.

The resulting value represents memory controller utilization as a percentage.

No additional metric-specific transformation is specified by the project reference.

---

### 6.8 `DCGM_FI_DEV_MEMORY_TEMP`

**Category:**  
Temperature

**Purpose:**  
Measures HBM/VRAM (memory) temperature.

**Type:**  
Gauge

**Unit:**  
Degrees Celsius

**Use When:**
- The user requests memory or VRAM temperature specifically.
- The user wants to distinguish memory thermal conditions from core GPU temperature.

**Do Not Use / Confusable With:**
- GPU core/die temperature -> `DCGM_FI_DEV_GPU_TEMP`

For broader distinctions, see **Local Fundamentals → Confusable Metric Families → GPU Core Temperature vs. Memory Temperature**.

**Relevant Scope / Dimensions:**
- GPU

**Known Labels:**

Not yet verified from the available datasource/schema.

Do not infer or invent label names.

**Intent Examples:**
- "Show memory temperature."
- "How hot is the VRAM?"

**Edge / Confusable Example:**
- "How hot is the GPU?" -> `DCGM_FI_DEV_GPU_TEMP`
- "Show GPU temperature." -> `DCGM_FI_DEV_GPU_TEMP`

**Metric-Specific Query / Result Semantics:**

The raw Gauge value directly represents the requested measurement.

The resulting value represents the current HBM/VRAM temperature in degrees Celsius.

No additional metric-specific transformation is specified by the project reference.

---

### 6.9 `DCGM_FI_DEV_POWER_USAGE`

**Category:**  
Power

**Purpose:**  
Measures instantaneous GPU power draw.

**Type:**  
Gauge

**Unit:**  
Watts

**Use When:**
- The user requests current power consumption or power draw.
- The user wants to monitor GPU power usage.

**Do Not Use / Confusable With:**
- Cumulative power-throttling time -> `DCGM_FI_DEV_POWER_VIOLATION`

For broader distinctions, see **Local Fundamentals → Confusable Metric Families → GPU Power Measurements**.

**Relevant Scope / Dimensions:**
- GPU

**Known Labels:**

Not yet verified from the available datasource/schema.

Do not infer or invent label names.

**Intent Examples:**
- "Show power consumption."
- "How much power is the GPU drawing?"

**Edge / Confusable Example:**
- "Show power." -> `AMBIGUOUS`
- "Has the GPU been power-throttled?" -> `DCGM_FI_DEV_POWER_VIOLATION`

**Metric-Specific Query / Result Semantics:**

The raw Gauge value directly represents the requested measurement.

The resulting value represents instantaneous GPU power draw in watts.

No additional metric-specific transformation is specified by the project reference.

---

### 6.10 `DCGM_FI_DEV_POWER_VIOLATION`

**Category:**  
Power

**Purpose:**  
Measures the cumulative time the GPU has spent power-throttled.

**Type:**  
Counter

**Unit:**  
Cumulative time (unit as exposed by the datasource — confirm before charting)

**Use When:**
- The user wants to detect or monitor power throttling over time.
- The user asks whether the GPU has been limited by power constraints.

**Do Not Use / Confusable With:**
- Instantaneous power draw -> `DCGM_FI_DEV_POWER_USAGE`

For broader distinctions, see **Local Fundamentals → Confusable Metric Families → GPU Power Measurements**.

**Relevant Scope / Dimensions:**
- GPU

**Known Labels:**

Not yet verified from the available datasource/schema.

Do not infer or invent label names.

**Intent Examples:**
- "Has the GPU been power-throttled?"
- "Detect power throttling over time."

**Edge / Confusable Example:**
- "How much power is being drawn?" -> `DCGM_FI_DEV_POWER_USAGE`
- "Show power." -> `AMBIGUOUS`

**Metric-Specific Query / Result Semantics:**

This Counter represents cumulative time spent power-throttled since the last reset.

The raw Counter value does not directly represent a meaningful current measurement. A meaningful user-facing measurement (e.g., throttling rate or whether throttling occurred recently) requires interpretation as a rate or increase over a time interval, following the shared Counter handling defined by the main skill.
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