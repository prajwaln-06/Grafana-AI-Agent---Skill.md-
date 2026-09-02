Representative regression cases for `observability-query-builder`. Maintainer-run
(e.g. by hand or scripted against a Claude session using this skill), not agent-run
— Claude is not responsible for judging its own output's correctness. Compare
actual output against Expected for each case after any change to `SKILL.md` or a
reference file.

Each case lists: the input, the reference(s) that should be consulted, and the
expected `status` (and key fields) per SKILL.md §9.

---

## 1. Correct routing

**Input:** "What is the CPU utilization on node-03?"
**Expected:** Routes to `references/node-exporter/cpu.md` only (not `dcgm-exporter/*`).
`status: "ok"`, `measurement_used.name: "node_cpu_seconds_total"`, entity scope
`node-03` preserved in the query.

**Input:** "Is any GPU throttling right now?"
**Expected:** Routes to `references/dcgm-exporter/thermal.md`.
`status: "unsupported_metric"` for `DCGM_FI_DEV_POWER_VIOLATION`
specifically (see case 8) — not `ok`, even though the metric is identified
unambiguously.

## 2. Correct domain selection within a shared exporter

**Input:** "How much memory is available, and what's the CPU utilization?"
**Expected:** Two independently resolved measurements from two different domain
files under the same exporter (`node-exporter/memory.md`, `node-exporter/cpu.md`).
`mode: "multi"`, two entries in `results`.

## 3. Raw vs. derived classification

**Input:** "What's the average CPU utilization over the last hour?"
**Expected:** `measurement_used.type: "raw_metric"`, `name: "node_cpu_seconds_total"`.
Applying `avg()`/`rate()` does **not** make this a `derived_measurement` — Principle
7 (§5) explicitly prevents transformation-based misclassification here.

**Input:** "What's the memory bandwidth utilization?"
**Expected:** `status: "ok"`, `measurement_used.type: "raw_metric"`,
`name: "DCGM_FI_DEV_MEM_COPY_UTIL"`. The authoritative metric document's own
Typical Query Intent column lists "Memory bandwidth utilization" as the
documented trigger phrase for this specific metric — this is a
document-established phrase-to-metric mapping, not an assertion that
`DCGM_FI_DEV_MEM_COPY_UTIL` and `DCGM_FI_PROF_DRAM_ACTIVE` measure the same
underlying activity (that relationship remains unresolved — see case 24).

**Input (negative control, currently unsupported):** "What's the
compute-to-memory-bandwidth ratio for this GPU?" where no reference defines a
derived measurement combining a compute-activity metric and a
memory-bandwidth metric into a single ratio.
**Expected:** `status: "unsupported_metric"` — must not be answered by
inventing a derived relationship between two plausibly-related raw metrics
from different domains.

## 4. Explicit scope handling

**Input:** "Show CPU utilization for CPU 2 on node-7."
**Expected:** The node-7 entity constraint and the per-CPU ("CPU 2") constraint
are both preserved, each expressed using a label key confirmed by the runtime
for `node_cpu_seconds_total` at query-generation time (`SKILL.md` §5
Principle 9) — never a literal `cpu="2"` assumed from this reference. If the
runtime cannot confirm a label key for the per-CPU constraint specifically,
this must not be silently dropped — see case 19. No additional scope (e.g. a
specific time range beyond the default) is invented.

**Input:** "How busy is the CPU?" (no entity given)
**Expected:** No entity/node scope invented (§5 Principle 3, §8) — defaults to the
aggregate view, with the assumption stated in `explanation`. Must not silently pick
one arbitrary node.

## 5. Multiple explicitly requested measurements

**Input:** "Show me both total and free swap space."
**Expected:** Two independent results from `node-exporter/memory.md`
(`node_memory_SwapTotal_bytes` and `node_memory_SwapFree_bytes`) — `mode:
"multi"`. Must not be collapsed into a single derived "swap usage" measurement,
since `node-exporter/overview.md` explicitly defines no derived/composed
measurements for this exporter — each requested metric is a distinct raw metric,
not a derivation.

## 6. Ambiguity

**Input:** "How's the memory looking?"
**Expected:** `status: "ok"`, `reference_used: "references/node-exporter/memory.md"`,
`measurement_used.name: "node_memory_MemAvailable_bytes"`. This broad memory-state
question is resolved as the current available-memory overview metric rather than
as an ambiguous `MemAvailable_bytes` vs `MemFree_bytes` choice; the runtime's
documented default interpretation for a general memory-health ask is the
available-memory figure. Must not force a `status: "ambiguous_metric"` merely
because the domain includes multiple memory-related metrics.

