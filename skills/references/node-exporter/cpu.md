Defines the metrics available for CPU utilization, CPU scheduling and interrupt
activity, and system load under Node Exporter, and the semantics needed to
select and query each one correctly.

## Contents

- Quick Facts
- Domain Fundamentals
- Metric Definitions
  - `node_cpu_seconds_total`
  - `node_context_switches_total`
  - `node_intr_total`
  - `node_load1`
  - `node_load5`
  - `node_load15`
- Domain-Specific Guardrails

## Quick Facts

- **Parent exporter:** node-exporter
- **Domain:** cpu
- **Covers:** CPU utilization, CPU scheduling and interrupt activity, and
  system load
- **Metric count:** 6
- **Merged from:** CPU — retained as the CPU functional domain because the
  vendor taxonomy directly groups CPU utilization and CPU scheduling activity.
  Load — merged into the CPU functional domain per the approved Phase 1 domain
  design: load average is the measurement most likely to be confused with CPU
  utilization in a user's wording, so it is kept in the same domain file to be
  resolved directly in Confusable Measurements below, rather than requiring
  routing-time disambiguation across two separate domains.

## Domain Fundamentals

Concepts true across this functional domain only. A concept true across multiple
domains belongs in `overview.md`'s Exporter Fundamentals instead.

### Common Labels & Dimensions

Label keys for this domain's metrics are sourced dynamically at query-generation
time — see `SKILL.md` §5 Principle 9 and `overview.md` § Entity Scope Baseline.
This reference does not enumerate a static label-key catalog. One dimension is
worth documenting as semantic knowledge regardless: `node_cpu_seconds_total`
varies by CPU and by mode/state (e.g. user, system, idle, iowait) — this is a
fact about what the metric represents, not a claim about the exact label key
names exposed by any particular datasource; confirm the exact keys against
runtime-supplied metric metadata before using them in a query. The other
metrics in this domain (`node_context_switches_total`, `node_intr_total`,
`node_load1`/`5`/`15`) have no additional documented dimension beyond the
node-level entity scope.

### Confusable Measurements

**CPU Utilization vs. CPU Scheduling vs. Interrupt Activity** — all three metrics
in this domain are CPU-related but answer different questions, and none
substitutes for another:

| Metric | What it measures | Use for |
|---|---|---|
| `node_cpu_seconds_total` | CPU time accumulated across CPU modes (user, system, idle, iowait, ...) | CPU utilization, busy time, idle time |
| `node_context_switches_total` | Accumulated context-switch count | Scheduling activity |
| `node_intr_total` | Accumulated interrupts handled | Interrupt activity |

A request about CPU utilization must not be answered with a context-switch or
interrupt metric merely because all three are CPU-related, and vice versa.

**CPU Utilization vs. System Load:**

| Metric | What it measures | Use for |
|---|---|---|
| `node_cpu_seconds_total` | CPU time accumulated across CPU modes | CPU utilization, busy time, idle time |
| `node_load1` / `node_load5` / `node_load15` | System load average over a 1/5/15-minute window | System load, whether the system is overloaded |

`node_cpu_seconds_total` is a Counter accumulating CPU time by mode; the load
average metrics are Gauges representing a windowed average figure. The
authoritative reference material documents these as separate categories (CPU
vs. Load) and gives no further explanation of what specifically composes the
load-average figure (e.g. which process states are counted) beyond calling it
a "load average" — do not assert a composition or mechanism for load average
beyond what is documented here; treat it as a distinct, separately-defined
measurement from CPU utilization rather than a component or synonym of it.

**`node_load1` vs. `node_load5` vs. `node_load15`:**

| Metric | What it measures | Use for |
|---|---|---|
| `node_load1` | 1-minute load average | "Is the system overloaded [right now]?" |
| `node_load5` | 5-minute load average | Sustained system load |
| `node_load15` | 15-minute load average | Long-term load trend |

The three differ only in averaging window per the authoritative material.
Preserve an explicitly requested window; if the user asks for "load" without
specifying a window, this is metric-ambiguous across the three per `SKILL.md`
§6 Step 3f, not resolvable by an assumed default window.

## Metric Definitions

### `node_cpu_seconds_total`

- **Category:** CPU Utilization
- **Purpose:** Measures CPU time spent in different modes, including user,
  system, idle, iowait, and other CPU modes.
- **Type:** `Counter`
- **Unit:** CPU time.
- **Use when:** the user asks for CPU utilization; how busy the CPU is; how much
  CPU time is idle; CPU time in a particular mode; or to compare CPU utilization
  across nodes or CPUs.
- **Do not use / confusable with:** context-switch activity →
  `node_context_switches_total`; interrupt activity → `node_intr_total`. Do not
  use context-switch or interrupt counts as substitutes for CPU utilization.
