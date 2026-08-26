Defines the metrics available for physical memory, cache, buffers, and swap
usage under Node Exporter, and the semantics needed to select and query each one
correctly.

## Contents

- Quick Facts
- Domain Fundamentals
- Metric Definitions
  - `node_memory_MemTotal_bytes`
  - `node_memory_MemAvailable_bytes`
  - `node_memory_MemFree_bytes`
  - `node_memory_Cached_bytes`
  - `node_memory_Buffers_bytes`
  - `node_memory_SwapTotal_bytes`
  - `node_memory_SwapFree_bytes`
- Domain-Specific Guardrails

## Quick Facts

- **Parent exporter:** node-exporter
- **Domain:** memory
- **Covers:** Physical memory, cache, buffers, and swap usage
- **Metric count:** 7
- **Merged from:** Memory — retained as the primary memory functional domain
  covering physical memory, cache, and buffers. Swap — merged into the broader
  Memory functional domain because the Swap category contains only two metrics
  and is closely related to system memory state.

## Domain Fundamentals

Concepts true across this functional domain only. A concept true across multiple
domains belongs in `overview.md`'s Exporter Fundamentals instead.

### Common Labels & Dimensions

Label keys for this domain's metrics are sourced dynamically at query-generation
time — see `SKILL.md` §5 Principle 9 and `overview.md` § Entity Scope Baseline.
This domain has no intrinsic dimension beyond the node-level entity scope
itself — each metric represents one current-state figure per node, without an
additional semantic sub-dimension documented here.

### Confusable Measurements

**Total vs. Available vs. Free Memory** — all three describe physical RAM but
answer different questions, and none is interchangeable with another:

| Metric | What it measures | Use for |
|---|---|---|
| `node_memory_MemTotal_bytes` | Total physical RAM | Total installed physical memory |
| `node_memory_MemAvailable_bytes` | Memory available for use without swapping | "How much memory is available?" |
| `node_memory_MemFree_bytes` | Completely free (unused) memory | "How much RAM is completely free/unused?" |

Do not treat available memory and completely free memory as interchangeable — a
request for "available memory" must not be automatically mapped to "free
memory." Available memory answers whether the system can operate without
swapping; free memory answers how much RAM is entirely unused.

**Cached vs. Buffer Memory** — both are physical-memory sub-categories, not
interchangeable:

| Metric | What it measures | Use for |
|---|---|---|
| `node_memory_Cached_bytes` | Linux page cache | Cache usage |
| `node_memory_Buffers_bytes` | Filesystem buffers | Buffer usage |

Do not treat page cache and filesystem buffers as the same memory category.

**Total Swap vs. Free Swap:**

| Metric | What it measures | Use for |
|---|---|---|
| `node_memory_SwapTotal_bytes` | Total swap space | Total swap capacity |
| `node_memory_SwapFree_bytes` | Available swap space | Remaining/free swap |

Do not treat total swap and free swap as the same measurement.

**Physical Memory vs. Swap** — the domain contains both physical-memory
measurements (total, available, free, cached, buffered) and swap measurements
(capacity, availability). Do not treat swap as another form of physical RAM
when interpreting the requested measurement, and do not use free physical
memory as a substitute for available memory without swapping, or vice versa.

## Metric Definitions

### `node_memory_MemTotal_bytes`

- **Category:** Physical Memory
- **Purpose:** Measures total physical memory (RAM).
- **Type:** `Gauge`
- **Unit:** bytes
- **Use when:** the user asks for total RAM; how much physical memory the
  system has; or to compare total physical memory across nodes.
- **Do not use / confusable with:** memory available without swapping →
  `node_memory_MemAvailable_bytes`; completely free memory →
  `node_memory_MemFree_bytes`; cached memory → `node_memory_Cached_bytes`;
  filesystem buffers → `node_memory_Buffers_bytes`; total swap space →
  `node_memory_SwapTotal_bytes`.
- **Relevant scope:** Node (not a label list).
- **Additional known labels:** no additional dimension beyond the node-level entity scope is documented for this metric. Label keys are sourced dynamically from the runtime at query-generation time (`SKILL.md` §5 Principle 9) — do not invent one.
- **Intent examples:** "How much RAM does this node have?", "What is the total
  physical memory?", "Compare total RAM across the nodes."
- **Edge/confusable example:** user asks how much memory is currently
  available for use → use `node_memory_MemAvailable_bytes`, not this metric.
