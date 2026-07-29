---
name: node_exporter
description: Node Exporter metrics for monitoring CPU activity, operating-system load, system memory, filesystem capacity, and host resource utilization through Prometheus.
data_source: prometheus
version: 1.0
---

# Node Exporter Metrics Sub-Skill

## 1. File-Level Routing

### Purpose

This sub-skill interprets user requests related to host and operating-system observability using metrics exposed by the Prometheus Node Exporter.

It covers CPU activity, operating-system load, system memory, filesystem capacity, storage usage, and other host resource measurements represented by the metrics defined in this sub-skill.

### Trigger Examples

Examples of user requests that should route to this sub-skill:

- "Show CPU utilization."
- "How much memory is available?"
- "Show available RAM."
- "Show available disk space."
- "How large is the filesystem?"
- "Show load average."
- "Compare CPU utilization across all servers."

These examples illustrate intent and are not an exhaustive whitelist.

### Do Not Use

Do not use this sub-skill for:

- GPU utilization or GPU hardware metrics -> `dcgm_exporter`
- Application or service logs -> appropriate OpenSearch sub-skill
- Application-specific metrics not exposed by Node Exporter

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
   - node
   - CPU
   - filesystem
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
| CPU | CPU utilization / CPU busy / CPU idle | `node_cpu_seconds_total` |
| Load | 1-minute operating-system load average | `node_load1` |
| Memory | Available system memory | `node_memory_MemAvailable_bytes` |
| Filesystem | Available filesystem space | `node_filesystem_avail_bytes` |
| Filesystem | Total filesystem capacity | `node_filesystem_size_bytes` |
| Load | 5-minute operating-system load average | `node_load5` |
| Load | 15-minute operating-system load average | `node_load15` |
| CPU | Context-switch / scheduling activity | `node_context_switches_total` |
| CPU | Interrupt activity | `node_intr_total` |
| Memory | Total physical memory | `node_memory_MemTotal_bytes` |
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

This section contains concepts that apply across multiple Node Exporter metrics but are not general query-language rules.

Do not duplicate general Counter/Gauge handling, range-vector behavior, aggregation, time-window handling, or shared query-construction rules from the main skill.

### 5.1 Entity Scope and Dimensions

The metrics in this sub-skill describe host and operating-system resources.

When the user explicitly specifies a host, instance, node, or collection of hosts, preserve that scope during metric selection.

Examples include:

- a specific host;
- multiple hosts;
- all monitored hosts.

Do not invent host or instance constraints that the user did not provide.

Concrete label names should only be documented after they have been verified from the actual datasource/schema.

---

### 5.2 Metric Ambiguity vs Parameter Vagueness

Request clarification only when the **requested measurement itself cannot be determined**.

Examples of metric ambiguity include:

- "Show CPU."
- "Show memory."
- "Show disk."

These requests may correspond to multiple semantically distinct measurements supported by this sub-skill.

By contrast, requests such as:

- "Show CPU utilization."
- "How much memory is available?"
- "Show the 1-minute load average."

identify a single measurement even if they omit details such as host selection or time range.

Do not classify a request as metric-ambiguous merely because query parameters or scope details were omitted.

If the requested measurement can still be identified confidently, select the appropriate metric and defer handling of missing parameters to the main skill.

---

### 5.3 Confusable Metric Families

#### CPU Activity and Operating-System Load

CPU utilization and operating-system load describe different aspects of overall system activity.

These measurements are not interchangeable.

Examples include:

- CPU utilization;
- CPU time;
- operating-system load average.

A broad request such as:

> "Show CPU."

may require clarification because multiple CPU-related measurements could reasonably satisfy the request.

Where the user's wording clearly identifies CPU utilization or load average, select only that measurement.

Node Exporter also exposes lower-level kernel activity counters — context switches (`node_context_switches_total`) and interrupts (`node_intr_total`) — representing scheduler and interrupt-handling activity respectively. These are distinct from both CPU utilization and load average, and from each other. They are typically requested using specific technical language ("context switching," "interrupt activity") rather than generic phrasing like "show CPU."

#### Load-Average Time Windows

Node Exporter exposes operating-system load average at three time windows: 1-minute (`node_load1`), 5-minute (`node_load5`), and 15-minute (`node_load15`). These represent the same underlying measurement sampled over different windows — not three materially different measurements in the way CPU utilization and load average are different from each other.

Select a specific window when the user's wording indicates one:

- immediate/current load -> `node_load1`
- sustained load over several minutes -> `node_load5`
- long-term or trend-oriented load -> `node_load15`

A bare request such as "Show the load average." with no further qualifier should be treated as ambiguous among the three windows, since each is a separately exposed metric rather than a parameter of a single metric.

---

### 5.4 Memory Measurements

Memory measurements represent different aspects of system memory.

This sub-skill currently supports:

