---
parent_exporter: dcgm_exporter
domain_id: compute
covers: GPU utilization and compute-pipeline activity
metric_count: 6

merged_from:
  - GPU Utilization → retained as part of the broader Compute functional domain because it describes overall GPU/compute activity.
  - Tensor Cores → merged into Compute because tensor-core activity is a form of GPU compute activity.
  - Compute → retained as part of the broader Compute functional domain because it contains precision-pipeline utilization metrics.
---

# DCGM Exporter — Compute

## 1. Domain Fundamentals

This section contains concepts that are true across this functional domain only.

If a concept is true across multiple domains, it belongs in the exporter's
**Exporter Fundamentals** section instead.

---

### 1.1 Common Labels & Dimensions

No domain-common labels beyond the exporter-wide baseline.

---

### 1.2 Confusable Metric Families

#### Overall GPU Utilization vs Graphics/SM Engine Activity

- `DCGM_FI_DEV_GPU_UTIL`
- `DCGM_FI_PROF_GR_ENGINE_ACTIVE`

Difference:

`DCGM_FI_DEV_GPU_UTIL` represents overall GPU utilization, while
`DCGM_FI_PROF_GR_ENGINE_ACTIVE` represents the fraction of time the
graphics/SM engine is active.

Use:

- General GPU utilization, busy/idle, or overall GPU activity →
  `DCGM_FI_DEV_GPU_UTIL`
- Specifically graphics/SM/compute-engine active time →
  `DCGM_FI_PROF_GR_ENGINE_ACTIVE`

---

#### Compute-Pipeline Activity vs Overall GPU Utilization

- `DCGM_FI_PROF_GR_ENGINE_ACTIVE`
- `DCGM_FI_PROF_PIPE_TENSOR_ACTIVE`
- `DCGM_FI_PROF_PIPE_FP64_ACTIVE`
- `DCGM_FI_PROF_PIPE_FP32_ACTIVE`
- `DCGM_FI_PROF_PIPE_FP16_ACTIVE`

These metrics describe more specific aspects of GPU compute activity.

Do not treat a request for a specific pipeline or Tensor Core as a request
for overall GPU utilization.

---

### 1.3 Domain-Specific Semantic Distinctions

#### General GPU Activity vs Specific Compute Pipeline Activity

This domain contains both broad GPU activity measurements and
specific compute-pipeline measurements.

- Overall GPU utilization →
  `DCGM_FI_DEV_GPU_UTIL`
- Graphics/SM engine activity →
  `DCGM_FI_PROF_GR_ENGINE_ACTIVE`
- Tensor Core activity →
  `DCGM_FI_PROF_PIPE_TENSOR_ACTIVE`
- FP64 pipeline utilization →
  `DCGM_FI_PROF_PIPE_FP64_ACTIVE`
- FP32 pipeline utilization →
  `DCGM_FI_PROF_PIPE_FP32_ACTIVE`
- FP16 pipeline utilization →
  `DCGM_FI_PROF_PIPE_FP16_ACTIVE`

If the user's wording specifically identifies a compute pipeline or
Tensor Core, select the corresponding metric rather than the general
GPU utilization metric.

---

## 2. Metric Definitions

### 2.1 `DCGM_FI_DEV_GPU_UTIL`

#### Category

GPU Utilization

#### Purpose

Measures overall GPU utilization as a percentage.

#### Type

`Gauge`

#### Unit

Percent (%)

#### Use When

- The user asks for general GPU utilization.
- The user asks how busy or idle a GPU is.
- The user wants to compare overall GPU utilization across GPUs.

#### Do Not Use / Confusable With

- Specifically graphics/SM/compute-engine active time →
  `DCGM_FI_PROF_GR_ENGINE_ACTIVE`
- A specific Tensor Core or precision-pipeline utilization request →
  the corresponding pipeline metric.

#### Relevant Scope

GPU.

This is not a label list.

#### Additional Known Labels

None beyond domain-level labels (see Domain Fundamentals §1.1).

#### Intent Examples

- "What is the GPU utilization?"
- "How busy is GPU 2?"

#### Edge / Confusable Example (Optional)