- **Metric-specific query/result semantics:** directly represents total
  physical memory. No per-metric override of `SKILL.md` defaults is currently
  defined.
- **Query examples:** no verified PromQL query example is currently available.
  Do not invent a literal query example.

### `node_memory_MemAvailable_bytes`

- **Category:** Physical Memory
- **Purpose:** Measures memory available without swapping.
- **Type:** `Gauge`
- **Unit:** bytes
- **Use when:** the user asks how much memory is available; how much RAM is
  available for use; whether the system has sufficient available memory; or
  about available memory without swapping.
- **Do not use / confusable with:** total physical RAM →
  `node_memory_MemTotal_bytes`; completely free memory →
  `node_memory_MemFree_bytes`; available swap → `node_memory_SwapFree_bytes`.
  Do not interpret available memory as equivalent to completely free memory.
- **Relevant scope:** Node (not a label list).
- **Additional known labels:** no additional dimension beyond the node-level entity scope is documented for this metric. Label keys are sourced dynamically from the runtime at query-generation time (`SKILL.md` §5 Principle 9) — do not invent one.
- **Intent examples:** "How much memory is available?", "How much RAM is
  available without swapping?", "Does this node have enough available memory?"
- **Edge/confusable example:** user asks for completely unused memory rather
  than memory available without swapping → use `node_memory_MemFree_bytes`.
- **Metric-specific query/result semantics:** directly represents memory
  available without swapping. No per-metric override of `SKILL.md` defaults is
  currently defined.
- **Query examples:** no verified PromQL query example is currently available.
  Do not invent a literal query example.

### `node_memory_MemFree_bytes`

- **Category:** Physical Memory
- **Purpose:** Measures completely free physical memory.
- **Type:** `Gauge`
- **Unit:** bytes
- **Use when:** the user asks for free RAM; how much completely unused
  physical memory remains; or to compare free physical memory across nodes.
- **Do not use / confusable with:** memory available without swapping →
  `node_memory_MemAvailable_bytes`; total physical memory →
  `node_memory_MemTotal_bytes`; free swap space →
  `node_memory_SwapFree_bytes`. Do not treat free physical memory as
  equivalent to memory available for use.
- **Relevant scope:** Node (not a label list).
- **Additional known labels:** no additional dimension beyond the node-level entity scope is documented for this metric. Label keys are sourced dynamically from the runtime at query-generation time (`SKILL.md` §5 Principle 9) — do not invent one.
- **Intent examples:** "How much free RAM is there?", "How much physical
  memory is completely free?", "Show the free memory on this node."
- **Edge/confusable example:** user asks how much memory can be used without
  swapping → use `node_memory_MemAvailable_bytes`, not this metric.
- **Metric-specific query/result semantics:** directly represents completely
  free physical memory. No per-metric override of `SKILL.md` defaults is
  currently defined.
- **Query examples:** no verified PromQL query example is currently available.
  Do not invent a literal query example.

### `node_memory_Cached_bytes`

- **Category:** Memory Cache
- **Purpose:** Measures Linux page cache.
- **Type:** `Gauge`
- **Unit:** bytes
- **Use when:** the user asks about cached memory; how much memory is being
  used by the Linux page cache; or wants to investigate cache usage.
- **Do not use / confusable with:** filesystem buffers →
  `node_memory_Buffers_bytes`; completely free memory →
  `node_memory_MemFree_bytes`; available memory →
  `node_memory_MemAvailable_bytes`. Do not treat page cache as filesystem
  buffer usage.
- **Relevant scope:** Node (not a label list).
- **Additional known labels:** no additional dimension beyond the node-level entity scope is documented for this metric. Label keys are sourced dynamically from the runtime at query-generation time (`SKILL.md` §5 Principle 9) — do not invent one.
- **Intent examples:** "How much memory is cached?", "How much RAM is being
  used for the page cache?", "Show the node's cached memory."
- **Edge/confusable example:** user asks about filesystem buffer usage → use
  `node_memory_Buffers_bytes`, not this metric.
- **Metric-specific query/result semantics:** directly represents Linux page
  cache. No per-metric override of `SKILL.md` defaults is currently defined.
- **Query examples:** no verified PromQL query example is currently available.
  Do not invent a literal query example.

### `node_memory_Buffers_bytes`