- available system memory;
- total physical memory.

These measurements are semantically distinct.

When the requested memory measurement is clear, select the corresponding metric.

If the user explicitly requests a memory measurement that is not represented by this sub-skill, classify the request as unsupported.

When the request simply asks for "memory" without indicating the intended measurement, and multiple memory measurements are supported by this sub-skill, request clarification rather than assuming which measurement is desired.

---

### 5.5 Filesystem Measurements

Filesystem metrics represent different measurements of storage resources.

Examples include:

- available filesystem space;
- total filesystem capacity.

These measurements are semantically distinct.

When the requested filesystem measurement is clear, select the corresponding metric.

If the user explicitly requests a filesystem measurement that is not represented by this sub-skill, classify the request as unsupported.

When the request simply asks for "disk" or "storage" and multiple filesystem measurements are supported by this sub-skill, request clarification.

---

## 6. Metric Definitions

### 6.1 `node_cpu_seconds_total`

**Category:**  
CPU

**Purpose:**  
Measures CPU time spent in different execution modes (user, system, idle, iowait, etc.).

**Type:**  
Counter

**Unit:**  
Seconds

**Use When:**
- The user requests CPU utilization.
- The user wants to know whether the CPU is busy or idle.
- The user wants to monitor CPU activity over time.

**Do Not Use / Confusable With:**
- Operating-system load average -> `node_load1`
- Context-switch activity -> `node_context_switches_total`
- Interrupt activity -> `node_intr_total`

For broader distinctions between CPU-related measurements, see **Local Fundamentals → Confusable Metric Families**.

**Relevant Scope / Dimensions:**
- Host

**Known Labels:**

Not yet verified from the available datasource/schema.

Do not infer or invent label names.

**Intent Examples:**
- "Show CPU utilization."
- "How busy is the CPU?"

**Edge / Confusable Examples:**
- "Show CPU." -> `AMBIGUOUS`
- "Show load average." -> `node_load1`
- "Show CPU utilization." -> `node_cpu_seconds_total`
- "Show CPU utilization and available memory." -> Select `node_cpu_seconds_total` and `node_memory_MemAvailable_bytes`.

**Metric-Specific Query / Result Semantics:**

This Counter represents cumulative CPU time spent in different execution modes.

The raw Counter value does not directly represent CPU utilization.

Meaningful user-facing measurements derived from this metric require interpretation over a time interval.

The exact query construction and interpretation should follow the shared Counter handling defined by the main skill.

---

### 6.2 `node_load1`

**Category:**  
Load

**Purpose:**  
Measures the 1-minute operating-system load average.

**Type:**  
Gauge

**Unit:**  
Load average

**Use When:**
- The user requests the current 1-minute load average.
- The user wants to know whether the system is overloaded.
- The user wants to monitor short-term system load.

**Do Not Use / Confusable With:**
- CPU utilization -> `node_cpu_seconds_total`
- 5-minute load average -> `node_load5`
- 15-minute load average -> `node_load15`

For broader distinctions between CPU-related measurements, see **Local Fundamentals → Confusable Metric Families**.

**Relevant Scope / Dimensions:**
- Host

**Known Labels:**

Not yet verified from the available datasource/schema.

Do not infer or invent label names.

**Intent Examples:**
- "Show the load average."
- "Is the system overloaded?"

**Edge / Confusable Examples:**
- "Show CPU." -> `AMBIGUOUS`
- "Show CPU utilization." -> `node_cpu_seconds_total`
- "Show load average." -> `node_load1`
- "Show load average and available disk space." -> Select `node_load1` and `node_filesystem_avail_bytes`.

**Metric-Specific Query / Result Semantics:**

The raw Gauge value directly represents the requested measurement.

The resulting value represents the current 1-minute operating-system load average.

No additional metric-specific transformation is specified by the project reference.

---

### 6.3 `node_memory_MemAvailable_bytes`

**Category:**  
Memory

**Purpose:**  
Measures the amount of system memory available without swapping.

**Type:**  
Gauge

**Unit:**  
Bytes

**Use When:**
- The user requests available memory.
- The user wants to know how much memory remains available without swapping.
- The user wants to monitor available system memory.

**Do Not Use / Confusable With:**
- Total physical memory -> `node_memory_MemTotal_bytes`

For broader distinctions between memory measurements, see **Local Fundamentals → Memory Measurements**.

**Relevant Scope / Dimensions:**
- Host

**Known Labels:**

Not yet verified from the available datasource/schema.

Do not infer or invent label names.

**Intent Examples:**
- "How much memory is available?"
- "Show available RAM."

**Edge / Confusable Examples:**
- "Show memory." -> `AMBIGUOUS`
- "Show available RAM." -> `node_memory_MemAvailable_bytes`
- "Show available disk space." -> `node_filesystem_avail_bytes`
- "Show CPU utilization and available memory." -> Select `node_cpu_seconds_total` and `node_memory_MemAvailable_bytes`.

