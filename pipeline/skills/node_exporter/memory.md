---
parent_exporter: node_exporter
domain_id: memory
covers: Physical memory, cache, buffers, and swap usage
metric_count: 7

merged_from:
  - Memory → retained as the primary memory functional domain covering physical memory, cache, and buffers.
  - Swap → merged into the broader Memory functional domain because the Swap category contains only two metrics and is closely related to system memory state.
---

# Node Exporter — Memory

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

All currently supported metrics in this domain use the exporter-wide
baseline labels.

---

### 1.2 Confusable Metric Families

#### Total vs Available vs Free Memory

- `node_memory_MemTotal_bytes`
- `node_memory_MemAvailable_bytes`
- `node_memory_MemFree_bytes`

Difference:

`node_memory_MemTotal_bytes` represents total physical RAM.

`node_memory_MemAvailable_bytes` represents memory available without
swapping.

`node_memory_MemFree_bytes` represents completely free memory.

Use:

- Total installed physical memory → `node_memory_MemTotal_bytes`
- Memory available for use without swapping → `node_memory_MemAvailable_bytes`
- Completely free memory → `node_memory_MemFree_bytes`

Do not treat available memory and completely free memory as interchangeable.

---

#### Cached vs Buffer Memory

- `node_memory_Cached_bytes`
- `node_memory_Buffers_bytes`

Difference:

`node_memory_Cached_bytes` measures Linux page cache, while
`node_memory_Buffers_bytes` measures filesystem buffers.

Use:

- Page cache → `node_memory_Cached_bytes`
- Filesystem buffers → `node_memory_Buffers_bytes`

Do not treat page cache and filesystem buffers as the same memory category.

---

#### Total Swap vs Free Swap

- `node_memory_SwapTotal_bytes`
- `node_memory_SwapFree_bytes`

Difference:

`node_memory_SwapTotal_bytes` represents total swap space, while
`node_memory_SwapFree_bytes` represents available swap space.

Use:

- Total swap capacity → `node_memory_SwapTotal_bytes`
- Available/free swap → `node_memory_SwapFree_bytes`

---

### 1.3 Domain-Specific Semantic Distinctions

#### Physical Memory vs Swap

The memory domain contains both physical-memory measurements and swap-space
measurements.

Physical-memory metrics describe RAM, including total, available, free,
cached, and buffered memory.

Swap metrics describe swap capacity and availability.

Do not treat swap as another form of physical RAM when interpreting the
requested measurement.

---

#### Available Memory vs Free Memory

`node_memory_MemAvailable_bytes` and `node_memory_MemFree_bytes` answer
different questions.

- Available memory → memory that can be used without swapping.
- Free memory → completely unused physical memory.

A request for "available memory" should not automatically be mapped to
"free memory."

---

## 2. Metric Definitions

### 2.1 `node_memory_MemTotal_bytes`

#### Category

Physical Memory

#### Purpose

Measures total physical memory (RAM).

#### Type

`Gauge`

#### Unit

bytes

#### Use When

- The user asks for total RAM.
- The user asks how much physical memory the system has.
- The user wants to compare total physical memory across nodes.

#### Do Not Use / Confusable With

- Memory available without swapping → `node_memory_MemAvailable_bytes`
- Completely free memory → `node_memory_MemFree_bytes`
- Cached memory → `node_memory_Cached_bytes`
- Filesystem buffers → `node_memory_Buffers_bytes`
- Total swap space → `node_memory_SwapTotal_bytes`

#### Relevant Scope

Node.

This is not a label list.

#### Additional Known Labels

None beyond domain-level labels (see Domain Fundamentals §1.1).

#### Intent Examples

- "How much RAM does this node have?"
- "What is the total physical memory?"
- "Compare total RAM across the nodes."

#### Edge / Confusable Example (Optional)

> User asks how much memory is currently available for use.
>
> Use `node_memory_MemAvailable_bytes`, not this metric.

#### Metric-Specific Query / Result Semantics

The metric directly represents total physical memory.

No per-metric override of Main Skill defaults is currently defined.

#### Query Examples (Optional)

No verified PromQL query example is currently available.
Do not invent a literal query example.