## 7. Unsupported measurements

**Input:** "What's the disk I/O throughput on node-3?"
**Expected:** `status: "unmapped"` — no Node Exporter domain in this skill
defines a disk I/O throughput metric (only filesystem *capacity* is defined,
in `references/node-exporter/filesystem.md`). Must not attempt to construct a
plausible-looking query for a metric that isn't in any Metric Directory, and
must not substitute a filesystem capacity metric for an I/O throughput
request merely because both concern "disk."

*(Superseded case, kept for history: "What's the load average on node-3?" was
previously used as an unsupported-measurement example. Load average
(`node_load1`/`5`/`15`) was implemented in the Node Exporter extension and now
resolves via `references/node-exporter/cpu.md` — see case 1 for its current
expected routing behavior.)*

## 8. Counter-increase checks vs. unit-dependent interpretation (DCGM_FI_DEV_POWER_VIOLATION)

**Input:** "Is the GPU being power-throttled right now?"
**Expected:** `status: "ok"` — resolves to `DCGM_FI_DEV_POWER_VIOLATION`
(unambiguous), `measurement_used.type: "raw_metric"`. The query checks whether
the Counter increased over a recent window (e.g. `increase(...[5m]) > 0`),
which does not depend on knowing the Counter's exact exposed unit. Must not
produce `unsupported_metric` merely because the unit is unverified — the
unit-verification rule blocks unit-*dependent* interpretation (durations,
conversions), not unit-*independent* increase checks. Must not produce an
invented `measurement_used.type` value like `raw_counter`, `raw_index`, or
`raw_cluster_or_counter` — any PromQL transformation applied to a single
source metric is always `raw_metric` per §5 Principle 7.

**Input (separate case, unit-dependent):** "How many seconds has the GPU been
power throttled?"
**Expected:** `status: "unsupported_metric"` for `DCGM_FI_DEV_POWER_VIOLATION`
— the user is asking for a specific duration in seconds, which requires knowing
the Counter's exposed unit. That unit is explicitly unverified in
`thermal.md`'s metric definition. Must not invent a unit assumption (e.g.
"the counter is probably in microseconds") to answer this.

## 9. Prevention of invented metric names / labels

**Input:** "What's `node_cpu_percent` on node-3?" (a plausible-sounding but
non-existent metric name, supplied explicitly by the user as an identifier)
**Expected:** `status: "unsupported_metric"`. An explicitly supplied metric
identifier that does not exist in the Metric Directory must be rejected as
unsupported, not silently substituted — the invented name must never appear in
a constructed query, and the request must not be reinterpreted as if the user
had asked a natural-language question instead.

**Input (separate case, natural-language mapping):** "What's the CPU usage
percentage on node-3?"
**Expected:** `status: "ok"`, resolving to the real metric
`node_cpu_seconds_total` per its documented Intent Examples — this is ordinary
natural-language-to-metric mapping, not an invented identifier, and must succeed
deterministically.

## 10. Out-of-scope actions

**Input:** "Restart node-3's GPU driver."
**Expected:** `status: "out_of_scope_action"`. Must not be reinterpreted as a
monitoring question about GPU state.

## 11. Datasource-infrastructure-only boundary (OpenSearch)

**Input:** "Show me error log volume from OpenSearch over the last day."
**Expected:** `status: "unmapped"` — `opensearch-fundamentals.md` documents query
mechanics only; no domain/exporter reference currently exists for it. Must not be
answered using Prometheus-side reasoning, and must not be treated as `ok` just
because query-language fundamentals exist for OpenSearch.

## 12. Panic-mode handling

**Input:** "everything is on fire the node is dying help"
**Expected:** At least one domain signal present ("node") →
`status: "panic_mode_best_effort"` using the broadest reasonable interpretation
(likely CPU + memory overview), with the required `caveat` field present. Must not
return `declined` when a domain signal exists.

**Input:** "help everything is broken"
**Expected:** Zero domain signal → `status: "declined"`,
`reason: "parameter_requires_clarification"`, with a single narrow
`clarification` question.

**Input:** "wait my node is dying what do i do?"
**Expected:** `status: "declined"`, `reason: "parameter_requires_clarification"`,
with a `clarification` that asks for the affected host and symptoms (e.g. CPU,
memory, or GPU issue). This is not a valid monitoring query because it contains
zero specific domain signals or metric names to route to a reference; the request
is too vague for a metric answer even though it contains the word "node" and a
panic tone.

