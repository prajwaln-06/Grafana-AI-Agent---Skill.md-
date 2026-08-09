---
parent_exporter: dcgm_exporter
domain_id: thermal
covers: GPU temperature, power, and clock operating state
metric_count: 6

merged_from:
  - Temperature → retained as part of the broader Thermal functional domain because it describes GPU and GPU-memory thermal state.
  - Power → merged into Thermal because power consumption and power-related throttling are directly relevant to GPU operating/thermal state.
  - Clocks → merged into Thermal because GPU operating frequencies provide context about the GPU's current operating state alongside temperature and power.

---

# DCGM Exporter — Thermal

## 1. Domain Fundamentals

This section contains concepts that are true across this functional domain only.

If a concept is true across multiple domains, it belongs in the exporter's
**Exporter Fundamentals** section instead.

---

### 1.1 Common Labels & Dimensions

No domain-common labels beyond the exporter-wide baseline.

---

### 1.2 Confusable Metric Families

#### GPU Core Temperature vs GPU Memory Temperature

- `DCGM_FI_DEV_GPU_TEMP`
- `DCGM_FI_DEV_MEMORY_TEMP`

Difference:

`DCGM_FI_DEV_GPU_TEMP` measures GPU core temperature, while
`DCGM_FI_DEV_MEMORY_TEMP` measures HBM/VRAM memory temperature.

Use:

- GPU/core/general GPU temperature → `DCGM_FI_DEV_GPU_TEMP`
- GPU memory/HBM/VRAM temperature → `DCGM_FI_DEV_MEMORY_TEMP`

---

#### GPU Core Clock vs GPU Memory Clock

- `DCGM_FI_DEV_SM_CLOCK`
- `DCGM_FI_DEV_MEM_CLOCK`

Difference:

`DCGM_FI_DEV_SM_CLOCK` measures the SM/core clock, while
`DCGM_FI_DEV_MEM_CLOCK` measures the GPU memory clock.

Use:

- GPU/core/SM frequency → `DCGM_FI_DEV_SM_CLOCK`
- GPU memory frequency → `DCGM_FI_DEV_MEM_CLOCK`

---

#### Power Usage vs Power Violation

- `DCGM_FI_DEV_POWER_USAGE`
- `DCGM_FI_DEV_POWER_VIOLATION`

Difference:

`DCGM_FI_DEV_POWER_USAGE` measures instantaneous GPU power draw, while
`DCGM_FI_DEV_POWER_VIOLATION` records time spent under power throttling.

Use:

- Current GPU power consumption/draw → `DCGM_FI_DEV_POWER_USAGE`
- Power throttling over time → `DCGM_FI_DEV_POWER_VIOLATION`

---

### 1.3 Domain-Specific Semantic Distinctions

#### Current Operating State vs Throttling History

This domain contains both current-state measurements and a cumulative
throttling measurement.

Current-state measurements include:

- GPU temperature
- GPU memory temperature
- GPU power usage
- SM/core clock
- memory clock

`DCGM_FI_DEV_POWER_VIOLATION` is different: it is a Counter representing
time spent power throttled and is intended for detecting power throttling
over time.

Do not treat the power-violation counter as the current power consumption
value.

---

## 2. Metric Definitions

### 2.1 `DCGM_FI_DEV_GPU_TEMP`

#### Category

Temperature

#### Purpose

Measures GPU core temperature.

#### Type

`Gauge`

#### Unit

Degrees Celsius (°C)

#### Use When

- The user asks for GPU temperature.
- The user asks for GPU core temperature.
- The user asks whether a GPU is overheating or running hot.
- The user wants to compare GPU core temperatures.

#### Do Not Use / Confusable With

- GPU memory/HBM/VRAM temperature →
  `DCGM_FI_DEV_MEMORY_TEMP`

#### Relevant Scope

GPU.

This is not a label list.

#### Additional Known Labels

None beyond domain-level labels (see Domain Fundamentals §1.1).

#### Intent Examples

- "What is the GPU temperature?"
- "Is GPU 2 running hot?"

#### Edge / Confusable Example (Optional)

> User specifically asks about HBM or VRAM temperature.
>
> Use `DCGM_FI_DEV_MEMORY_TEMP`, not this metric.

#### Metric-Specific Query / Result Semantics

The metric directly represents GPU core temperature.

No per-metric override of Main Skill defaults is currently defined.

#### Query Examples (Optional)

No verified DCGM PromQL query example is currently available.
Do not invent a literal query example.


### 2.2 `DCGM_FI_DEV_MEMORY_TEMP`

#### Category

Temperature

#### Purpose

Measures HBM/VRAM GPU memory temperature.

#### Type

`Gauge`

#### Unit

Degrees Celsius (°C)

#### Use When

- The user asks for GPU memory temperature.
- The user asks about HBM temperature.
- The user asks about VRAM temperature.
- The user asks whether GPU memory is overheating or running hot.

#### Do Not Use / Confusable With

- GPU core or general GPU temperature →
  `DCGM_FI_DEV_GPU_TEMP`

#### Relevant Scope

GPU memory / HBM / VRAM.

This is not a label list.

#### Additional Known Labels

None beyond domain-level labels (see Domain Fundamentals §1.1).

#### Intent Examples

- "What is the GPU memory temperature?"
- "Is the HBM getting too hot?"

#### Edge / Confusable Example (Optional)

> User asks for general GPU/core temperature.
>
> Use `DCGM_FI_DEV_GPU_TEMP`, not this metric.

#### Metric-Specific Query / Result Semantics

The metric represents GPU memory/HBM temperature rather than GPU core
temperature.

