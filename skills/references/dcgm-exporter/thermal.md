Defines the metrics available for GPU temperature, power, and clock operating
state under DCGM Exporter, and the semantics needed to select and query each one
correctly.

## Contents

- Quick Facts
- Domain Fundamentals
- Metric Definitions
  - `DCGM_FI_DEV_GPU_TEMP`
  - `DCGM_FI_DEV_MEMORY_TEMP`
  - `DCGM_FI_DEV_POWER_USAGE`
  - `DCGM_FI_DEV_POWER_VIOLATION`
  - `DCGM_FI_DEV_SM_CLOCK`
  - `DCGM_FI_DEV_MEM_CLOCK`
- Domain-Specific Guardrails

## Quick Facts

- **Parent exporter:** dcgm-exporter
- **Domain:** thermal
- **Covers:** GPU temperature, power, and clock operating state
- **Metric count:** 6
- **Merged from:** Temperature — retained as part of the broader Thermal
  functional domain because it describes GPU and GPU-memory thermal state.
  Power — merged into Thermal because power consumption and power-related
  throttling are directly relevant to GPU operating/thermal state. Clocks —
  merged into Thermal because GPU operating frequencies provide context about
  the GPU's current operating state alongside temperature and power.

## Domain Fundamentals

Concepts true across this functional domain only. A concept true across multiple
domains belongs in `overview.md`'s Exporter Fundamentals instead.

### Common Labels & Dimensions

Label keys for this domain's metrics are sourced dynamically at query-generation
time — see `SKILL.md` §5 Principle 9 and `overview.md` § Entity Scope Baseline.
This domain has no intrinsic dimension beyond the GPU-level entity scope
itself — each metric represents one current-state (or, for
`DCGM_FI_DEV_POWER_VIOLATION`, cumulative) figure per GPU, without an
additional semantic sub-dimension documented here.

### Confusable Measurements

**GPU Core Temperature vs. GPU Memory Temperature:**

| Metric | What it measures | Use for |
|---|---|---|
| `DCGM_FI_DEV_GPU_TEMP` | GPU core temperature | GPU/core/general GPU temperature |
| `DCGM_FI_DEV_MEMORY_TEMP` | HBM/VRAM memory temperature | GPU memory/HBM/VRAM temperature |

**GPU Core Clock vs. GPU Memory Clock:**

| Metric | What it measures | Use for |
|---|---|---|
| `DCGM_FI_DEV_SM_CLOCK` | SM/core clock frequency | GPU/core/SM frequency |
| `DCGM_FI_DEV_MEM_CLOCK` | GPU memory clock frequency | GPU memory frequency |

**Power Usage vs. Power Violation:**

| Metric | What it measures | Use for |
|---|---|---|
| `DCGM_FI_DEV_POWER_USAGE` | Instantaneous GPU power draw | Current GPU power consumption/draw |
| `DCGM_FI_DEV_POWER_VIOLATION` | Time spent under power throttling | Power throttling over time |

**Current Operating State vs. Throttling History** — this domain contains both
current-state measurements (GPU temperature, GPU memory temperature, GPU power
usage, SM/core clock, memory clock) and one cumulative throttling measurement.
`DCGM_FI_DEV_POWER_VIOLATION` is different from the rest of this domain: it is
a Counter representing accumulated time spent power throttled, intended for
detecting throttling over time — not a current-state snapshot. Do not treat the
power-violation counter as the current power consumption value.

## Metric Definitions

### `DCGM_FI_DEV_GPU_TEMP`

- **Category:** Temperature
- **Purpose:** Measures GPU core temperature.
- **Type:** `Gauge`
- **Unit:** Degrees Celsius (°C)
- **Use when:** the user asks for GPU temperature; GPU core temperature;
  whether a GPU is overheating or running hot; or wants to compare GPU core
  temperatures.
- **Do not use / confusable with:** GPU memory/HBM/VRAM temperature →
  `DCGM_FI_DEV_MEMORY_TEMP`.
- **Relevant scope:** GPU (not a label list).
- **Additional known labels:** sourced dynamically at query-generation time from the runtime — see `SKILL.md` §5 Principle 9. No intrinsic dimension beyond the GPU-level scope is documented for this metric.
- **Intent examples:** "What is the GPU temperature?", "Is GPU 2 running hot?"
- **Edge/confusable example:** user specifically asks about HBM or VRAM
  temperature → use `DCGM_FI_DEV_MEMORY_TEMP`, not this metric.
