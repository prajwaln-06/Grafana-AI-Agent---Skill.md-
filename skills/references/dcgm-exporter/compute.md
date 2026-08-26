Defines the metrics available for GPU utilization and compute-pipeline activity
under DCGM Exporter, and the semantics needed to select and query each one
correctly.

## Contents

- Quick Facts
- Domain Fundamentals
- Metric Definitions
  - `DCGM_FI_DEV_GPU_UTIL`
  - `DCGM_FI_PROF_GR_ENGINE_ACTIVE`
  - `DCGM_FI_PROF_PIPE_TENSOR_ACTIVE`
  - `DCGM_FI_PROF_PIPE_FP64_ACTIVE`
  - `DCGM_FI_PROF_PIPE_FP32_ACTIVE`
  - `DCGM_FI_PROF_PIPE_FP16_ACTIVE`
- Domain-Specific Guardrails

## Quick Facts

- **Parent exporter:** dcgm-exporter
- **Domain:** compute
- **Covers:** GPU utilization and compute-pipeline activity
- **Metric count:** 6
- **Merged from:** GPU Utilization — retained as part of the broader Compute
  functional domain because it describes overall GPU/compute activity. Tensor
  Cores — merged into Compute because tensor-core activity is a form of GPU
  compute activity. Compute — retained as part of the broader Compute
  functional domain because it contains precision-pipeline utilization
  metrics.

## Domain Fundamentals

Concepts true across this functional domain only. A concept true across multiple
domains belongs in `overview.md`'s Exporter Fundamentals instead.

### Common Labels & Dimensions