**Metric-Specific Query / Result Semantics:**

The raw Gauge value directly represents the requested measurement.

The resulting value represents the amount of system memory available without swapping, expressed in bytes.

No additional metric-specific transformation is specified by the project reference.

---

### 6.4 `node_filesystem_avail_bytes`

**Category:**  
Filesystem

**Purpose:**  
Measures the amount of filesystem space available to non-root users.

**Type:**  
Gauge

**Unit:**  
Bytes

**Use When:**
- The user requests available disk space.
- The user wants to know how much filesystem space remains available.
- The user wants to monitor available storage capacity.

**Do Not Use / Confusable With:**
- Total filesystem capacity -> `node_filesystem_size_bytes`

For broader distinctions between filesystem measurements, see **Local Fundamentals → Filesystem Measurements**.

**Relevant Scope / Dimensions:**
- Host
- Filesystem

**Known Labels:**

Not yet verified from the available datasource/schema.

Do not infer or invent label names.

**Intent Examples:**
- "Show available disk space."
- "How much storage is still available?"

**Edge / Confusable Examples:**
- "Show disk." -> `AMBIGUOUS`
- "How large is the filesystem?" -> `node_filesystem_size_bytes`
- "Show available disk space." -> `node_filesystem_avail_bytes`
- "Show available disk space and load average." -> Select `node_filesystem_avail_bytes` and `node_load1`.

**Metric-Specific Query / Result Semantics:**

The raw Gauge value directly represents the requested measurement.

The resulting value represents the amount of filesystem space currently available to non-root users, expressed in bytes.

No additional metric-specific transformation is specified by the project reference.

---

### 6.5 `node_filesystem_size_bytes`

**Category:**  
Filesystem

**Purpose:**  
Measures the total capacity of a filesystem.

**Type:**  
Gauge

**Unit:**  
Bytes

**Use When:**
- The user requests total filesystem capacity.
- The user wants to know the size of a filesystem.
- The user wants to compare filesystem capacities.

**Do Not Use / Confusable With:**
- Available filesystem space -> `node_filesystem_avail_bytes`

For broader distinctions between filesystem measurements, see **Local Fundamentals → Filesystem Measurements**.

**Relevant Scope / Dimensions:**
- Host
- Filesystem

**Known Labels:**

Not yet verified from the available datasource/schema.

Do not infer or invent label names.

**Intent Examples:**
- "Show total disk capacity."
- "How large is the filesystem?"

**Edge / Confusable Examples:**
- "Show storage." -> `AMBIGUOUS`
- "How much disk space is available?" -> `node_filesystem_avail_bytes`
- "Show total filesystem capacity." -> `node_filesystem_size_bytes`
- "Show total filesystem capacity and available disk space." -> Select `node_filesystem_size_bytes` and `node_filesystem_avail_bytes`.

**Metric-Specific Query / Result Semantics:**

The raw Gauge value directly represents the requested measurement.

The resulting value represents the total filesystem capacity, expressed in bytes.

No additional metric-specific transformation is specified by the project reference.

### 6.6 `node_load5`

**Category:**  
Load

**Purpose:**  
Measures the 5-minute operating-system load average.

**Type:**  
Gauge

**Unit:**  
Load average

**Use When:**
- The user requests the 5-minute load average.
- The user wants to know about sustained system load over several minutes.
- The user wants a smoothed view of load beyond the immediate moment.

**Do Not Use / Confusable With:**
- 1-minute load average -> `node_load1`
- 15-minute load average -> `node_load15`
- CPU utilization -> `node_cpu_seconds_total`

For broader distinctions between load-average windows, see **Local Fundamentals → Confusable Metric Families → Load-Average Time Windows**.

**Relevant Scope / Dimensions:**
- Host

**Known Labels:**

Not yet verified from the available datasource/schema.

Do not infer or invent label names.

**Intent Examples:**
- "Show sustained system load."
- "What's the 5-minute load average?"

**Edge / Confusable Examples:**
- "Show the load average." -> `AMBIGUOUS`
- "Show the long-term load trend." -> `node_load15`
- "Is the system overloaded right now?" -> `node_load1`

**Metric-Specific Query / Result Semantics:**

The raw Gauge value directly represents the requested measurement.

The resulting value represents the current 5-minute operating-system load average.

No additional metric-specific transformation is specified by the project reference.

---

### 6.7 `node_load15`

**Category:**  
Load

**Purpose:**  
Measures the 15-minute operating-system load average.

**Type:**  
Gauge

**Unit:**  
Load average

**Use When:**
- The user requests the 15-minute load average.
- The user wants to know about long-term system load trends.
- The user wants to distinguish a sustained trend from a brief spike.