- **Metric-specific query/result semantics:** directly represents GPU core
  temperature. No per-metric override of `SKILL.md` defaults is currently
  defined.
- **Query examples:** no verified DCGM PromQL query example is currently
  available. Do not invent a literal query example.

### `DCGM_FI_DEV_MEMORY_TEMP`

- **Category:** Temperature
- **Purpose:** Measures HBM/VRAM GPU memory temperature.
- **Type:** `Gauge`
- **Unit:** Degrees Celsius (°C)
- **Use when:** the user asks for GPU memory temperature; HBM temperature;
  VRAM temperature; or whether GPU memory is overheating or running hot.
- **Do not use / confusable with:** GPU core or general GPU temperature →
  `DCGM_FI_DEV_GPU_TEMP`.
- **Relevant scope:** GPU memory / HBM / VRAM (not a label list).
- **Additional known labels:** sourced dynamically at query-generation time from the runtime — see `SKILL.md` §5 Principle 9. No intrinsic dimension beyond the GPU-level scope is documented for this metric.
- **Intent examples:** "What is the GPU memory temperature?", "Is the HBM
  getting too hot?"
- **Edge/confusable example:** user asks for general GPU/core temperature →
  use `DCGM_FI_DEV_GPU_TEMP`, not this metric.
- **Metric-specific query/result semantics:** represents GPU memory/HBM
  temperature rather than GPU core temperature. No per-metric override of
  `SKILL.md` defaults is currently defined.
- **Query examples:** no verified DCGM PromQL query example is currently
  available. Do not invent a literal query example.

### `DCGM_FI_DEV_POWER_USAGE`

- **Category:** Power
- **Purpose:** Measures instantaneous GPU power draw.
- **Type:** `Gauge`
- **Unit:** Watts (W)
- **Use when:** the user asks for current GPU power consumption; how much
  power a GPU is drawing; or wants to compare GPU power usage.
- **Do not use / confusable with:** power throttling over time →
  `DCGM_FI_DEV_POWER_VIOLATION`.
- **Relevant scope:** GPU (not a label list).
- **Additional known labels:** sourced dynamically at query-generation time from the runtime — see `SKILL.md` §5 Principle 9. No intrinsic dimension beyond the GPU-level scope is documented for this metric.
- **Intent examples:** "How much power is the GPU using?", "What is the
  current GPU power draw?"
- **Edge/confusable example:** user asks whether the GPU has been power
  throttling → use `DCGM_FI_DEV_POWER_VIOLATION`, not this metric.
- **Metric-specific query/result semantics:** represents instantaneous GPU
  power draw. No per-metric override of `SKILL.md` defaults is currently
  defined.
- **Query examples:** no verified DCGM PromQL query example is currently
  available. Do not invent a literal query example.

### `DCGM_FI_DEV_POWER_VIOLATION`

- **Category:** Power
- **Purpose:** Measures time spent power throttled.
- **Type:** `Counter`
- **Unit:** Time spent power throttled; exact exposed unit should be verified
  from the datasource before conversion or presentation.
- **Use when:** the user asks whether a GPU has been power throttling; wants
  to detect power throttling over time; or asks about the history/trend of
  power-related throttling.
- **Do not use / confusable with:** current GPU power consumption/draw →
  `DCGM_FI_DEV_POWER_USAGE`.
- **Relevant scope:** GPU (not a label list).
- **Additional known labels:** sourced dynamically at query-generation time from the runtime — see `SKILL.md` §5 Principle 9. No intrinsic dimension beyond the GPU-level scope is documented for this metric.
- **Intent examples:** "Has the GPU been power throttling?", "Detect power
  throttling over the last hour."
- **Edge/confusable example:** user asks for the GPU's current power
  consumption → use `DCGM_FI_DEV_POWER_USAGE`, not this metric.