> User asks specifically how much time the SM/compute engine is active.
>
> Use `DCGM_FI_PROF_GR_ENGINE_ACTIVE`, not
> `DCGM_FI_DEV_GPU_UTIL`.

#### Metric-Specific Query / Result Semantics

The metric directly represents overall GPU utilization.

No per-metric override of Main Skill defaults is currently defined.

#### Query Examples (Optional)

No verified DCGM PromQL query example is currently available.
Do not invent a literal query example.


### 2.2 `DCGM_FI_PROF_GR_ENGINE_ACTIVE`

#### Category

GPU Utilization

#### Purpose

Measures the fraction of time the GPU graphics/SM engine is active.

#### Type

`Gauge`

#### Unit

Fraction / utilization ratio

#### Use When

- The user specifically asks about graphics-engine activity.
- The user asks how much of the time the SM/compute engine is active.
- The user asks specifically about compute-engine utilization.

#### Do Not Use / Confusable With

- General GPU utilization or a generic "how busy is the GPU?" request →
  `DCGM_FI_DEV_GPU_UTIL`
- Tensor Core activity →
  `DCGM_FI_PROF_PIPE_TENSOR_ACTIVE`
- FP64, FP32, or FP16 pipeline utilization →
  the corresponding precision-pipeline metric.

#### Relevant Scope

GPU.

This is not a label list.

#### Additional Known Labels

None beyond domain-level labels (see Domain Fundamentals §1.1).

#### Intent Examples

- "How much of the time is the GPU compute engine active?"
- "What is the graphics/SM engine utilization?"

#### Edge / Confusable Example (Optional)

> User asks for overall GPU utilization.
>
> Use `DCGM_FI_DEV_GPU_UTIL`, not this metric.

#### Metric-Specific Query / Result Semantics

The metric represents the fraction of time the graphics/SM engine is active.

No per-metric override of Main Skill defaults is currently defined.

#### Query Examples (Optional)

No verified DCGM PromQL query example is currently available.
Do not invent a literal query example.


### 2.3 `DCGM_FI_PROF_PIPE_TENSOR_ACTIVE`

#### Category

Tensor Cores

#### Purpose

Measures Tensor Core utilization.

#### Type

`Gauge`

#### Unit

Percent (%)

#### Use When

- The user asks about Tensor Core activity.
- The user asks how much Tensor Core utilization there is.
- The user specifically asks about Tensor Core workload activity.

#### Do Not Use / Confusable With

- General GPU utilization →
  `DCGM_FI_DEV_GPU_UTIL`
- General graphics/SM engine activity →
  `DCGM_FI_PROF_GR_ENGINE_ACTIVE`
- FP64, FP32, or FP16 pipeline utilization →
  the corresponding precision-pipeline metric.

#### Relevant Scope

GPU.

This is not a label list.

#### Additional Known Labels

None beyond domain-level labels (see Domain Fundamentals §1.1).

#### Intent Examples

- "How active are the Tensor Cores?"
- "What is the Tensor Core utilization?"

#### Edge / Confusable Example (Optional)

> User asks for general GPU utilization rather than Tensor Core activity.
>
> Use `DCGM_FI_DEV_GPU_UTIL`.

#### Metric-Specific Query / Result Semantics

The metric represents Tensor Core utilization.

No per-metric override of Main Skill defaults is currently defined.

#### Query Examples (Optional)

No verified DCGM PromQL query example is currently available.
Do not invent a literal query example.


### 2.4 `DCGM_FI_PROF_PIPE_FP64_ACTIVE`

#### Category

Compute

#### Purpose

Measures FP64 pipeline utilization.

#### Type

`Gauge`

#### Unit

Percent (%)

#### Use When

- The user asks about FP64 utilization.
- The user asks about double-precision workload activity.
- The user specifically asks about FP64 pipeline activity.

#### Do Not Use / Confusable With

- FP32 pipeline utilization →
  `DCGM_FI_PROF_PIPE_FP32_ACTIVE`
- FP16 pipeline utilization →
  `DCGM_FI_PROF_PIPE_FP16_ACTIVE`
- Overall GPU utilization →
  `DCGM_FI_DEV_GPU_UTIL`

#### Relevant Scope

GPU.

This is not a label list.

