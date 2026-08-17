Defines the GPU observability domain covered by DCGM Exporter and routes requests
to the appropriate metric domain file. Does not contain detailed metric
definitions — see the linked domain files for those.

## Contents

- Quick Facts
- Trigger Examples
- Do Not Use
- Metric Directory
- Derived / Composed Measurements
- Exporter Fundamentals
- Guardrails

## Quick Facts

- **Data source:** prometheus
- **Covers:** GPU observability provided by the DCGM Exporter
- **Domain files:** [compute.md](compute.md), [thermal.md](thermal.md), [memory.md](memory.md), [interconnect.md](interconnect.md), [reliability.md](reliability.md)

## Trigger Examples

Examples of user requests that route here:

- "What is the GPU utilization?"
- "How busy is GPU 2?"
- "How active are the GPU compute engines?"
- "How much tensor activity is there?"
- "What is the FP32 pipeline utilization?"
- "How much FP64 or FP16 workload is running?"
- "What is the GPU temperature?"
- "Is the GPU running too hot?"
- "What is the GPU power consumption?"
- "What is the current GPU clock?"
- "Is the GPU being power throttled?"
- "How much GPU memory is being used?"
- "How much VRAM is free on GPU 0?"
- "What is the memory controller utilization?"
- "Is the GPU memory-bandwidth-bound?"
- "What is the PCIe transmit bandwidth?"
- "How much data is moving over NVLink?"
- "Is this GPU's memory degrading?"
- "How many ECC errors has this GPU had?"
- "Are any pages pending retirement?"
- "Is this GPU's NVLink connection stable?"

These illustrate intent and are not an exhaustive whitelist.

## Do Not Use

Do not use this reference for:

- Host CPU, host memory, load, swap, filesystem, or other Node Exporter
  measurements → `node-exporter/overview.md`
- DCGM metrics not currently defined in this skill.
- Measurements for which no supported DCGM metric or derived/composed
  measurement is defined.
- Any DCGM metric not listed in the Metric Directory below.

## Metric Directory

Exhaustive for the metrics currently supported by this skill. Every supported
metric appears here in addition to its detailed definition in the relevant
domain file; the domain file's detailed definition is authoritative for final
metric selection.

| Domain | Intent / Measurement | Metric | Detail File |
|---|---|---|---|
| compute | Overall GPU utilization | `DCGM_FI_DEV_GPU_UTIL` | `compute.md` |
| compute | Graphics/SM engine active time | `DCGM_FI_PROF_GR_ENGINE_ACTIVE` | `compute.md` |
| compute | Tensor Core activity | `DCGM_FI_PROF_PIPE_TENSOR_ACTIVE` | `compute.md` |
| compute | FP64 pipeline utilization | `DCGM_FI_PROF_PIPE_FP64_ACTIVE` | `compute.md` |
| compute | FP32 pipeline utilization | `DCGM_FI_PROF_PIPE_FP32_ACTIVE` | `compute.md` |
| compute | FP16 pipeline utilization | `DCGM_FI_PROF_PIPE_FP16_ACTIVE` | `compute.md` |
| thermal | GPU core temperature | `DCGM_FI_DEV_GPU_TEMP` | `thermal.md` |
| thermal | GPU memory/HBM temperature | `DCGM_FI_DEV_MEMORY_TEMP` | `thermal.md` |
| thermal | Instantaneous GPU power consumption | `DCGM_FI_DEV_POWER_USAGE` | `thermal.md` |
| thermal | Time spent power throttled | `DCGM_FI_DEV_POWER_VIOLATION` | `thermal.md` |
| thermal | Current GPU SM/core clock | `DCGM_FI_DEV_SM_CLOCK` | `thermal.md` |
| thermal | Current GPU memory clock | `DCGM_FI_DEV_MEM_CLOCK` | `thermal.md` |
| memory | Used GPU memory (VRAM) | `DCGM_FI_DEV_FB_USED` | `memory.md` |
| memory | Free GPU memory (VRAM) | `DCGM_FI_DEV_FB_FREE` | `memory.md` |
| memory | Memory controller utilization | `DCGM_FI_DEV_MEM_COPY_UTIL` | `memory.md` |
| memory | DRAM bandwidth utilization | `DCGM_FI_PROF_DRAM_ACTIVE` | `memory.md` |
| interconnect | PCIe transmit traffic | `DCGM_FI_PROF_PCIE_TX_BYTES` | `interconnect.md` |
| interconnect | PCIe receive traffic | `DCGM_FI_PROF_PCIE_RX_BYTES` | `interconnect.md` |
| interconnect | NVLink transmit traffic | `DCGM_FI_PROF_NVLINK_TX_BYTES` | `interconnect.md` |
| interconnect | NVLink receive traffic | `DCGM_FI_PROF_NVLINK_RX_BYTES` | `interconnect.md` |
| reliability | Single-bit ECC error volume | `DCGM_FI_DEV_ECC_SBE_VOL_TOTAL` | `reliability.md` |
| reliability | Double-bit ECC error volume | `DCGM_FI_DEV_ECC_DBE_VOL_TOTAL` | `reliability.md` |
| reliability | Pages retired due to single-bit errors | `DCGM_FI_DEV_RETIRED_SBE` | `reliability.md` |
| reliability | Pages retired due to double-bit errors | `DCGM_FI_DEV_RETIRED_DBE` | `reliability.md` |
| reliability | Pages currently pending retirement | `DCGM_FI_DEV_RETIRED_PENDING` | `reliability.md` |
| reliability | NVLink CRC (flit) errors | `DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL` | `reliability.md` |
| reliability | NVLink recovery events | `DCGM_FI_DEV_NVLINK_RECOVERY_ERROR_COUNT_TOTAL` | `reliability.md` |