- **Relevant scope:** CPU (not a label list).
- **Additional known labels:** this metric varies by CPU and mode/state as a
  documented semantic dimension (see Common Labels & Dimensions above) — the
  exact label key names for those dimensions are sourced dynamically from the
  runtime at query-generation time (`SKILL.md` §5 Principle 9), not treated as
  a static verified catalog here.
- **Intent examples:** "What is the CPU utilization?", "How busy is the CPU?",
  "How much CPU time is idle?", "Show CPU utilization for CPU 2."
- **Edge/confusable example:** user asks "Are there a lot of context switches?"
  → use `node_context_switches_total`, not this metric.
- **Metric-specific query/result semantics:** a Counter representing
  accumulated CPU time across CPU modes. Counter handling and range-query
  construction follow `SKILL.md` and `prometheus-fundamentals.md`. The `mode`
  dimension distinguishes CPU states (user/system/idle/iowait); preserve an
  explicitly requested mode when constructing the query. No per-metric override
  of `SKILL.md` defaults is currently defined.
- **Query examples:** a verified example from the project:

  ```promql
  100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[1m])) * 100)
  ```

  This represents CPU utilization based on the idle CPU-time rate. Verified
  against a live Prometheus during project testing. Label names used in the
  example should only be reused where they've been verified for the actual
  datasource.

### `node_context_switches_total`

- **Category:** CPU Scheduling Activity
- **Purpose:** Measures the total number of context switches.
- **Type:** `Counter`
- **Unit:** Total context switches.
- **Use when:** the user asks about context switches; whether scheduling
  activity is high; context-switch activity over time; or wants to investigate
  a workload with heavy scheduling activity.
- **Do not use / confusable with:** CPU utilization or busy/idle time →
  `node_cpu_seconds_total`; interrupt activity → `node_intr_total`. Do not
  interpret a high context-switch count as a direct CPU-utilization measurement.
- **Relevant scope:** Node (not a label list).
- **Additional known labels:** no additional dimension beyond the node-level entity scope is documented for this metric. Label keys are sourced dynamically from the runtime at query-generation time (`SKILL.md` §5 Principle 9) — do not invent one.
- **Intent examples:** "How many context switches are happening?", "Is there
  high scheduling activity?", "Show context-switch activity over the last hour."
- **Edge/confusable example:** user asks "How much CPU is being used?" → use
  `node_cpu_seconds_total`, not this metric.
- **Metric-specific query/result semantics:** a Counter representing
  accumulated context switches. Counter handling and range-query construction
  follow `SKILL.md` and `prometheus-fundamentals.md`. No per-metric override of
  `SKILL.md` defaults is currently defined.
- **Query examples:** no verified PromQL query example is currently available.
  Do not invent a literal query example.

### `node_intr_total`

- **Category:** CPU Interrupt Activity
- **Purpose:** Measures the total number of interrupts handled.
- **Type:** `Counter`
- **Unit:** Total interrupts handled.
- **Use when:** the user asks about interrupt activity; whether the system is
  handling a high number of interrupts; wants to investigate an interrupt-heavy
  workload; or asks about interrupt activity over time.
- **Do not use / confusable with:** CPU utilization or busy/idle time →
  `node_cpu_seconds_total`; context-switch activity →
  `node_context_switches_total`. Do not interpret interrupt counts as a direct
  CPU-utilization measurement.
- **Relevant scope:** Node (not a label list).
- **Additional known labels:** no additional dimension beyond the node-level entity scope is documented for this metric. Label keys are sourced dynamically from the runtime at query-generation time (`SKILL.md` §5 Principle 9) — do not invent one.
- **Intent examples:** "How many interrupts are being handled?", "Is this an
  interrupt-heavy workload?", "Show interrupt activity over the last hour."
- **Edge/confusable example:** user asks "Are there a lot of context switches?"
  → use `node_context_switches_total`, not this metric.
- **Metric-specific query/result semantics:** a Counter representing
  accumulated interrupts handled. Counter handling and range-query construction
  follow `SKILL.md` and `prometheus-fundamentals.md`. No per-metric override of
  `SKILL.md` defaults is currently defined.
- **Query examples:** no verified PromQL query example is currently available.
  Do not invent a literal query example.

### `node_load1`

- **Category:** System Load
- **Purpose:** Measures the 1-minute load average.
- **Type:** `Gauge`
- **Unit:** Not stated in the authoritative document. Do not assume a specific
  unit or presentation without verification against the datasource.
- **Use when:** the user asks whether the system is overloaded right now; for
  the current/short-term load average; or for the 1-minute load figure
  specifically.
- **Do not use / confusable with:** CPU utilization (a different measurement —
  see Confusable Measurements above) → `node_cpu_seconds_total`; sustained
  load → `node_load5`; long-term load trend → `node_load15`.
