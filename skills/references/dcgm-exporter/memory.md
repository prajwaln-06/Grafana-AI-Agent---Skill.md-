Defines the metrics available for GPU memory capacity and memory-subsystem
utilization/bandwidth under DCGM Exporter, and the semantics needed to select
and query each one correctly.

## Contents

- Quick Facts
- Domain Fundamentals
- Metric Definitions
  - `DCGM_FI_DEV_FB_USED`
  - `DCGM_FI_DEV_FB_FREE`
  - `DCGM_FI_DEV_MEM_COPY_UTIL`
  - `DCGM_FI_PROF_DRAM_ACTIVE`
- Domain-Specific Guardrails

## Quick Facts

- **Parent exporter:** dcgm-exporter
- **Domain:** memory
- **Covers:** GPU memory capacity and memory-subsystem bandwidth/utilization
- **Metric count:** 4
- **Merged from:** GPU Memory — retained as part of the broader Memory
  functional domain because it contains GPU memory capacity and
  memory-controller utilization metrics. Memory Bandwidth — merged into
  Memory because it concerns the same physical resource (the GPU memory
  subsystem) as GPU Memory, following the same one-file-per-resource shape
  already used by `compute.md`, rather than being split into a separate
  bandwidth-only domain.

## Domain Fundamentals

Concepts true across this functional domain only. A concept true across multiple
domains belongs in `overview.md`'s Exporter Fundamentals instead.

### Common Labels & Dimensions

Label keys for this domain's metrics are sourced dynamically at query-generation
time — see `SKILL.md` §5 Principle 9 and `overview.md` § Entity Scope Baseline.
This domain has no intrinsic dimension beyond the GPU-level entity scope
itself — each metric in this domain represents one capacity or utilization
figure per GPU, without an additional semantic sub-dimension documented here.

### Confusable Measurements

**GPU Memory Capacity — Used vs. Free:**

| Metric | What it measures | Use for |
|---|---|---|
| `DCGM_FI_DEV_FB_USED` | Used framebuffer (VRAM) memory, in bytes | How much GPU memory is currently in use |
| `DCGM_FI_DEV_FB_FREE` | Free framebuffer memory, in bytes | How much GPU memory is currently available |

> **No "Total" metric is documented for GPU memory capacity.** Unlike Node
> Exporter's memory triad (Total/Available/Free), there is no
> `DCGM_FI_DEV_FB_TOTAL` metric defined in this skill. Do not construct a
> "total GPU memory" answer by assuming `DCGM_FI_DEV_FB_USED` plus
> `DCGM_FI_DEV_FB_FREE` equals a meaningful total unless that relationship is
> independently verified — treat a request specifically for total GPU memory
> capacity as not currently supported by a defined metric.

**GPU Memory Capacity vs. Memory-Controller Utilization vs. DRAM Bandwidth:**

| Metric | What it measures | Use for |
|---|---|---|
| `DCGM_FI_DEV_FB_USED` / `DCGM_FI_DEV_FB_FREE` | GPU memory capacity in use/available, in bytes | "How much GPU memory is used/free", capacity questions |
| `DCGM_FI_DEV_MEM_COPY_UTIL` | Memory controller utilization, as a percent | "How utilized is the GPU's memory controller" |
| `DCGM_FI_PROF_DRAM_ACTIVE` | DRAM bandwidth utilization, as a percent | "Is the GPU memory-bandwidth-bound", DRAM activity questions |