Label keys for this domain's metrics are sourced dynamically at query-generation
time — see `SKILL.md` §5 Principle 9 and `overview.md` § Entity Scope Baseline.
This domain has no intrinsic dimension beyond the GPU-level entity scope
itself (unlike, for example, `node_cpu_seconds_total`'s `mode` dimension) — each
metric in this domain represents one utilization figure per GPU/engine, without
an additional semantic sub-dimension documented here.

### Confusable Measurements

**Overall GPU Utilization vs. Graphics/SM Engine Activity:**

| Metric | What it measures | Use for |
|---|---|---|
| `DCGM_FI_DEV_GPU_UTIL` | Overall GPU utilization | General GPU utilization, busy/idle, or overall GPU activity |
| `DCGM_FI_PROF_GR_ENGINE_ACTIVE` | Fraction of time the graphics/SM engine is active | Specifically graphics/SM/compute-engine active time |

> **Unit note (unresolved, do not normalize):** `DCGM_FI_DEV_GPU_UTIL` is
> documented as Percent (%); `DCGM_FI_PROF_GR_ENGINE_ACTIVE` is documented as a
> Fraction/utilization ratio. This discrepancy between two conceptually similar
> measurements has not been resolved against the datasource and should not be
> silently normalized to match one another — use each metric's own documented
> unit as-is until independently verified.

**Compute-Pipeline Activity vs. Overall GPU Utilization** —
`DCGM_FI_PROF_GR_ENGINE_ACTIVE`, `DCGM_FI_PROF_PIPE_TENSOR_ACTIVE`,
`DCGM_FI_PROF_PIPE_FP64_ACTIVE`, `DCGM_FI_PROF_PIPE_FP32_ACTIVE`, and
`DCGM_FI_PROF_PIPE_FP16_ACTIVE` all describe more specific aspects of GPU
compute activity than `DCGM_FI_DEV_GPU_UTIL`'s general measurement. Do not
treat a request for a specific pipeline or Tensor Core as a request for overall
GPU utilization, and do not infer a specific precision pipeline from a generic
GPU-utilization request — if the user's wording specifically identifies a
compute pipeline or Tensor Core, select that corresponding metric rather than
the general GPU utilization metric.

## Metric Definitions

### `DCGM_FI_DEV_GPU_UTIL`

- **Category:** GPU Utilization
- **Purpose:** Measures overall GPU utilization as a percentage.
- **Type:** `Gauge`
- **Unit:** Percent (%)
- **Use when:** the user asks for general GPU utilization; how busy or idle a
  GPU is; or wants to compare overall GPU utilization across GPUs.
- **Do not use / confusable with:** specifically graphics/SM/compute-engine
  active time → `DCGM_FI_PROF_GR_ENGINE_ACTIVE`; a specific Tensor Core or
  precision-pipeline utilization request → the corresponding pipeline metric.
- **Relevant scope:** GPU (not a label list).
- **Additional known labels:** sourced dynamically at query-generation time from the runtime — see `SKILL.md` §5 Principle 9. No intrinsic dimension beyond the GPU-level scope is documented for this metric.
- **Intent examples:** "What is the GPU utilization?", "How busy is GPU 2?"
- **Edge/confusable example:** user asks specifically how much time the
  SM/compute engine is active → use `DCGM_FI_PROF_GR_ENGINE_ACTIVE`, not
  `DCGM_FI_DEV_GPU_UTIL`.
- **Metric-specific query/result semantics:** directly represents overall GPU
  utilization. No per-metric override of `SKILL.md` defaults is currently
  defined.
- **Query examples:** no verified DCGM PromQL query example is currently
  available. Do not invent a literal query example.

### `DCGM_FI_PROF_GR_ENGINE_ACTIVE`

- **Category:** GPU Utilization
- **Purpose:** Measures the fraction of time the GPU graphics/SM engine is
  active.
- **Type:** `Gauge`
- **Unit:** Fraction / utilization ratio
- **Use when:** the user specifically asks about graphics-engine activity; how
  much of the time the SM/compute engine is active; or specifically about
  compute-engine utilization.
- **Do not use / confusable with:** general GPU utilization or a generic "how
  busy is the GPU?" request → `DCGM_FI_DEV_GPU_UTIL`; Tensor Core activity →
  `DCGM_FI_PROF_PIPE_TENSOR_ACTIVE`; FP64, FP32, or FP16 pipeline utilization →
  the corresponding precision-pipeline metric.
- **Relevant scope:** GPU (not a label list).
- **Additional known labels:** sourced dynamically at query-generation time from the runtime — see `SKILL.md` §5 Principle 9. No intrinsic dimension beyond the GPU-level scope is documented for this metric.
- **Intent examples:** "How much of the time is the GPU compute engine
  active?", "What is the graphics/SM engine utilization?"
- **Edge/confusable example:** user asks for overall GPU utilization → use
  `DCGM_FI_DEV_GPU_UTIL`, not this metric.
- **Metric-specific query/result semantics:** represents the fraction of time
  the graphics/SM engine is active. No per-metric override of `SKILL.md`
  defaults is currently defined.
- **Query examples:** no verified DCGM PromQL query example is currently
  available. Do not invent a literal query example.

### `DCGM_FI_PROF_PIPE_TENSOR_ACTIVE`

- **Category:** Tensor Cores
- **Purpose:** Measures Tensor Core utilization.
- **Type:** `Gauge`
- **Unit:** Percent (%)
- **Use when:** the user asks about Tensor Core activity; how much Tensor Core
  utilization there is; or specifically about Tensor Core workload activity.
- **Do not use / confusable with:** general GPU utilization →
  `DCGM_FI_DEV_GPU_UTIL`; general graphics/SM engine activity →
  `DCGM_FI_PROF_GR_ENGINE_ACTIVE`; FP64, FP32, or FP16 pipeline utilization →
  the corresponding precision-pipeline metric.
- **Relevant scope:** GPU (not a label list).
- **Additional known labels:** sourced dynamically at query-generation time from the runtime — see `SKILL.md` §5 Principle 9. No intrinsic dimension beyond the GPU-level scope is documented for this metric.
- **Intent examples:** "How active are the Tensor Cores?", "What is the Tensor
  Core utilization?"
- **Edge/confusable example:** user asks for general GPU utilization rather
  than Tensor Core activity → use `DCGM_FI_DEV_GPU_UTIL`.
- **Metric-specific query/result semantics:** represents Tensor Core
  utilization. No per-metric override of `SKILL.md` defaults is currently
  defined.
- **Query examples:** no verified DCGM PromQL query example is currently
  available. Do not invent a literal query example.

### `DCGM_FI_PROF_PIPE_FP64_ACTIVE`

- **Category:** Compute
- **Purpose:** Measures FP64 pipeline utilization.
- **Type:** `Gauge`
- **Unit:** Percent (%)
- **Use when:** the user asks about FP64 utilization; double-precision
  workload activity; or specifically about FP64 pipeline activity.
- **Do not use / confusable with:** FP32 pipeline utilization →
  `DCGM_FI_PROF_PIPE_FP32_ACTIVE`; FP16 pipeline utilization →
  `DCGM_FI_PROF_PIPE_FP16_ACTIVE`; overall GPU utilization →
  `DCGM_FI_DEV_GPU_UTIL`.
- **Relevant scope:** GPU (not a label list).
- **Additional known labels:** sourced dynamically at query-generation time from the runtime — see `SKILL.md` §5 Principle 9. No intrinsic dimension beyond the GPU-level scope is documented for this metric.
- **Intent examples:** "How much FP64 workload is running?", "What is the FP64
  pipeline utilization?"
- **Edge/confusable example:** user asks about FP32 workload rather than FP64
  → use `DCGM_FI_PROF_PIPE_FP32_ACTIVE`.
- **Metric-specific query/result semantics:** represents FP64 pipeline
  utilization and therefore specifically describes double-precision compute
  activity rather than overall GPU utilization. No per-metric override of
  `SKILL.md` defaults is currently defined.
- **Query examples:** no verified DCGM PromQL query example is currently
  available. Do not invent a literal query example.

### `DCGM_FI_PROF_PIPE_FP32_ACTIVE`

- **Category:** Compute
- **Purpose:** Measures FP32 pipeline utilization.
- **Type:** `Gauge`
- **Unit:** Percent (%)
- **Use when:** the user asks about FP32 utilization; single-precision
  workload activity; or specifically about FP32 pipeline activity.
- **Do not use / confusable with:** FP64 pipeline utilization →
  `DCGM_FI_PROF_PIPE_FP64_ACTIVE`; FP16 pipeline utilization →
  `DCGM_FI_PROF_PIPE_FP16_ACTIVE`; overall GPU utilization →
  `DCGM_FI_DEV_GPU_UTIL`.
- **Relevant scope:** GPU (not a label list).
- **Additional known labels:** sourced dynamically at query-generation time from the runtime — see `SKILL.md` §5 Principle 9. No intrinsic dimension beyond the GPU-level scope is documented for this metric.
- **Intent examples:** "What is the FP32 pipeline utilization?", "How much
  single-precision workload is running?"
- **Edge/confusable example:** user asks about double-precision workload → use
  `DCGM_FI_PROF_PIPE_FP64_ACTIVE`.
- **Metric-specific query/result semantics:** represents FP32 pipeline
  utilization and specifically describes single-precision compute activity
  rather than overall GPU utilization. No per-metric override of `SKILL.md`
  defaults is currently defined.
- **Query examples:** no verified DCGM PromQL query example is currently
  available. Do not invent a literal query example.

### `DCGM_FI_PROF_PIPE_FP16_ACTIVE`

- **Category:** Compute
- **Purpose:** Measures FP16 pipeline utilization.
- **Type:** `Gauge`
- **Unit:** Percent (%)
- **Use when:** the user asks about FP16 utilization; mixed-precision workload
  activity; or specifically about FP16 pipeline activity.
- **Do not use / confusable with:** FP64 pipeline utilization →
  `DCGM_FI_PROF_PIPE_FP64_ACTIVE`; FP32 pipeline utilization →
  `DCGM_FI_PROF_PIPE_FP32_ACTIVE`; overall GPU utilization →
  `DCGM_FI_DEV_GPU_UTIL`.
- **Relevant scope:** GPU (not a label list).
- **Additional known labels:** sourced dynamically at query-generation time from the runtime — see `SKILL.md` §5 Principle 9. No intrinsic dimension beyond the GPU-level scope is documented for this metric.
- **Intent examples:** "What is the FP16 pipeline utilization?", "How much
  mixed-precision workload is running?"
- **Edge/confusable example:** user asks about FP32 workload rather than FP16
  → use `DCGM_FI_PROF_PIPE_FP32_ACTIVE`.
- **Metric-specific query/result semantics:** represents FP16 pipeline
  utilization and specifically describes mixed-precision compute activity
  rather than overall GPU utilization. No per-metric override of `SKILL.md`
  defaults is currently defined.
- **Query examples:** no verified DCGM PromQL query example is currently
  available. Do not invent a literal query example.

## Domain-Specific Guardrails

- Do not treat overall GPU utilization as interchangeable with graphics/SM
  engine activity or a specific compute-pipeline utilization.
- Do not infer a specific precision pipeline from a generic request about GPU
  utilization.
- Do not invent Prometheus label names or label values — label keys must be
  confirmed by the runtime, never assumed from this reference (`SKILL.md` §5
  Principle 9).
- Do not assume a specific GPU, device, or node unless the user provides the
  relevant constraint.
- Do not invent a PromQL expression when the required query semantics have not
  been verified.
- Do not normalize or reconcile the `DCGM_FI_DEV_GPU_UTIL` /
  `DCGM_FI_PROF_GR_ENGINE_ACTIVE` unit discrepancy noted above — use each
  metric's documented unit as-is.