No per-metric override of Main Skill defaults is currently defined.

#### Query Examples (Optional)

No verified DCGM PromQL query example is currently available.
Do not invent a literal query example.


### 2.3 `DCGM_FI_DEV_POWER_USAGE`

#### Category

Power

#### Purpose

Measures instantaneous GPU power draw.

#### Type

`Gauge`

#### Unit

Watts (W)

#### Use When

- The user asks for current GPU power consumption.
- The user asks how much power a GPU is drawing.
- The user wants to compare GPU power usage.

#### Do Not Use / Confusable With

- Power throttling over time →
  `DCGM_FI_DEV_POWER_VIOLATION`

#### Relevant Scope

GPU.

This is not a label list.

#### Additional Known Labels

None beyond domain-level labels (see Domain Fundamentals §1.1).

#### Intent Examples

- "How much power is the GPU using?"
- "What is the current GPU power draw?"

#### Edge / Confusable Example (Optional)

> User asks whether the GPU has been power throttling.
>
> Use `DCGM_FI_DEV_POWER_VIOLATION`, not this metric.

#### Metric-Specific Query / Result Semantics

The metric represents instantaneous GPU power draw.

No per-metric override of Main Skill defaults is currently defined.

#### Query Examples (Optional)

No verified DCGM PromQL query example is currently available.
Do not invent a literal query example.


### 2.4 `DCGM_FI_DEV_POWER_VIOLATION`

#### Category

Power

#### Purpose

Measures time spent power throttled.

#### Type

`Counter`

#### Unit

Time spent power throttled; exact exposed unit should be verified from the
datasource before conversion or presentation.

#### Use When

- The user asks whether a GPU has been power throttling.
- The user wants to detect power throttling over time.
- The user asks about the history/trend of power-related throttling.

#### Do Not Use / Confusable With

- Current GPU power consumption/draw →
  `DCGM_FI_DEV_POWER_USAGE`

#### Relevant Scope

GPU.

This is not a label list.

#### Additional Known Labels

None beyond domain-level labels (see Domain Fundamentals §1.1).

#### Intent Examples

- "Has the GPU been power throttling?"
- "Detect power throttling over the last hour."

#### Edge / Confusable Example (Optional)

> User asks for the GPU's current power consumption.
>
> Use `DCGM_FI_DEV_POWER_USAGE`, not this metric.

#### Metric-Specific Query / Result Semantics

This is a Counter representing accumulated time spent power throttled.

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

#### Query Examples (Optional)

No verified DCGM PromQL query example is currently available.
Do not invent a literal query example.


### 2.5 `DCGM_FI_DEV_SM_CLOCK`

#### Category

Clocks

#### Purpose

Measures the current GPU SM/core clock frequency.

#### Type

`Gauge`

#### Unit

MHz

#### Use When

- The user asks for the current GPU clock.
- The user asks for GPU core frequency.
- The user asks for SM/core clock frequency.

#### Do Not Use / Confusable With

- GPU memory frequency →
  `DCGM_FI_DEV_MEM_CLOCK`

#### Relevant Scope

GPU.

This is not a label list.

#### Additional Known Labels

None beyond domain-level labels (see Domain Fundamentals §1.1).

#### Intent Examples

- "What is the current GPU clock?"
- "What is the GPU core frequency?"

#### Edge / Confusable Example (Optional)

> User asks for memory clock frequency.
>
> Use `DCGM_FI_DEV_MEM_CLOCK`, not this metric.

#### Metric-Specific Query / Result Semantics

The metric represents the current SM/core clock frequency.

No per-metric override of Main Skill defaults is currently defined.

#### Query Examples (Optional)

No verified DCGM PromQL query example is currently available.
Do not invent a literal query example.


### 2.6 `DCGM_FI_DEV_MEM_CLOCK`

#### Category

Clocks

#### Purpose

Measures the current GPU memory clock frequency.

#### Type

`Gauge`

#### Unit

MHz

#### Use When

- The user asks for the current GPU memory clock.
- The user asks for GPU memory frequency.
- The user wants to compare memory clock frequencies across GPUs.

#### Do Not Use / Confusable With

- GPU SM/core clock frequency →
  `DCGM_FI_DEV_SM_CLOCK`

#### Relevant Scope

GPU memory.

This is not a label list.

#### Additional Known Labels

None beyond domain-level labels (see Domain Fundamentals §1.1).

#### Intent Examples

- "What is the current GPU memory clock?"
- "What is the GPU memory frequency?"

#### Edge / Confusable Example (Optional)

> User asks for the GPU core/SM clock.
>
> Use `DCGM_FI_DEV_SM_CLOCK`, not this metric.

#### Metric-Specific Query / Result Semantics

The metric represents the current GPU memory clock frequency.

No per-metric override of Main Skill defaults is currently defined.

#### Query Examples (Optional)

No verified DCGM PromQL query example is currently available.
Do not invent a literal query example.

---

## 3. Domain-Specific Guardrails (Optional)

- Do not treat GPU core temperature and GPU memory temperature as the same measurement.
- Do not treat GPU power usage and power violation as the same measurement.
- Do not treat SM/core clock and memory clock as the same measurement.
- Do not interpret the power-violation Counter as instantaneous power consumption.
- `DCGM_FI_DEV_POWER_VIOLATION` specifically: its exposed unit is unverified (see §2.4). Never construct a query
  for it — resolve as `unsupported_metric` instead, even though the metric itself is correctly identified.
- Do not invent Prometheus label names or label values.
- Do not assume a specific GPU, device, or node unless the user provides the relevant constraint.
- Do not invent a PromQL expression when the required query semantics have not been verified.