---

### 2.2 `node_memory_MemAvailable_bytes`

#### Category

Physical Memory

#### Purpose

Measures memory available without swapping.

#### Type

`Gauge`

#### Unit

bytes

#### Use When

- The user asks how much memory is available.
- The user asks how much RAM is available for use.
- The user wants to determine whether the system has sufficient available memory.
- The user asks about available memory without swapping.

#### Do Not Use / Confusable With

- Total physical RAM → `node_memory_MemTotal_bytes`
- Completely free memory → `node_memory_MemFree_bytes`
- Available swap → `node_memory_SwapFree_bytes`

Do not interpret available memory as equivalent to completely free memory.

#### Relevant Scope

Node.

This is not a label list.

#### Additional Known Labels

None beyond domain-level labels (see Domain Fundamentals §1.1).

#### Intent Examples

- "How much memory is available?"
- "How much RAM is available without swapping?"
- "Does this node have enough available memory?"

#### Edge / Confusable Example (Optional)

> User asks for completely unused memory rather than memory available
> without swapping.
>
> Use `node_memory_MemFree_bytes`.

#### Metric-Specific Query / Result Semantics

The metric directly represents memory available without swapping.

No per-metric override of Main Skill defaults is currently defined.

#### Query Examples (Optional)

No verified PromQL query example is currently available.
Do not invent a literal query example.

---

### 2.3 `node_memory_MemFree_bytes`

#### Category

Physical Memory

#### Purpose

Measures completely free physical memory.

#### Type

`Gauge`

#### Unit

bytes

#### Use When

- The user asks for free RAM.
- The user asks how much completely unused physical memory remains.
- The user wants to compare free physical memory across nodes.

#### Do Not Use / Confusable With

- Memory available without swapping → `node_memory_MemAvailable_bytes`
- Total physical memory → `node_memory_MemTotal_bytes`
- Free swap space → `node_memory_SwapFree_bytes`

Do not treat free physical memory as equivalent to memory available for use.

#### Relevant Scope

Node.

This is not a label list.

#### Additional Known Labels

None beyond domain-level labels (see Domain Fundamentals §1.1).

#### Intent Examples

- "How much free RAM is there?"
- "How much physical memory is completely free?"
- "Show the free memory on this node."

#### Edge / Confusable Example (Optional)

> User asks how much memory can be used without swapping.
>
> Use `node_memory_MemAvailable_bytes`, not this metric.

#### Metric-Specific Query / Result Semantics

The metric directly represents completely free physical memory.

No per-metric override of Main Skill defaults is currently defined.

#### Query Examples (Optional)

No verified PromQL query example is currently available.
Do not invent a literal query example.

---

### 2.4 `node_memory_Cached_bytes`

#### Category

Memory Cache

#### Purpose

Measures Linux page cache.

#### Type

`Gauge`

#### Unit

bytes

#### Use When

- The user asks about cached memory.
- The user asks how much memory is being used by the Linux page cache.
- The user wants to investigate cache usage.

#### Do Not Use / Confusable With

- Filesystem buffers → `node_memory_Buffers_bytes`
- Completely free memory → `node_memory_MemFree_bytes`
- Available memory → `node_memory_MemAvailable_bytes`

Do not treat page cache as filesystem buffer usage.

#### Relevant Scope

Node.

This is not a label list.

#### Additional Known Labels

None beyond domain-level labels (see Domain Fundamentals §1.1).

#### Intent Examples

- "How much memory is cached?"
- "How much RAM is being used for the page cache?"
- "Show the node's cached memory."

#### Edge / Confusable Example (Optional)

> User asks about filesystem buffer usage.
>
> Use `node_memory_Buffers_bytes`, not this metric.

#### Metric-Specific Query / Result Semantics

The metric directly represents Linux page cache.

No per-metric override of Main Skill defaults is currently defined.

#### Query Examples (Optional)

No verified PromQL query example is currently available.
Do not invent a literal query example.

---

### 2.5 `node_memory_Buffers_bytes`

#### Category

Memory Buffers

#### Purpose

Measures filesystem buffers.

#### Type

`Gauge`

#### Unit

bytes