## 13. Node Exporter extension — correct routing to the filesystem domain

**Input:** "How much disk space is available on node-03?"
**Expected:** Routes to `references/node-exporter/filesystem.md` (not
`memory.md`, despite superficial "available space" wording overlap with
`node_memory_MemAvailable_bytes`). `status: "ok"`,
`measurement_used.name: "node_filesystem_avail_bytes"`.

## 14. Node Exporter extension — CPU utilization vs. system load (within-domain ambiguity boundary)

**Input:** "Is the CPU under heavy load?"
**Expected:** `status: "ok"`, `reference_used: "references/node-exporter/cpu.md"`,
resolving to the current 1-minute system load average (`node_load1`) rather
than CPU utilization (`node_cpu_seconds_total`). This phrase is interpreted as
the CPU reference's documented intent example for current system overload
("Is the system overloaded [right now]?") with `query_type: "instant"` and
`time_range: {"time": "now"}`. Must not treat this as an ambiguity requiring
clarification simply because the wording includes "CPU" and "load".

**Input (negative control — not ambiguous):** "Is the system overloaded?"
**Expected:** `status: "panic_mode_best_effort"` with a required `caveat`,
still resolving to the same broad `node_load1` system-load interpretation.
This is the same CPU-domain routing as the previous case, but the generic,
low-information panic-mode wording makes it a best-effort answer rather than a
definitive `ok` result.

**Input (negative control — not ambiguous):** "What's the CPU utilization?"
**Expected:** `status: "ok"`, resolving to `node_cpu_seconds_total` directly,
per the same reasoning — this phrasing matches CPU utilization's documented
intent examples with no genuine overlap with the load-average family's wording.

## 15. Node Exporter extension — Load window ambiguity

**Input:** "What's the load average on node-3?" (no window specified)
**Expected:** `status: "declined"`, `reason: "parameter_requires_clarification"`,
with a `clarification` question asking whether the user wants the 1-minute,
5-minute, or 15-minute load average. The underlying metrics (`node_load1`,
`node_load5`, and `node_load15`) are materially different measurements; the
request does not establish which, and the system must not assume a default
window. This remains distinct from the system-overload intent examples in case
14, where the reference explicitly supports a current `node_load1`
interpretation without a generic no-window ambiguity.

## 16. Node Exporter extension — filesystem avail vs. free, without inventing a reserved-space mechanism

**Input:** "How much free disk space is there for a regular user to use?"
**Expected:** Resolves to `node_filesystem_avail_bytes` (the "for non-root"
qualified figure), not `node_filesystem_free_bytes`. The `explanation` field
may cite the authoritative document's own "for non-root" wording as the basis
for the distinction, but must not assert an unverified mechanism (e.g.
reserved filesystem blocks) as the reason the two figures differ.

## 17. Node Exporter extension — prevention of invented labels on newly added metrics

**Input:** "What's the load average for CPU 2 on node-3?"
**Expected:** The node-level scope (`node-3`) may be preserved if the runtime
confirms a label key for it, but a per-CPU (e.g. `cpu="2"`) constraint must
**not** be added to a `node_load1`/`5`/`15` query by inventing a label key —
`node_load1`/`5`/`15` are node-level measurements with no documented CPU-level
dimension (`references/node-exporter/cpu.md`'s Confusable Measurements
section), and per `SKILL.md` §5 Principle 9, no label key may be invented by
analogy with `node_cpu_seconds_total`'s CPU dimension merely because both
metrics live in the same domain file. Similarly, "How much disk space is
available on the root filesystem?" must not produce a query with an invented
`mountpoint="/"` or `device` label — no such key is confirmed for
`node_filesystem_avail_bytes`. Either request should proceed using only
label keys the runtime actually confirms (or no additional label constraint
if none is confirmed and none is required), with the unavailability of a
more specific label noted in `explanation` rather than silently fabricated.

## 18. Dynamic label sourcing — label-key fabrication

**Input:** "What's the GPU utilization for GPU model H100 specifically?"
(assume runtime metadata for `DCGM_FI_DEV_GPU_UTIL` confirms a `gpu` index
label but establishes no label representing GPU *model/SKU*)
**Expected:** `status: "declined"`, `reason:
"parameter_requires_clarification"`. The user's scope concept ("GPU model")
cannot be mapped to any runtime-confirmed label key for this metric. Must not
guess a plausible-sounding key (e.g. `model`, `sku`, `gpu_model`) and must not
silently drop the model constraint and answer for all GPUs unfiltered.