Metric names, Prometheus types, measurements, and documented query intents come
from the project's DCGM metric reference.

## Derived / Composed Measurements

> No derived/composed measurements are currently defined for this exporter.

## Exporter Fundamentals

Concepts true across the entire exporter, or across two or more domain files.
A concept true for only one domain belongs in that domain file's Domain
Fundamentals instead. Datasource syntax, PromQL language rules, aggregation
behavior, and Counter/Gauge handling live in `prometheus-fundamentals.md`, not
here.

### Entity Scope Baseline

The primary entity represented by the DCGM Exporter metrics is the **GPU**.
DCGM metrics may represent GPUs across multiple nodes.

Label keys for DCGM Exporter metrics are sourced dynamically at query-generation
time — see `SKILL.md` §5 Principle 9. This reference intentionally does not
enumerate a static label-key catalog, because those keys are live schema
information that may vary by environment.

Preserve an explicitly specified node, GPU, or device scope when the user
provides it, using a label key confirmed by the runtime for the metric being
queried — never a label key assumed from this reference or by analogy with
another metric. If the runtime cannot confirm a label key for an explicitly
requested scope, do not guess one; use the existing `declined` /
`parameter_requires_clarification` path (`SKILL.md` §7.2, §8) rather than
silently dropping the constraint. Domain-specific dimension knowledge (which
semantic axes a metric varies along) belongs in the corresponding domain
file's Domain Fundamentals — see, for example, `compute.md`'s and
`thermal.md`'s notes on GPU/device-level dimensions.

### Cross-Domain Semantic Distinctions

**Memory bandwidth activity (`memory`) vs. compute-engine activity
(`compute`) — is the GPU compute-bound or memory-bound:**

`DCGM_FI_PROF_DRAM_ACTIVE` (`memory.md`) measures the memory subsystem's own
bandwidth activity, while `DCGM_FI_DEV_GPU_UTIL` and
`DCGM_FI_PROF_GR_ENGINE_ACTIVE` (`compute.md`) measure compute-engine
activity. A question like "is this GPU compute-bound or memory-bound?" is
answered by comparing a `memory`-domain metric against a `compute`-domain
metric, not by either domain alone — do not answer it using only one of the
two domains' metrics, and do not treat `DCGM_FI_PROF_DRAM_ACTIVE`'s
`DCGM_FI_PROF_*` naming and Gauge/percent shape as evidence that it belongs
with the `compute` domain's utilization metrics; it measures the memory
subsystem's own activity, not the compute engine's, and is assigned to
`memory` on that basis.

Other cross-domain distinctions (e.g. NVLink traffic vs. NVLink health) will
be documented here once the corresponding domain files exist.

**NVLink traffic (`interconnect`) vs. NVLink health (`reliability`) — same
physical subsystem, different measurement kind:**

`DCGM_FI_PROF_NVLINK_TX_BYTES`/`RX_BYTES` (`interconnect.md`) are throughput
counters — how much data moved over NVLink. `DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL`
and `DCGM_FI_DEV_NVLINK_RECOVERY_ERROR_COUNT_TOTAL` (`reliability.md`) are
error/health counters — is the link degrading. Both concern the same
physical subsystem (NVLink) but answer categorically different questions
(performance/throughput vs. hardware health), which is why they live in
different domain files rather than one. A user is more likely to conflate
"NVLink traffic" with "PCIe traffic" (same measurement kind, different
physical link — see `interconnect.md`'s Confusable Measurements) than with
"NVLink errors" (same link, different measurement kind) — but do not
substitute one for the other in either direction: a request about NVLink
traffic volume should not be answered with error counts, and a request about
NVLink health/stability should not be answered with traffic volume.

## Guardrails

- Route requests only to metrics defined in this Metric Directory.
- Use derived/composed measurements only when explicitly defined here.
- Use only metric names, units, dimensions, relationships, and semantics
  established in this skill's references, and only label keys confirmed by
  the runtime — never a label key established solely by a reference file (see
  `SKILL.md` §5 Principle 9).
- When information hasn't been verified against the datasource or runtime, mark
  it as requiring verification rather than guessing.
- Apply only node, GPU, device, or other scope constraints the user provided
  and that the runtime can confirm a label key for.
- Preserve every measurement explicitly requested by the user.
- Treat the Metric Directory as a routing aid only — verify final metric
  selection using the detailed definition in the relevant domain file.