- **Relevant scope:** Node (not a label list).
- **Additional known labels:** no additional dimension beyond the node-level
  entity scope is documented for this metric. Label keys (including the
  node-level scope itself) are sourced dynamically from the runtime at
  query-generation time (`SKILL.md` §5 Principle 9) — do not invent one.
- **Intent examples:** "Is the system overloaded?", "What's the current load
  average?"
- **Edge/confusable example:** user asks about sustained load over a longer
  window → use `node_load5` or `node_load15`, not this metric.
- **Metric-specific query/result semantics:** represents the 1-minute load
  average as a Gauge. What specifically composes the load figure is not
  established by the authoritative document — do not assert a mechanism (e.g.
  which process states are counted) beyond what is documented. No per-metric
  override of `SKILL.md` defaults is currently defined.
- **Query examples:** no verified PromQL query example is currently available.
  Do not invent a literal query example.

### `node_load5`

- **Category:** System Load
- **Purpose:** Measures the 5-minute load average.
- **Type:** `Gauge`
- **Unit:** Not stated in the authoritative document. Do not assume a specific
  unit or presentation without verification against the datasource.
- **Use when:** the user asks about sustained system load; the 5-minute load
  figure specifically; or a mid-range load trend.
- **Do not use / confusable with:** CPU utilization (a different measurement —
  see Confusable Measurements above) → `node_cpu_seconds_total`; the
  short-term/current load → `node_load1`; the long-term trend → `node_load15`.
- **Relevant scope:** Node (not a label list).
- **Additional known labels:** no additional dimension beyond the node-level
  entity scope is documented for this metric. Label keys (including the
  node-level scope itself) are sourced dynamically from the runtime at
  query-generation time (`SKILL.md` §5 Principle 9) — do not invent one.
- **Intent examples:** "What's the sustained load?", "What's the 5-minute load
  average?"
- **Edge/confusable example:** user asks for the current, right-now load → use
  `node_load1`, not this metric.
- **Metric-specific query/result semantics:** represents the 5-minute load
  average as a Gauge. What specifically composes the load figure is not
  established by the authoritative document — do not assert a mechanism
  beyond what is documented. No per-metric override of `SKILL.md` defaults is
  currently defined.
- **Query examples:** no verified PromQL query example is currently available.
  Do not invent a literal query example.

### `node_load15`

- **Category:** System Load
- **Purpose:** Measures the 15-minute load average.
- **Type:** `Gauge`
- **Unit:** Not stated in the authoritative document. Do not assume a specific
  unit or presentation without verification against the datasource.
- **Use when:** the user asks about the long-term load trend; the 15-minute
  load figure specifically; or a longer-window load comparison.
- **Do not use / confusable with:** CPU utilization (a different measurement —
  see Confusable Measurements above) → `node_cpu_seconds_total`; the
  short-term/current load → `node_load1`; sustained load → `node_load5`.
- **Relevant scope:** Node (not a label list).
- **Additional known labels:** no additional dimension beyond the node-level
  entity scope is documented for this metric. Label keys (including the
  node-level scope itself) are sourced dynamically from the runtime at
  query-generation time (`SKILL.md` §5 Principle 9) — do not invent one.
- **Intent examples:** "What's the long-term load trend?", "What's the
  15-minute load average?"
- **Edge/confusable example:** user asks for the current, right-now load → use
  `node_load1`, not this metric.
- **Metric-specific query/result semantics:** represents the 15-minute load
  average as a Gauge. What specifically composes the load figure is not
  established by the authoritative document — do not assert a mechanism
  beyond what is documented. No per-metric override of `SKILL.md` defaults is
  currently defined.
- **Query examples:** no verified PromQL query example is currently available.
  Do not invent a literal query example.

## Domain-Specific Guardrails

- Do not treat CPU utilization, context-switch activity, and interrupt activity
  as interchangeable measurements.
- Do not use `node_context_switches_total` or `node_intr_total` as substitutes
  for CPU utilization.
- Do not treat system load average as equivalent to, or a synonym for, CPU
  utilization — see Confusable Measurements above.
- Do not treat `node_load1`, `node_load5`, and `node_load15` as
  interchangeable; preserve the explicitly requested averaging window, and
  treat an unspecified window as metric-ambiguous rather than defaulting to
  one.
- Do not assert a mechanism or composition for the load-average figure (e.g.
  which process states contribute to it) beyond what the authoritative
  document states.
- Do not invent a unit for `node_load1`/`5`/`15` — none is established by the
  authoritative document.
- Do not assume a specific CPU or node unless the user provides the relevant
  constraint.
- Do not invent Prometheus label names or label values — label keys must be
  confirmed by the runtime, never assumed from this reference (`SKILL.md` §5
  Principle 9).
- Preserve an explicitly requested CPU mode when selecting
  `node_cpu_seconds_total`.
- Do not invent a PromQL expression when the required query semantics have not
  been verified.