## 19. Dynamic label sourcing — label value vs. label key

**Input:** "What's the CPU utilization on node-7?"
(assume runtime metadata confirms an `instance` label key represents node-level
scope for `node_cpu_seconds_total`)
**Expected:** `status: "ok"`. The label *value* `node-7` is used directly as
supplied by the user once the label *key* (`instance`) is confirmed by the
runtime — the skill does not independently re-verify that a series with
`instance="node-7"` actually exists in the current datasource before
constructing the query. Confirming the label key is the runtime-sourcing
requirement (`SKILL.md` §5 Principle 9); confirming the specific value exists
is a separate, out-of-scope concern for query construction.

## 20. Dynamic label sourcing — runtime label list unavailable for a required scope

**Input:** "What's the CPU utilization on node-7?" (assume the runtime label
metadata needed to express node-level scope for `node_cpu_seconds_total` is
unavailable at query-generation time — e.g. the runtime call failed or
returned nothing)
**Expected:** `status: "declined"`, `reason:
"parameter_requires_clarification"`. The user explicitly requires a scope
constraint (`node-7`) that cannot currently be expressed without a confirmed
label key. Must not guess a common label name (e.g. `instance`, `node`,
`hostname`) merely because it's a plausible convention, and must not silently
answer unfiltered as if the user hadn't specified a node.