#### Additional Known Labels

None beyond domain-level labels (see Domain Fundamentals §1.1).

#### Intent Examples

- "How much FP64 workload is running?"
- "What is the FP64 pipeline utilization?"

#### Edge / Confusable Example (Optional)

> User asks about FP32 workload rather than FP64.
>
> Use `DCGM_FI_PROF_PIPE_FP32_ACTIVE`.

#### Metric-Specific Query / Result Semantics

The metric represents FP64 pipeline utilization and therefore specifically
describes double-precision compute activity rather than overall GPU utilization.

No per-metric override of Main Skill defaults is currently defined.

#### Query Examples (Optional)

No verified DCGM PromQL query example is currently available.
Do not invent a literal query example.


### 2.5 `DCGM_FI_PROF_PIPE_FP32_ACTIVE`

#### Category

Compute

#### Purpose

Measures FP32 pipeline utilization.

#### Type

`Gauge`

#### Unit

Percent (%)

#### Use When

- The user asks about FP32 utilization.
- The user asks about single-precision workload activity.
- The user specifically asks about FP32 pipeline activity.

#### Do Not Use / Confusable With

- FP64 pipeline utilization →
  `DCGM_FI_PROF_PIPE_FP64_ACTIVE`
- FP16 pipeline utilization →
  `DCGM_FI_PROF_PIPE_FP16_ACTIVE`
- Overall GPU utilization →
  `DCGM_FI_DEV_GPU_UTIL`

#### Relevant Scope

GPU.

This is not a label list.

#### Additional Known Labels

None beyond domain-level labels (see Domain Fundamentals §1.1).

#### Intent Examples

- "What is the FP32 pipeline utilization?"
- "How much single-precision workload is running?"

#### Edge / Confusable Example (Optional)

> User asks about double-precision workload.
>
> Use `DCGM_FI_PROF_PIPE_FP64_ACTIVE`.

#### Metric-Specific Query / Result Semantics

The metric represents FP32 pipeline utilization and specifically describes
single-precision compute activity rather than overall GPU utilization.

No per-metric override of Main Skill defaults is currently defined.

#### Query Examples (Optional)

No verified DCGM PromQL query example is currently available.
Do not invent a literal query example.


### 2.6 `DCGM_FI_PROF_PIPE_FP16_ACTIVE`

#### Category

Compute

#### Purpose

Measures FP16 pipeline utilization.

#### Type

`Gauge`

#### Unit

Percent (%)

#### Use When

- The user asks about FP16 utilization.
- The user asks about mixed-precision workload activity.
- The user specifically asks about FP16 pipeline activity.

#### Do Not Use / Confusable With

- FP64 pipeline utilization →
  `DCGM_FI_PROF_PIPE_FP64_ACTIVE`
- FP32 pipeline utilization →
  `DCGM_FI_PROF_PIPE_FP32_ACTIVE`
- Overall GPU utilization →
  `DCGM_FI_DEV_GPU_UTIL`

#### Relevant Scope

GPU.

This is not a label list.

#### Additional Known Labels

None beyond domain-level labels (see Domain Fundamentals §1.1).

#### Intent Examples

- "What is the FP16 pipeline utilization?"
- "How much mixed-precision workload is running?"

#### Edge / Confusable Example (Optional)

> User asks about FP32 workload rather than FP16.
>
> Use `DCGM_FI_PROF_PIPE_FP32_ACTIVE`.

#### Metric-Specific Query / Result Semantics

The metric represents FP16 pipeline utilization and specifically describes
mixed-precision compute activity rather than overall GPU utilization.

No per-metric override of Main Skill defaults is currently defined.

#### Query Examples (Optional)

No verified DCGM PromQL query example is currently available.
Do not invent a literal query example.

---

## 3. Domain-Specific Guardrails (Optional)

- Do not treat overall GPU utilization as interchangeable with
  graphics/SM engine activity or a specific compute-pipeline utilization.
- Do not infer a specific precision pipeline from a generic request about
  GPU utilization.
- Do not invent Prometheus label names or label values.
- Do not assume a specific GPU, device, or node unless the user provides
  the relevant constraint.
- Do not invent a PromQL expression when the required query semantics have
  not been verified.