- **Metric-specific query/result semantics:** this is a Counter representing
  accumulated time spent power throttled.

  **Per-metric override — construction blocked.** The exact exposed unit for
  this counter has not been verified against the live datasource (see Unit
  above). Whether to present this as a raw `increase()`, a `rate()`, or a
  converted duration depends on knowing that unit, so this cannot be decided
  from Prometheus Fundamentals' generic Counter rules alone the way an
  ordinary Counter can. Do NOT construct a query for this metric on assumed
  semantics.

  Resolve any request that selects this metric as `status: "unsupported_metric"`
  instead of `"ok"` — do not build a `query` field at all. The metric was
  correctly identified; only query construction is blocked. State plainly in
  `explanation` that `DCGM_FI_DEV_POWER_VIOLATION` is a Counter whose exposed
  unit is unverified, so no query can be safely constructed until that is
  confirmed against the live datasource.

  This is the concrete case exercised by `SKILL.md` §5 Principle 8 / §6 Step 5:
  "query/result semantics themselves stated as unverified" (not merely "no
  verified example") — construction is blocked here specifically because the
  unit itself is unverified, not because no example exists.

- **Query examples:** no verified DCGM PromQL query example is currently
  available. Do not invent a literal query example.

### `DCGM_FI_DEV_SM_CLOCK`

- **Category:** Clocks
- **Purpose:** Measures the current GPU SM/core clock frequency.
- **Type:** `Gauge`
- **Unit:** MHz
- **Use when:** the user asks for the current GPU clock; GPU core frequency;
  or SM/core clock frequency.
- **Do not use / confusable with:** GPU memory frequency →
  `DCGM_FI_DEV_MEM_CLOCK`.
- **Relevant scope:** GPU (not a label list).
- **Additional known labels:** sourced dynamically at query-generation time from the runtime — see `SKILL.md` §5 Principle 9. No intrinsic dimension beyond the GPU-level scope is documented for this metric.
- **Intent examples:** "What is the current GPU clock?", "What is the GPU core
  frequency?"
- **Edge/confusable example:** user asks for memory clock frequency → use
  `DCGM_FI_DEV_MEM_CLOCK`, not this metric.
- **Metric-specific query/result semantics:** represents the current SM/core
  clock frequency. No per-metric override of `SKILL.md` defaults is currently
  defined.
- **Query examples:** no verified DCGM PromQL query example is currently
  available. Do not invent a literal query example.

### `DCGM_FI_DEV_MEM_CLOCK`

- **Category:** Clocks
- **Purpose:** Measures the current GPU memory clock frequency.
- **Type:** `Gauge`
- **Unit:** MHz
- **Use when:** the user asks for the current GPU memory clock; GPU memory
  frequency; or wants to compare memory clock frequencies across GPUs.
- **Do not use / confusable with:** GPU SM/core clock frequency →
  `DCGM_FI_DEV_SM_CLOCK`.
- **Relevant scope:** GPU memory (not a label list).
- **Additional known labels:** sourced dynamically at query-generation time from the runtime — see `SKILL.md` §5 Principle 9. No intrinsic dimension beyond the GPU-level scope is documented for this metric.
- **Intent examples:** "What is the current GPU memory clock?", "What is the
  GPU memory frequency?"
- **Edge/confusable example:** user asks for the GPU core/SM clock → use
  `DCGM_FI_DEV_SM_CLOCK`, not this metric.
- **Metric-specific query/result semantics:** represents the current GPU
  memory clock frequency. No per-metric override of `SKILL.md` defaults is
  currently defined.
- **Query examples:** no verified DCGM PromQL query example is currently
  available. Do not invent a literal query example.

## Domain-Specific Guardrails

- Do not treat GPU core temperature and GPU memory temperature as the same
  measurement.
- Do not treat GPU power usage and power violation as the same measurement.
- Do not treat SM/core clock and memory clock as the same measurement.
- Do not interpret the power-violation Counter as instantaneous power
  consumption.
- `DCGM_FI_DEV_POWER_VIOLATION` specifically: its exposed unit is unverified
  (see its Metric-Specific Query/Result Semantics above). Never construct a
  query for it — resolve as `unsupported_metric` instead, even though the
  metric itself is correctly identified.
- Do not invent Prometheus label names or label values — label keys must be
  confirmed by the runtime, never assumed from this reference (`SKILL.md` §5
  Principle 9).
- Do not assume a specific GPU, device, or node unless the user provides the
  relevant constraint.
- Do not invent a PromQL expression when the required query semantics have not
  been verified.