**Input (negative control — must not over-trigger `declined`):** "What's the
CPU utilization?" (no entity given, same runtime-unavailability condition)
**Expected:** `status: "ok"`. This request needs no label constraint to be
answered — an unfiltered metric request must not become `declined` merely
because a runtime label list is unavailable, when no explicit scope
constraint actually requires one (`SKILL.md` §5 Principle 9's closing rule).

## 21. Preservation of the unresolved GPU utilization / graphics-engine unit discrepancy

**Input:** "Compare GPU utilization and graphics engine activity for GPU 2."
**Expected:** `mode: "multi"`, two independent results —
`DCGM_FI_DEV_GPU_UTIL` (unit: Percent) and `DCGM_FI_PROF_GR_ENGINE_ACTIVE`
(unit: Fraction/utilization ratio) — each reported in its own documented unit.
Must not convert, rescale, or normalize one figure to match the other's unit,
and must not silently present them as directly comparable percentages. This
exercises the unresolved unit-discrepancy flag preserved in
`references/dcgm-exporter/compute.md`'s Confusable Measurements section and
Guardrails since the DCGM bulk migration — it remains unresolved, not
something this or any later phase should silently normalize.

## 22. Node Exporter extension — explicit filesystem scope must not be silently discarded

**Input:** "How much disk space is available on /data?" (assume the runtime
cannot confirm a label key expressing a specific mountpoint for
`node_filesystem_avail_bytes` at query-generation time)
**Expected:** `status: "declined"`, `reason:
"parameter_requires_clarification"`, per
`references/node-exporter/filesystem.md`'s Domain-Specific Guardrails. The
user explicitly named a scope (`/data`) that cannot currently be expressed.
Must **not** silently drop the `/data` constraint and return `status: "ok"`
for the filesystem domain unfiltered — an explicit, unmappable scope
constraint is a distinct failure mode from the case-20 "no scope requested"
negative control and must not be collapsed into it. This is the corrected
behavior for the Phase 2 implementation issue where an explicit scope
constraint risked being dropped rather than surfaced.

## 23. DCGM memory extension — capacity vs. bandwidth/utilization, and the no-total guardrail

**Input:** "How much GPU memory is being used, and how much is free?"
**Expected:** Routes to `references/dcgm-exporter/memory.md`. `mode: "multi"`,
two results — `DCGM_FI_DEV_FB_USED` and `DCGM_FI_DEV_FB_FREE`, each in bytes.

**Input:** "What's the total GPU memory on GPU 3?"
**Expected:** `status: "unsupported_metric"`. No `DCGM_FI_DEV_FB_TOTAL` (or
equivalent total-capacity) metric is currently defined. Must **not** be
answered by silently summing `DCGM_FI_DEV_FB_USED` and `DCGM_FI_DEV_FB_FREE`
into an implied total — this is the guardrail from `memory.md`'s Confusable
Measurements section.

**Input:** "Is the GPU memory-bandwidth-bound?"
**Expected:** Routes to `references/dcgm-exporter/memory.md`, resolves to
`DCGM_FI_PROF_DRAM_ACTIVE` (category Memory Bandwidth), not
`DCGM_FI_DEV_FB_USED`/`FREE` (capacity) — a bandwidth/utilization question
must not be answered with a capacity metric merely because both concern GPU
memory.

## 24. DCGM memory extension — `DCGM_FI_DEV_MEM_COPY_UTIL` vs. `DCGM_FI_PROF_DRAM_ACTIVE` must not be asserted as equivalent

**Input:** "What is the memory controller utilization on GPU 1?"
**Expected:** `status: "ok"`, resolves specifically to
`DCGM_FI_DEV_MEM_COPY_UTIL`. Must not substitute or silently combine with
`DCGM_FI_PROF_DRAM_ACTIVE`.

**Input:** "What's the memory bandwidth utilization on GPU 1?"
**Expected:** `status: "ok"`, resolves specifically to
`DCGM_FI_DEV_MEM_COPY_UTIL` — this exact phrase is the authoritative
document's documented Typical Query Intent for this metric (see case 3). Must
not treat this phrase as ambiguous with `DCGM_FI_PROF_DRAM_ACTIVE`, and must
not use it as grounds to assert the two metrics measure the same underlying
activity — the document establishes this specific phrase-to-metric mapping,
not a general equivalence between the two metrics.

**Input:** "What's the DRAM bandwidth utilization on GPU 1?"
**Expected:** `status: "ok"`, resolves specifically to
`DCGM_FI_PROF_DRAM_ACTIVE` — the authoritative document's "What it Measures"
text for this metric is verbatim "DRAM bandwidth utilization (%)". Must not
substitute or silently combine with `DCGM_FI_DEV_MEM_COPY_UTIL`, and must not
use the shared word "bandwidth" as grounds to treat the two metrics as
interchangeable.

**Input:** "Compare memory controller utilization and DRAM bandwidth
utilization for GPU 2."
**Expected:** `mode: "multi"`, two independent results —
`DCGM_FI_DEV_MEM_COPY_UTIL` and `DCGM_FI_PROF_DRAM_ACTIVE` — each reported on
its own terms. Must not annotate them as redundant or as two views of the
same measurement; the authoritative reference does not establish that
relationship either way.

## 25. Cross-domain — compute-bound vs. memory-bound spans `compute.md` and `memory.md`

**Input:** "Is this GPU compute-bound or memory-bound right now?"
**Expected:** Consults both `references/dcgm-exporter/compute.md` (for
`DCGM_FI_DEV_GPU_UTIL` and/or `DCGM_FI_PROF_GR_ENGINE_ACTIVE`) and
`references/dcgm-exporter/memory.md` (for `DCGM_FI_PROF_DRAM_ACTIVE`), per
`dcgm-exporter/overview.md`'s Cross-Domain Semantic Distinctions entry for
this question. Must not answer using only one domain's metrics, and must not
treat `DCGM_FI_PROF_DRAM_ACTIVE`'s `DCGM_FI_PROF_*` naming as grounds for
routing it to `compute.md` instead of `memory.md`.

## 26. DCGM interconnect extension — correct routing and rate() preservation

**Input:** "What is the PCIe transmit bandwidth on GPU 0?"
**Expected:** Routes to `references/dcgm-exporter/interconnect.md`.
`status: "ok"`, `measurement_used.name: "DCGM_FI_PROF_PCIE_TX_BYTES"`,
`measurement_used.type: "raw_metric"`. Per `interconnect.md`'s Metric-Specific
Query/Result Semantics, `rate()` is the document-established query intent —
the raw counter alone must not be reported as "bandwidth."

**Input:** "How much data is moving over NVLink, received on GPU 2?"
**Expected:** `status: "ok"`, resolves to `DCGM_FI_PROF_NVLINK_RX_BYTES`, not
`DCGM_FI_PROF_PCIE_RX_BYTES` — the user named the link (NVLink) and direction
(received) explicitly.

## 27. DCGM interconnect extension — four-way metric ambiguity must not be silently resolved

**Input:** "What's the GPU bandwidth right now?" (no link or direction named)
**Expected:** `status: "ambiguous_metric"`, candidates
`DCGM_FI_PROF_PCIE_TX_BYTES`, `DCGM_FI_PROF_PCIE_RX_BYTES`,
`DCGM_FI_PROF_NVLINK_TX_BYTES`, `DCGM_FI_PROF_NVLINK_RX_BYTES`. Must not
default to PCIe over NVLink, or TX over RX, and must not invent a
combined/total-bandwidth figure across the four — no such derived measurement
is defined in this skill (see `interconnect.md`'s Domain-Specific
Guardrails).

**Input:** "What's the PCIe bandwidth?" (link named, direction unspecified)
**Expected:** Still `status: "ambiguous_metric"`, narrowed to
`DCGM_FI_PROF_PCIE_TX_BYTES` and `DCGM_FI_PROF_PCIE_RX_BYTES` only — naming
the link narrows but does not fully resolve the ambiguity absent a stated
direction.

## 28. DCGM interconnect extension — traffic volume vs. link health is not yet answerable for NVLink health

**Input:** "Is the NVLink connection healthy, or just how much traffic is it
carrying?"
**Expected:** The traffic-volume half (`DCGM_FI_PROF_NVLINK_TX_BYTES`/
`RX_BYTES`) resolves normally via `interconnect.md`. The health half is not
currently answerable — `DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL` and
`DCGM_FI_DEV_NVLINK_RECOVERY_ERROR_COUNT_TOTAL` are not yet implemented
(pending Phase 5, `reliability.md`). Must not substitute an NVLink traffic
metric as a proxy for link health, and must not silently drop the health half
of the request — the unsupported half should be reported as such (e.g.
`unsupported_metric` or `unmapped` for that portion) rather than answered
only with traffic volume and presented as a complete answer.

## 29. DCGM reliability extension — correct routing and SBE/DBE preservation

**Input:** "How many double-bit ECC errors has GPU 1 had?"
**Expected:** Routes to `references/dcgm-exporter/reliability.md`.
`status: "ok"`, `measurement_used.name: "DCGM_FI_DEV_ECC_DBE_VOL_TOTAL"` — not
`DCGM_FI_DEV_ECC_SBE_VOL_TOTAL`. The SBE/DBE distinction must not be
collapsed.

**Input:** "Are there any pending memory failures on this GPU?"
**Expected:** `status: "ok"`, resolves to `DCGM_FI_DEV_RETIRED_PENDING`, not
`DCGM_FI_DEV_RETIRED_SBE`/`DCGM_FI_DEV_RETIRED_DBE` — "pending" specifically
indicates the not-yet-retired Gauge metric, not an already-retired Counter.

## 30. DCGM reliability extension — ECC volume vs. retired pages must not be presented as causal

**Input:** "This GPU has had a lot of single-bit ECC errors — how many pages
has that caused to be retired?"
**Expected:** May report `DCGM_FI_DEV_ECC_SBE_VOL_TOTAL` and
`DCGM_FI_DEV_RETIRED_SBE` as two independent results, but must **not**
present retired-page count as caused by, derived from, or a consequence of
ECC error volume — the authoritative metric reference does not establish
that relationship (see `reliability.md`'s Confusable Measurements). The
`explanation` field must not assert or imply causation between the two
figures.

## 31. DCGM reliability extension — Gauge vs. Counter must not be flattened

**Input:** "Give me a full reliability summary for this GPU: ECC errors,
retired pages, and pending retirements."
**Expected:** `mode: "multi"`, five results
(`DCGM_FI_DEV_ECC_SBE_VOL_TOTAL`, `DCGM_FI_DEV_ECC_DBE_VOL_TOTAL`,
`DCGM_FI_DEV_RETIRED_SBE`, `DCGM_FI_DEV_RETIRED_DBE`,
`DCGM_FI_DEV_RETIRED_PENDING`). `DCGM_FI_DEV_RETIRED_PENDING`'s type must be
reported as `Gauge`, distinctly from the other four `Counter` metrics — must
not describe all reliability metrics uniformly as "error counters."

## 32. DCGM reliability extension — asymmetric rate()/increase() guidance must not be symmetrized

**Input:** "What's the single-bit ECC error trend on GPU 0?"
**Expected:** `status: "ok"`, resolves to `DCGM_FI_DEV_ECC_SBE_VOL_TOTAL`
with `increase()` noted as the query intent, per the metric's own
Metric-Specific Query/Result Semantics.

**Input:** "What's the double-bit ECC error trend on GPU 0?"
**Expected:** `status: "ok"`, resolves to `DCGM_FI_DEV_ECC_DBE_VOL_TOTAL`.
Must **not** apply `increase()` to this metric merely because its SBE
sibling has that note — `reliability.md` explicitly states no
`rate()`/`increase()` function is documented for this metric, and that
asymmetry must be preserved exactly.

**Input:** "What's the NVLink recovery-event trend on GPU 3?"
**Expected:** `status: "ok"`, resolves to
`DCGM_FI_DEV_NVLINK_RECOVERY_ERROR_COUNT_TOTAL`. Must **not** apply
`increase()` by analogy with `DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL`,
which does have that note — same asymmetry-preservation requirement as above.

## 33. Cross-domain — NVLink traffic vs. NVLink health spans `interconnect.md` and `reliability.md`

**Input:** "Is the NVLink connection healthy, or just how much traffic is it
carrying?" (re-run of case 28, now that `reliability.md` exists)
**Expected:** Both halves are now answerable. Traffic volume resolves via
`references/dcgm-exporter/interconnect.md`
(`DCGM_FI_PROF_NVLINK_TX_BYTES`/`RX_BYTES`); health resolves via
`references/dcgm-exporter/reliability.md`
(`DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL`/
`DCGM_FI_DEV_NVLINK_RECOVERY_ERROR_COUNT_TOTAL`), per
`dcgm-exporter/overview.md`'s Cross-Domain Semantic Distinctions entry for
this boundary. `mode: "multi"`. Must not substitute one measurement kind for
the other, and must not answer using only one of the two domain files.

**Input:** "What's the NVLink traffic?"
**Expected:** Routes to `interconnect.md` only — resolves per case 27's
ambiguity handling (link/direction unspecified beyond "NVLink" narrows away
PCIe but not TX/RX). Must not additionally surface
`reliability.md`'s NVLink health metrics as candidates — "traffic" is not
ambiguous with "health" per the Cross-Domain Semantic Distinctions entry.

## 34. Alert-rule creation (Section 12) — narrow exception vs. unchanged out-of-scope boundary

**Input:** "Create an alert if CPU usage on node-1 exceeds 90% for 5 minutes."
**Expected** (`alert_rule_creation_enabled = true`): `status:
"alert_rule_proposed"`. Resolves `node_cpu_seconds_total` via `cpu.md`'s
Step 3 metric-selection procedure — unchanged from a read question — then
builds `alert_rule.condition_query` via the exact same Step 5 procedure
Section 12.4 now specifies, which for this metric means reusing its verified
Query Example expression. `alert_rule.condition_query` must reuse that exact
base expression (scoped to `node-1` via a runtime-confirmed `instance`
label, per Principle 9); `alert_rule.comparison` must be `{"operator": ">",
"threshold": 90}` and `alert_rule.for_duration` must be `"5m"`, all taken
verbatim from the request, never a "reasonable-sounding" substitute. A
session id is returned; nothing is created in Grafana by this response —
§12.1's separate confirmation step is required before anything is written.

**Input:** "Alert me if GPU temperature gets too high."
**Expected:** No threshold value or comparison direction stated →
`status: "declined"`, `reason: "parameter_requires_clarification"`. Section
12.4 never invents a threshold merely because "too high" is directionally
unambiguous — the numeric value and comparison operator must both come from
the user, with no exception for a request that sounds clear in plain
English.

**Input:** "Create an alert if double-bit ECC error volume on GPU 0 goes
above 5, for 10 minutes."
**Expected:** `status: "alert_rule_proposed"`. `DCGM_FI_DEV_ECC_DBE_VOL_TOTAL`
resolves cleanly (same metric as case 32), and per the post-1.3 §12.4 its
alert condition is built via the same Step 5 procedure already used for the
read question in case 32 — this metric's query/result semantics are
established (a Counter with no `rate()`/`increase()` documented for it, per
its own Metric-Specific Query/Result Semantics), so it is NOT blocked by
Principle 8 the way a genuinely-unverified metric (e.g. a unit-dependent
reading of `DCGM_FI_DEV_POWER_VIOLATION`, see case 8) is. `alert_rule.
condition_query` must be built the same way case 32's read query was (the
raw counter, scoped to GPU 0 via a runtime-confirmed label), never copied
from `node_cpu_seconds_total`'s CPU expression or any other metric's
condition by analogy. `alert_rule.comparison` must be `{"operator": ">",
"threshold": 5}` and `alert_rule.for_duration` must be `"10m"`, verbatim
from the request. This case demonstrates alert-rule creation is no longer
CPU-only: it now covers every metric whose read-query construction already
succeeds, gated only by the same Principle 8 check that already gates reads
— see case 8's negative-control-style pairing below for a metric that
genuinely IS still blocked.

**Input (negative control — genuinely unsupported):** "Create an alert if
the GPU has been power-throttled for more than 30 seconds."
**Expected:** `status: "unsupported_metric"`. `DCGM_FI_DEV_POWER_VIOLATION`
resolves unambiguously, but this specific interpretation asks for a
duration in seconds, which requires the Counter's exposed unit — explicitly
unverified in `thermal.md` (same blocking condition as case 8's
unit-dependent read-query example). Because Step 5 itself would block this
exact interpretation for a read question, §12.4 blocks it identically for
alert-rule creation — not because alerting has a stricter bar, but because
it has the same one. A differently-phrased request that only needs a
Counter-increase check (e.g. "alert me if the GPU is power-throttled at
all in the next hour") is NOT blocked by this same metric, mirroring case
8's unit-independent increase-check example.

**Input:** "Silence the CPU alert on node-1."
**Expected:** `status: "out_of_scope_action"`. Must **not** be reinterpreted
as a request to create a new, differently-scoped alert merely because §12
now permits alert-rule *creation* — silencing an EXISTING alert remains
fully out of scope with no exception, exactly as before §12 existed. If a
request could plausibly be read either way (creation vs. mutating something
existing), the safer failure mode (`out_of_scope_action`) applies.

**Input:** "Delete the alert rule for low disk space."
**Expected:** `status: "out_of_scope_action"` — same reasoning as above;
deleting is a mutation of something that already exists, not a creation
request, regardless of phrasing.

**Input:** "Turn off the GPU temperature alert, then set up a new one for 85°C instead."
**Expected:** `status: "out_of_scope_action"` for the whole request, not a
`mode: "multi"` split into one out_of_scope_action entry and one
alert_rule_proposed entry. The request matches Step 1's silence/mutate
gating condition ("turn off the GPU temperature alert"), and Step 1 stops
the entire construction procedure immediately on the first matching gate
condition (§6) — it never proceeds far enough to also consider the
"set up a new one" clause. This is the existing gate-short-circuit
behavior applied unchanged, not a new rule specific to alert-rule creation.

**Input:** "Create an alert if CPU usage exceeds 90%." (re-run with
`alert_rule_creation_enabled = false`, this deployment's default)
**Expected:** `status: "out_of_scope_action"` — byte-for-byte the same
classification this request would have received before §12 existed. With
the feature flag off, the Router's prompt never even mentions alert-rule
creation or the `action_intent` field at all
(`app/pipeline.py`'s `_build_router_instructions`), so this is not a
special case requiring its own reasoning — it is the original, unmodified
behavior, which is the entire point of gating the addendum behind the flag
rather than behind in-prompt reasoning about deployment configuration.

## 35. `query_type` (Section 8/9) — instant value vs. range/trend

**Input:** "What is the CPU utilization right now?"
**Expected:** `status: "ok"`, `query_type: "instant"`, `time_range: {"time":
"now"}` (not `{"from", "to", "step"}`). This phrasing asks for a single
current value with no implied trend — before the `query_type` field existed
in the output contract, this resolved to a short-window range query
(effectively a one-point matrix) because the Generator had no way to signal
an instant read at all; `execution.series[].points` must now come back as a
single point per series via Prometheus's instant-query endpoint, not a
short window standing in for one.

**Input:** "How much memory is available?"
**Expected:** `status: "ok"`, `query_type: "instant"` — same reasoning:
"how much X is there" asks for a present value, not a trend.

**Input:** "Has CPU utilization been high over the last hour?"
**Expected:** `status: "ok"`, `query_type: "range"`, `time_range: {"from":
"now-1h", "to": "now", "step": "<...>"}`. Explicit trend/window language
("over the last hour," "been high") means a range query is correct here —
this case is the negative control confirming the instant default introduced
above does not over-trigger on genuinely trend-shaped questions.

**Input:** "What was CPU utilization on node-3 at 3pm yesterday?"
**Expected:** `status: "ok"`, `query_type: "instant"`, `time_range: {"time":
"now-<N>h"}` (or another single resolvable point expressing "3pm
yesterday" per `prometheus-fundamentals.md`'s Time Expression Grammar) — a
single named point in the past is still an instant read, not a range,
even though it isn't literally `"now"`.