#### Use When

- The user asks about buffer cache usage.
- The user asks how much memory is used for filesystem buffers.
- The user wants to investigate buffer usage.

#### Do Not Use / Confusable With

- Linux page cache → `node_memory_Cached_bytes`
- Available memory → `node_memory_MemAvailable_bytes`
- Completely free memory → `node_memory_MemFree_bytes`

Do not treat filesystem buffers as equivalent to page cache.

#### Relevant Scope

Node.

This is not a label list.

#### Additional Known Labels

None beyond domain-level labels (see Domain Fundamentals §1.1).

#### Intent Examples

- "How much memory is being used for buffers?"
- "What is the buffer cache usage?"
- "Show filesystem buffer memory."

#### Edge / Confusable Example (Optional)

> User asks about Linux page cache.
>
> Use `node_memory_Cached_bytes`, not this metric.

#### Metric-Specific Query / Result Semantics

The metric directly represents filesystem buffers.

No per-metric override of Main Skill defaults is currently defined.

#### Query Examples (Optional)

No verified PromQL query example is currently available.
Do not invent a literal query example.

---

### 2.6 `node_memory_SwapTotal_bytes`

#### Category

Swap

#### Purpose

Measures total swap space.

#### Type

`Gauge`

#### Unit

bytes

#### Use When

- The user asks for total swap space.
- The user asks how much swap capacity the node has.
- The user wants to compare swap capacity across nodes.

#### Do Not Use / Confusable With

- Available/free swap → `node_memory_SwapFree_bytes`
- Total physical RAM → `node_memory_MemTotal_bytes`

Do not treat total swap capacity as total physical memory.

#### Relevant Scope

Node.

This is not a label list.

#### Additional Known Labels

None beyond domain-level labels (see Domain Fundamentals §1.1).

#### Intent Examples

- "How much swap does this node have?"
- "What is the total swap space?"
- "Compare swap capacity across nodes."

#### Edge / Confusable Example (Optional)

> User asks how much swap is currently available.
>
> Use `node_memory_SwapFree_bytes`, not this metric.

#### Metric-Specific Query / Result Semantics

The metric directly represents total swap space.

No per-metric override of Main Skill defaults is currently defined.

#### Query Examples (Optional)

No verified PromQL query example is currently available.
Do not invent a literal query example.

---

### 2.7 `node_memory_SwapFree_bytes`

#### Category

Swap

#### Purpose

Measures available swap space.

#### Type

`Gauge`

#### Unit

bytes

#### Use When

- The user asks how much swap is free.
- The user asks how much swap is available.
- The user wants to determine remaining swap capacity.
- The user wants to compare available swap across nodes.

#### Do Not Use / Confusable With

- Total swap space → `node_memory_SwapTotal_bytes`
- Available physical memory → `node_memory_MemAvailable_bytes`
- Free physical memory → `node_memory_MemFree_bytes`

Do not treat free swap as free physical memory.

#### Relevant Scope

Node.

This is not a label list.

#### Additional Known Labels

None beyond domain-level labels (see Domain Fundamentals §1.1).

#### Intent Examples

- "How much swap is free?"
- "How much swap space is available?"
- "How much remaining swap capacity does the node have?"

#### Edge / Confusable Example (Optional)

> User asks how much physical RAM is available without swapping.
>
> Use `node_memory_MemAvailable_bytes`, not this metric.

#### Metric-Specific Query / Result Semantics

The metric directly represents available swap space.

No per-metric override of Main Skill defaults is currently defined.

#### Query Examples (Optional)

No verified PromQL query example is currently available.
Do not invent a literal query example.

---

## 3. Domain-Specific Guardrails (Optional)

- Do not treat total physical memory, available memory, and completely free memory as interchangeable measurements.
- Do not treat page cache and filesystem buffers as the same memory category.
- Do not treat physical memory and swap as interchangeable.
- Do not treat total swap and free swap as the same measurement.
- Do not use free physical memory as a substitute for available memory without swapping.
- Do not assume a specific node unless the user provides the relevant constraint.
- Do not invent Prometheus label names or label values.
- Do not invent a PromQL expression when the required query semantics have not been verified.