> **`DCGM_FI_DEV_MEM_COPY_UTIL` vs. `DCGM_FI_PROF_DRAM_ACTIVE` — plausible
> overlap, not established as identical or as distinct.** Both concern
> memory-subsystem activity ("memory controller utilization" vs. "DRAM
> bandwidth utilization") and are documented under different original vendor
> categories ("GPU Memory" vs. "Memory Bandwidth") with different metric-name
> prefixes (`DCGM_FI_DEV_*` vs. `DCGM_FI_PROF_*`) — the same naming-prefix
> pattern already seen with `DCGM_FI_DEV_GPU_UTIL` vs.
> `DCGM_FI_PROF_GR_ENGINE_ACTIVE` in `compute.md`, where prefix alone does not
> indicate semantic equivalence. The authoritative metric reference does not
> state whether these two metrics measure the same underlying activity through
> two interfaces or genuinely different things, and this remains unresolved.
> **Do not collapse these into one metric, treat them as redundant, or
> silently pick one over the other on the user's behalf.**
>
> The authoritative metric reference does, however, explicitly establish two
> specific phrase-to-metric mappings, independent of the open
> identical-vs-distinct question above: its Typical Query Intent column lists
> **"Memory bandwidth utilization"** as the documented trigger phrase for
> `DCGM_FI_DEV_MEM_COPY_UTIL` specifically, and its What-it-Measures text for
> `DCGM_FI_PROF_DRAM_ACTIVE` is verbatim **"DRAM bandwidth utilization (%)"**.
> Use these document-established mappings for exactly those phrasings — this
> is following a stated source fact, not independently resolving the
> identical-vs-distinct question. A request using different, less specific
> wording (e.g. "GPU memory bandwidth" alone, without "memory" or "DRAM"
> qualifying it as above) is not covered by either established mapping and
> should be treated per `SKILL.md` §6 Step 3 rather than assumed to match one
> of the two document-established phrasings.

Capacity (`DCGM_FI_DEV_FB_USED`/`FREE`) answers "how much memory," while
`DCGM_FI_DEV_MEM_COPY_UTIL` and `DCGM_FI_PROF_DRAM_ACTIVE` answer "how
utilized/active is the memory subsystem" — do not substitute a capacity metric
for a utilization/bandwidth question or vice versa.

## Metric Definitions

### `DCGM_FI_DEV_FB_USED`

- **Category:** GPU Memory
- **Purpose:** Measures used framebuffer (VRAM) memory.
- **Type:** `Gauge`
- **Unit:** Bytes
- **Use when:** the user asks how much GPU memory is in use, GPU memory
  consumption, or how much VRAM a GPU is using.
- **Do not use / confusable with:** how much GPU memory is available →
  `DCGM_FI_DEV_FB_FREE`; how utilized the memory controller or DRAM bandwidth
  is → `DCGM_FI_DEV_MEM_COPY_UTIL` or `DCGM_FI_PROF_DRAM_ACTIVE` (see
  Confusable Measurements above); a "total GPU memory" question — no total
  metric is currently defined (see Confusable Measurements above).
- **Relevant scope:** GPU (not a label list).
- **Additional known labels:** sourced dynamically at query-generation time from the runtime — see `SKILL.md` §5 Principle 9. No additional dimension beyond the GPU-level scope is documented for this metric. Label keys are sourced dynamically from the runtime at query-generation time — do not invent one.
- **Intent examples:** "How much GPU memory is being used?", "What's the VRAM
  usage on GPU 1?"
- **Edge/confusable example:** user asks how much GPU memory is still
  available rather than in use → use `DCGM_FI_DEV_FB_FREE`, not this metric.
- **Metric-specific query/result semantics:** directly represents used GPU
  memory capacity in bytes. No per-metric override of `SKILL.md` defaults is
  currently defined.
- **Query examples:** no verified DCGM PromQL query example is currently
  available. Do not invent a literal query example.

### `DCGM_FI_DEV_FB_FREE`

- **Category:** GPU Memory
- **Purpose:** Measures free framebuffer (VRAM) memory.
- **Type:** `Gauge`
- **Unit:** Bytes
- **Use when:** the user asks how much GPU memory is available, free, or
  remaining.
- **Do not use / confusable with:** how much GPU memory is currently in use →
  `DCGM_FI_DEV_FB_USED`; how utilized the memory controller or DRAM bandwidth
  is → `DCGM_FI_DEV_MEM_COPY_UTIL` or `DCGM_FI_PROF_DRAM_ACTIVE` (see
  Confusable Measurements above); a "total GPU memory" question — no total
  metric is currently defined (see Confusable Measurements above).
- **Relevant scope:** GPU (not a label list).
- **Additional known labels:** sourced dynamically at query-generation time from the runtime — see `SKILL.md` §5 Principle 9. No additional dimension beyond the GPU-level scope is documented for this metric. Label keys are sourced dynamically from the runtime at query-generation time — do not invent one.
- **Intent examples:** "How much GPU memory is free?", "How much VRAM is
  available on GPU 0?"
- **Edge/confusable example:** user asks how much GPU memory is in use rather
  than available → use `DCGM_FI_DEV_FB_USED`, not this metric.
- **Metric-specific query/result semantics:** directly represents free GPU
  memory capacity in bytes. No per-metric override of `SKILL.md` defaults is
  currently defined.
- **Query examples:** no verified DCGM PromQL query example is currently
  available. Do not invent a literal query example.

### `DCGM_FI_DEV_MEM_COPY_UTIL`

- **Category:** GPU Memory
- **Purpose:** Measures memory controller utilization as a percentage.
- **Type:** `Gauge`
- **Unit:** Percent (%)
- **Use when:** the user asks about memory controller utilization or memory
  bandwidth utilization framed in terms of the memory controller specifically.
  The authoritative metric reference's Typical Query Intent column lists
  "Memory bandwidth utilization" as the documented trigger phrase for this
  metric specifically — see Confusable Measurements above.
- **Do not use / confusable with:** GPU memory capacity in use/available →
  `DCGM_FI_DEV_FB_USED`/`DCGM_FI_DEV_FB_FREE`; DRAM bandwidth utilization
  phrased specifically in terms of DRAM → `DCGM_FI_PROF_DRAM_ACTIVE` — see the
  document-established phrase mappings and the unresolved
  identical-vs-distinct question in Confusable Measurements above.
- **Relevant scope:** GPU (not a label list).
- **Additional known labels:** sourced dynamically at query-generation time from the runtime — see `SKILL.md` §5 Principle 9. No additional dimension beyond the GPU-level scope is documented for this metric. Label keys are sourced dynamically from the runtime at query-generation time — do not invent one.
- **Intent examples:** "What is the memory controller utilization?", "What's
  the memory bandwidth utilization?", "How utilized is the GPU's memory copy
  engine?"
- **Edge/confusable example:** user asks specifically about "DRAM bandwidth
  utilization" rather than "memory bandwidth utilization" → use
  `DCGM_FI_PROF_DRAM_ACTIVE` per its own document-established mapping, not
  this metric — see Confusable Measurements above.
- **Metric-specific query/result semantics:** represents memory controller
  utilization as a percentage. No per-metric override of `SKILL.md` defaults
  is currently defined.
- **Query examples:** no verified DCGM PromQL query example is currently
  available. Do not invent a literal query example.

### `DCGM_FI_PROF_DRAM_ACTIVE`

- **Category:** Memory Bandwidth
- **Purpose:** Measures DRAM bandwidth utilization as a percentage.
- **Type:** `Gauge`
- **Unit:** Percent (%)
- **Use when:** the user asks about DRAM bandwidth utilization, whether the
  GPU is memory-bandwidth-bound, or DRAM activity specifically. The
  authoritative metric reference's own What-it-Measures text for this metric
  is verbatim "DRAM bandwidth utilization (%)" — see Confusable Measurements
  above.
- **Do not use / confusable with:** GPU memory capacity in use/available →
  `DCGM_FI_DEV_FB_USED`/`DCGM_FI_DEV_FB_FREE`; memory bandwidth utilization
  phrased specifically in terms of the memory controller →
  `DCGM_FI_DEV_MEM_COPY_UTIL` — see the document-established phrase mappings
  and the unresolved identical-vs-distinct question in Confusable
  Measurements above; overall GPU compute-engine activity → see the
  compute-vs-memory-bound cross-domain distinction in `overview.md`'s
  Cross-Domain Semantic Distinctions.
- **Relevant scope:** GPU (not a label list).
- **Additional known labels:** sourced dynamically at query-generation time from the runtime — see `SKILL.md` §5 Principle 9. No additional dimension beyond the GPU-level scope is documented for this metric. Label keys are sourced dynamically from the runtime at query-generation time — do not invent one.
- **Intent examples:** "Is the GPU memory-bandwidth-bound?", "What is the DRAM
  bandwidth utilization?"
- **Edge/confusable example:** user asks specifically about "memory bandwidth
  utilization" (the memory-controller phrasing) rather than "DRAM bandwidth
  utilization" → use `DCGM_FI_DEV_MEM_COPY_UTIL` per its own
  document-established mapping, not this metric — see Confusable Measurements
  above. User asks whether the GPU is compute-bound rather than memory-bound
  → this is a cross-domain question; see `overview.md`'s Cross-Domain
  Semantic Distinctions rather than answering from this metric alone.
- **Metric-specific query/result semantics:** represents DRAM bandwidth
  utilization as a percentage, distinct from compute-engine activity. No
  per-metric override of `SKILL.md` defaults is currently defined.
- **Query examples:** no verified DCGM PromQL query example is currently
  available. Do not invent a literal query example.

## Domain-Specific Guardrails

- Do not treat GPU memory capacity (`DCGM_FI_DEV_FB_USED`/`FREE`) as
  interchangeable with memory-subsystem utilization/bandwidth
  (`DCGM_FI_DEV_MEM_COPY_UTIL`/`DCGM_FI_PROF_DRAM_ACTIVE`).
- Do not construct a "total GPU memory" figure from `DCGM_FI_DEV_FB_USED` plus
  `DCGM_FI_DEV_FB_FREE` — no total metric is currently defined for this skill.
- Do not resolve the `DCGM_FI_DEV_MEM_COPY_UTIL` vs. `DCGM_FI_PROF_DRAM_ACTIVE`
  overlap by picking one on the user's behalf, collapsing them into a single
  answer, or asserting they measure the same thing — see Confusable
  Measurements above.
- Do not invent Prometheus label names or label values — label keys must be
  confirmed by the runtime, never assumed from this reference (`SKILL.md` §5
  Principle 9).
- Do not assume a specific GPU, device, or node unless the user provides the
  relevant constraint.
- Do not invent a PromQL expression when the required query semantics have not
  been verified.