- **Category:** Memory Buffers
- **Purpose:** Measures filesystem buffers.
- **Type:** `Gauge`
- **Unit:** bytes
- **Use when:** the user asks about buffer cache usage; how much memory is
  used for filesystem buffers; or wants to investigate buffer usage.
- **Do not use / confusable with:** Linux page cache →
  `node_memory_Cached_bytes`; available memory →
  `node_memory_MemAvailable_bytes`; completely free memory →
  `node_memory_MemFree_bytes`. Do not treat filesystem buffers as equivalent
  to page cache.
- **Relevant scope:** Node (not a label list).
- **Additional known labels:** no additional dimension beyond the node-level entity scope is documented for this metric. Label keys are sourced dynamically from the runtime at query-generation time (`SKILL.md` §5 Principle 9) — do not invent one.
- **Intent examples:** "How much memory is being used for buffers?", "What is
  the buffer cache usage?", "Show filesystem buffer memory."
- **Edge/confusable example:** user asks about Linux page cache → use
  `node_memory_Cached_bytes`, not this metric.
- **Metric-specific query/result semantics:** directly represents filesystem
  buffers. No per-metric override of `SKILL.md` defaults is currently defined.
- **Query examples:** no verified PromQL query example is currently available.
  Do not invent a literal query example.

### `node_memory_SwapTotal_bytes`

- **Category:** Swap
- **Purpose:** Measures total swap space.
- **Type:** `Gauge`
- **Unit:** bytes
- **Use when:** the user asks for total swap space; how much swap capacity the
  node has; or wants to compare swap capacity across nodes.
- **Do not use / confusable with:** available/free swap →
  `node_memory_SwapFree_bytes`; total physical RAM →
  `node_memory_MemTotal_bytes`. Do not treat total swap capacity as total
  physical memory.
- **Relevant scope:** Node (not a label list).
- **Additional known labels:** no additional dimension beyond the node-level entity scope is documented for this metric. Label keys are sourced dynamically from the runtime at query-generation time (`SKILL.md` §5 Principle 9) — do not invent one.
- **Intent examples:** "How much swap does this node have?", "What is the
  total swap space?", "Compare swap capacity across nodes."
- **Edge/confusable example:** user asks how much swap is currently available
  → use `node_memory_SwapFree_bytes`, not this metric.
- **Metric-specific query/result semantics:** directly represents total swap
  space. No per-metric override of `SKILL.md` defaults is currently defined.
- **Query examples:** no verified PromQL query example is currently available.
  Do not invent a literal query example.

### `node_memory_SwapFree_bytes`

- **Category:** Swap
- **Purpose:** Measures available swap space.
- **Type:** `Gauge`
- **Unit:** bytes
- **Use when:** the user asks how much swap is free; how much swap is
  available; wants to determine remaining swap capacity; or wants to compare
  available swap across nodes.
- **Do not use / confusable with:** total swap space →
  `node_memory_SwapTotal_bytes`; available physical memory →
  `node_memory_MemAvailable_bytes`; free physical memory →
  `node_memory_MemFree_bytes`. Do not treat free swap as free physical memory.
- **Relevant scope:** Node (not a label list).
- **Additional known labels:** no additional dimension beyond the node-level entity scope is documented for this metric. Label keys are sourced dynamically from the runtime at query-generation time (`SKILL.md` §5 Principle 9) — do not invent one.
- **Intent examples:** "How much swap is free?", "How much swap space is
  available?", "How much remaining swap capacity does the node have?"
- **Edge/confusable example:** user asks how much physical RAM is available
  without swapping → use `node_memory_MemAvailable_bytes`, not this metric.
- **Metric-specific query/result semantics:** directly represents available
  swap space. No per-metric override of `SKILL.md` defaults is currently
  defined.
- **Query examples:** no verified PromQL query example is currently available.
  Do not invent a literal query example.

## Domain-Specific Guardrails

- Do not treat total physical memory, available memory, and completely free
  memory as interchangeable measurements.
- Do not treat page cache and filesystem buffers as the same memory category.
- Do not treat physical memory and swap as interchangeable.
- Do not treat total swap and free swap as the same measurement.
- Do not use free physical memory as a substitute for available memory without
  swapping.
- Do not assume a specific node unless the user provides the relevant
  constraint.
- Do not invent Prometheus label names or label values — label keys must be
  confirmed by the runtime, never assumed from this reference (`SKILL.md` §5
  Principle 9).
- Do not invent a PromQL expression when the required query semantics have not
  been verified.
