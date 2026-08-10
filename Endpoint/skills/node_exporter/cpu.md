---
parent_exporter: node_exporter
domain_id: cpu
covers: CPU utilization and CPU scheduling activity
metric_count: 3

merged_from:
  - CPU → retained as the CPU functional domain because the vendor taxonomy directly groups CPU utilization and CPU scheduling activity.
  
---

# Node Exporter — CPU

## 1. Domain Fundamentals

This section contains concepts that are true across this functional domain only.

If a concept is true across multiple domains, it belongs in the exporter's
**Exporter Fundamentals** section instead.

---

### 1.1 Common Labels & Dimensions

No domain-common labels beyond the exporter-wide baseline.

The exporter-wide baseline labels are:

- `cluster`
- `instance`
- `job`
- `node_id`

The `node_cpu_seconds_total` metric additionally has CPU-specific
labels documented in its individual metric definition.

---

### 1.2 Confusable Metric Families

#### CPU Utilization vs CPU Scheduling Activity

- `node_cpu_seconds_total`
- `node_context_switches_total`
- `node_intr_total`

Difference:

`node_cpu_seconds_total` measures CPU time spent in different CPU modes and
is used for questions about CPU utilization, busy time, or idle time.

`node_context_switches_total` measures total context switches and represents
scheduling activity.

`node_intr_total` measures total interrupts handled and represents
interrupt activity.

Use:

- CPU utilization / busy / idle → `node_cpu_seconds_total`
- Context-switch activity → `node_context_switches_total`
- Interrupt activity → `node_intr_total`

Do not treat context switches or interrupts as direct measures of CPU
utilization.

---

### 1.3 Domain-Specific Semantic Distinctions

#### CPU Time vs Scheduling / Interrupt Activity

The CPU domain contains both CPU-time measurements and activity counters.

`node_cpu_seconds_total` represents CPU time accumulated across different
CPU modes.

`node_context_switches_total` represents accumulated context-switch activity.

`node_intr_total` represents accumulated interrupt activity.

These measurements are related to CPU activity but answer different questions.
A request about CPU utilization should not be replaced with a context-switch
or interrupt metric merely because all three are CPU-related.

---

## 2. Metric Definitions

### 2.1 `node_cpu_seconds_total`

#### Category

CPU Utilization

#### Purpose

Measures CPU time spent in different modes, including user, system, idle,
iowait, and other CPU modes.

#### Type

`Counter`

#### Unit

CPU time.

#### Use When

- The user asks for CPU utilization.
- The user asks how busy the CPU is.
- The user asks how much CPU time is idle.
- The user asks about CPU time spent in a particular mode.
- The user asks to compare CPU utilization across nodes or CPUs.

#### Do Not Use / Confusable With

- Context-switch activity → `node_context_switches_total`
- Interrupt activity → `node_intr_total`

Do not use context-switch or interrupt counts as substitutes for CPU
utilization.

#### Relevant Scope

CPU.

This is not a label list.

#### Additional Known Labels

- `cpu`
- `mode`

These are in addition to the exporter-wide baseline labels documented in
`index.md` §5.1.

#### Intent Examples

- "What is the CPU utilization?"
- "How busy is the CPU?"
- "How much CPU time is idle?"
- "Show CPU utilization for CPU 2."

#### Edge / Confusable Example (Optional)

> User asks "Are there a lot of context switches?"
>
> Use `node_context_switches_total`, not this metric.

#### Metric-Specific Query / Result Semantics

This is a Counter representing accumulated CPU time across CPU modes.

For utilization or rate-based questions, the appropriate Counter handling
and range-query construction are delegated to the Main Skill and Prometheus
Fundamentals.

The `mode` dimension distinguishes CPU states such as user, system, idle,
and iowait. Preserve an explicitly requested CPU mode when constructing the
query.

No per-metric override of Main Skill defaults is currently defined.

#### Query Examples (Optional)

A verified example from the project is:

```promql
100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[1m])) * 100)
````

This represents CPU utilization based on the idle CPU-time rate.

The query example has been verified against a live Prometheus during project
testing. Label names used in the example should only be used where they have
been verified for the actual datasource.

---

### 2.2 `node_context_switches_total`

#### Category

CPU Scheduling Activity

#### Purpose

Measures the total number of context switches.

#### Type

`Counter`

#### Unit

Total context switches.

#### Use When

* The user asks about context switches.
* The user asks whether scheduling activity is high.
* The user asks about context-switch activity over time.
* The user wants to investigate a workload with heavy scheduling activity.

#### Do Not Use / Confusable With

* CPU utilization or CPU busy/idle time → `node_cpu_seconds_total`
* Interrupt activity → `node_intr_total`

Do not interpret a high context-switch count as a direct CPU-utilization
measurement.

#### Relevant Scope

Node.

This is not a label list.

#### Additional Known Labels

None beyond domain-level labels (see Domain Fundamentals §1.1).

#### Intent Examples

* "How many context switches are happening?"
* "Is there high scheduling activity?"
* "Show context-switch activity over the last hour."

#### Edge / Confusable Example (Optional)

> User asks "How much CPU is being used?"
>
> Use `node_cpu_seconds_total`, not this metric.

#### Metric-Specific Query / Result Semantics

This is a Counter representing accumulated context switches.

For requests about context-switch activity over a time period, the
appropriate Counter handling and range-query construction are delegated to
the Main Skill and Prometheus Fundamentals.

No per-metric override of Main Skill defaults is currently defined.

#### Query Examples (Optional)

No additional verified PromQL query example is currently available.
Do not invent a literal query example.

---

### 2.3 `node_intr_total`

#### Category

CPU Interrupt Activity

#### Purpose

Measures the total number of interrupts handled.

#### Type

`Counter`

#### Unit

Total interrupts handled.

#### Use When

* The user asks about interrupt activity.
* The user asks whether the system is handling a high number of interrupts.
* The user wants to investigate an interrupt-heavy workload.
* The user asks about interrupt activity over time.

#### Do Not Use / Confusable With

* CPU utilization or CPU busy/idle time → `node_cpu_seconds_total`
* Context-switch activity → `node_context_switches_total`

Do not interpret interrupt counts as a direct CPU-utilization measurement.

#### Relevant Scope

Node.

This is not a label list.

#### Additional Known Labels

None beyond domain-level labels (see Domain Fundamentals §1.1).

#### Intent Examples

* "How many interrupts are being handled?"
* "Is this an interrupt-heavy workload?"
* "Show interrupt activity over the last hour."

#### Edge / Confusable Example (Optional)

> User asks "Are there a lot of context switches?"
>
> Use `node_context_switches_total`, not this metric.

#### Metric-Specific Query / Result Semantics

This is a Counter representing accumulated interrupts handled.

For requests about interrupt activity over a time period, the appropriate
Counter handling and range-query construction are delegated to the Main Skill
and Prometheus Fundamentals.

No per-metric override of Main Skill defaults is currently defined.

#### Query Examples (Optional)

No verified PromQL query example is currently available.
Do not invent a literal query example.

---

## 3. Domain-Specific Guardrails (Optional)

* Do not treat CPU utilization, context-switch activity, and interrupt activity
  as interchangeable measurements.
* Do not use `node_context_switches_total` or `node_intr_total` as substitutes
  for CPU utilization.
* Do not assume a specific CPU or node unless the user provides the relevant
  constraint.
* Do not invent Prometheus label names or label values.
* Preserve an explicitly requested CPU mode when selecting
  `node_cpu_seconds_total`.
* Do not invent a PromQL expression when the required query semantics have not
  been verified.