**Do Not Use / Confusable With:**
- 1-minute load average -> `node_load1`
- 5-minute load average -> `node_load5`
- CPU utilization -> `node_cpu_seconds_total`

For broader distinctions between load-average windows, see **Local Fundamentals → Confusable Metric Families → Load-Average Time Windows**.

**Relevant Scope / Dimensions:**
- Host

**Known Labels:**

Not yet verified from the available datasource/schema.

Do not infer or invent label names.

**Intent Examples:**
- "Show the long-term load trend."
- "What's the 15-minute load average?"

**Edge / Confusable Examples:**
- "Show the load average." -> `AMBIGUOUS`
- "Show sustained load over the last few minutes." -> `node_load5`
- "Is the system overloaded right now?" -> `node_load1`

**Metric-Specific Query / Result Semantics:**

The raw Gauge value directly represents the requested measurement.

The resulting value represents the current 15-minute operating-system load average.

No additional metric-specific transformation is specified by the project reference.

---

### 6.8 `node_context_switches_total`

**Category:**  
CPU

**Purpose:**  
Measures the cumulative total number of context switches performed by the kernel scheduler.

**Type:**  
Counter

**Unit:**  
Count (cumulative)

**Use When:**
- The user requests context-switch activity.
- The user wants to know whether the system is experiencing high scheduling activity.
- The user wants to monitor kernel scheduler overhead.

**Do Not Use / Confusable With:**
- CPU utilization -> `node_cpu_seconds_total`
- Interrupt activity -> `node_intr_total`
- Operating-system load average -> `node_load1`

**Relevant Scope / Dimensions:**
- Host

**Known Labels:**

Not yet verified from the available datasource/schema.

Do not infer or invent label names.

**Intent Examples:**
- "Show context switches."
- "Is the system doing a lot of context switching?"

**Edge / Confusable Examples:**
- "How busy is the CPU?" -> `node_cpu_seconds_total`
- "Show interrupt activity." -> `node_intr_total`

**Metric-Specific Query / Result Semantics:**

This Counter represents the cumulative number of context switches since boot.

The raw Counter value does not directly represent a meaningful current measurement. A meaningful user-facing measurement (context switches per second) requires interpretation as a rate over a time interval, following the shared Counter handling defined by the main skill.

---

### 6.9 `node_intr_total`

**Category:**  
CPU

**Purpose:**  
Measures the cumulative total number of interrupts handled by the system.

**Type:**  
Counter

**Unit:**  
Count (cumulative)

**Use When:**
- The user requests interrupt activity.
- The user wants to know whether the system is handling an unusually high volume of interrupts.
- The user wants to monitor interrupt-heavy workload behavior.

**Do Not Use / Confusable With:**
- CPU utilization -> `node_cpu_seconds_total`
- Context-switch activity -> `node_context_switches_total`
- Operating-system load average -> `node_load1`

**Relevant Scope / Dimensions:**
- Host

**Known Labels:**

Not yet verified from the available datasource/schema.

Do not infer or invent label names.

**Intent Examples:**
- "Show interrupt activity."
- "Is this an interrupt-heavy workload?"

**Edge / Confusable Examples:**
- "How busy is the CPU?" -> `node_cpu_seconds_total`
- "Show context switches." -> `node_context_switches_total`

**Metric-Specific Query / Result Semantics:**

This Counter represents the cumulative number of interrupts handled since boot.

As with other Counters, the raw value does not directly represent a meaningful current measurement. A meaningful user-facing measurement (interrupts per second) requires interpretation as a rate over a time interval, following the shared Counter handling defined by the main skill.

---

### 6.10 `node_memory_MemTotal_bytes`

**Category:**  
Memory

**Purpose:**  
Measures the total amount of physical memory installed on the host.

**Type:**  
Gauge

**Unit:**  
Bytes

**Use When:**
- The user requests total physical memory.
- The user wants to know the total amount of RAM installed.
- The user wants to compare total memory capacity across hosts.

**Do Not Use / Confusable With:**
- Available system memory -> `node_memory_MemAvailable_bytes`

For broader distinctions between memory measurements, see **Local Fundamentals → Memory Measurements**.

**Relevant Scope / Dimensions:**
- Host

**Known Labels:**

Not yet verified from the available datasource/schema.

Do not infer or invent label names.

**Intent Examples:**
- "How much total RAM does this host have?"
- "Show total physical memory."

**Edge / Confusable Examples:**
- "Show memory." -> `AMBIGUOUS`
- "How much memory is available?" -> `node_memory_MemAvailable_bytes`

**Metric-Specific Query / Result Semantics:**

The raw Gauge value directly represents the requested measurement.

The resulting value represents the total physical memory installed on the host, expressed in bytes. This value is expected to remain effectively constant per host and should not be treated as a live/fluctuating measurement.

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