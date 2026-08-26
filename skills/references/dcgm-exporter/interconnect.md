Defines the metrics available for GPU interconnect traffic (PCIe and NVLink)
under DCGM Exporter, and the semantics needed to select and query each one
correctly.

## Contents

- Quick Facts
- Domain Fundamentals
- Metric Definitions
  - `DCGM_FI_PROF_PCIE_TX_BYTES`
  - `DCGM_FI_PROF_PCIE_RX_BYTES`
  - `DCGM_FI_PROF_NVLINK_TX_BYTES`
  - `DCGM_FI_PROF_NVLINK_RX_BYTES`
- Domain-Specific Guardrails

## Quick Facts

- **Parent exporter:** dcgm-exporter
- **Domain:** interconnect
- **Covers:** Raw traffic throughput over PCIe and NVLink
- **Metric count:** 4
- **Merged from:** PCIe — retained as part of the broader Interconnect
  functional domain because it measures traffic over a GPU physical link.
  NVLink — merged into Interconnect rather than given its own domain because
  its traffic metrics are structurally identical to PCIe's (Counter, bytes,
  TX/RX, document-established `rate()` intent) and answer the same kind of
  question ("how much data is moving over a GPU link"), differing only in
  which physical link — keeping both pairs in one domain lets a vague "GPU
  bandwidth" request be recognized as ambiguous across all four candidates
  (see Confusable Measurements below) rather than being split before any
  candidate list exists. NVLink's error/health counters
  (`DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL`,
  `DCGM_FI_DEV_NVLINK_RECOVERY_ERROR_COUNT_TOTAL`) are a different measurement
  kind (health, not throughput) and belong to the `reliability` domain
  instead — see this file's Domain-Specific Guardrails and, once
  `reliability.md` exists, `overview.md`'s Cross-Domain Semantic
  Distinctions.

## Domain Fundamentals

Concepts true across this functional domain only. A concept true across multiple
domains belongs in `overview.md`'s Exporter Fundamentals instead.

### Common Labels & Dimensions

Label keys for this domain's metrics are sourced dynamically at query-generation
time — see `SKILL.md` §5 Principle 9 and `overview.md` § Entity Scope Baseline.
This domain has no intrinsic dimension beyond the GPU-level entity scope
itself — each metric in this domain represents one traffic-volume figure per
GPU/link direction, without an additional semantic sub-dimension documented
here.

### Confusable Measurements

**PCIe Traffic vs. NVLink Traffic — same TX/RX shape, different physical
link:**

| Metric | What it measures | Use for |
|---|---|---|
| `DCGM_FI_PROF_PCIE_TX_BYTES` | Bytes transmitted over PCIe | PCIe transmit bandwidth |
| `DCGM_FI_PROF_PCIE_RX_BYTES` | Bytes received over PCIe | PCIe receive bandwidth |
| `DCGM_FI_PROF_NVLINK_TX_BYTES` | Bytes transmitted over NVLink | NVLink transmit bandwidth |
| `DCGM_FI_PROF_NVLINK_RX_BYTES` | Bytes received over NVLink | NVLink receive bandwidth |

All four metrics are structurally identical (Counter, bytes, and the
authoritative metric reference explicitly establishes `rate()` as the typical
query intent for all four) and differ only in which physical link (PCIe vs.
NVLink) and direction (TX vs. RX) they measure. **A request for "GPU
bandwidth" or "GPU interconnect traffic" without naming a specific link is
genuinely metric-ambiguous across all four** — do not default to PCIe or
NVLink, and do not default to TX or RX, without the user specifying which. If
the user names a link (PCIe or NVLink) but not a direction, that narrows the
candidates to two (TX and RX for that link) — that remains ambiguous absent
further qualification unless a total combining both directions is
independently requested and no such combined/derived measurement is defined
in this skill.

**Interconnect Traffic vs. Interconnect Health** — traffic-volume metrics
(this domain) measure how much data moved, not whether the link is healthy or
degrading. NVLink's health/error counters
(`DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL`,
`DCGM_FI_DEV_NVLINK_RECOVERY_ERROR_COUNT_TOTAL`) are a different measurement
kind and, once implemented, belong to the `reliability` domain — do not
substitute an NVLink traffic metric for a link-health/error question or vice
versa.

## Metric Definitions

### `DCGM_FI_PROF_PCIE_TX_BYTES`

- **Category:** PCIe
- **Purpose:** Measures bytes transmitted over PCIe.
- **Type:** `Counter`
- **Unit:** Bytes
- **Use when:** the user asks about PCIe transmit traffic or PCIe transmit
  bandwidth specifically.
- **Do not use / confusable with:** PCIe receive traffic →
  `DCGM_FI_PROF_PCIE_RX_BYTES`; NVLink transmit traffic →
  `DCGM_FI_PROF_NVLINK_TX_BYTES` — see Confusable Measurements above; a
  vague, link-unspecified "GPU bandwidth" request → treat as metric-ambiguous
  across all four traffic metrics rather than defaulting to this one.
- **Relevant scope:** GPU (not a label list).
- **Additional known labels:** sourced dynamically at query-generation time from the runtime — see `SKILL.md` §5 Principle 9. No additional dimension beyond the GPU-level scope is documented for this metric. Label keys are sourced dynamically from the runtime at query-generation time — do not invent one.
- **Intent examples:** "What is the PCIe transmit bandwidth?", "How much data
  is being sent over PCIe on GPU 0?"
- **Edge/confusable example:** user asks about NVLink transmit traffic rather
  than PCIe → use `DCGM_FI_PROF_NVLINK_TX_BYTES`, not this metric.
- **Metric-specific query/result semantics:** the authoritative metric
  reference explicitly establishes `rate()` as the typical query intent for
  this metric — the raw counter value alone is not the meaningful quantity;
  a rate over time is normally needed to express "bandwidth." No further
  per-metric override of `SKILL.md` defaults is currently defined.
- **Query examples:** no verified DCGM PromQL query example is currently
  available. Do not invent a literal query example.

### `DCGM_FI_PROF_PCIE_RX_BYTES`

- **Category:** PCIe
- **Purpose:** Measures bytes received over PCIe.
- **Type:** `Counter`
- **Unit:** Bytes
- **Use when:** the user asks about PCIe receive traffic or PCIe receive
  bandwidth specifically.
- **Do not use / confusable with:** PCIe transmit traffic →
  `DCGM_FI_PROF_PCIE_TX_BYTES`; NVLink receive traffic →
  `DCGM_FI_PROF_NVLINK_RX_BYTES` — see Confusable Measurements above; a
  vague, link-unspecified "GPU bandwidth" request → treat as metric-ambiguous
  across all four traffic metrics rather than defaulting to this one.
- **Relevant scope:** GPU (not a label list).
- **Additional known labels:** sourced dynamically at query-generation time from the runtime — see `SKILL.md` §5 Principle 9. No additional dimension beyond the GPU-level scope is documented for this metric. Label keys are sourced dynamically from the runtime at query-generation time — do not invent one.
- **Intent examples:** "What is the PCIe receive bandwidth?", "How much data
  is coming in over PCIe on GPU 0?"
- **Edge/confusable example:** user asks about NVLink receive traffic rather
  than PCIe → use `DCGM_FI_PROF_NVLINK_RX_BYTES`, not this metric.
- **Metric-specific query/result semantics:** the authoritative metric
  reference explicitly establishes `rate()` as the typical query intent for
  this metric — the raw counter value alone is not the meaningful quantity;
  a rate over time is normally needed to express "bandwidth." No further
  per-metric override of `SKILL.md` defaults is currently defined.
- **Query examples:** no verified DCGM PromQL query example is currently
  available. Do not invent a literal query example.

### `DCGM_FI_PROF_NVLINK_TX_BYTES`

- **Category:** NVLink
- **Purpose:** Measures bytes transmitted over NVLink.
- **Type:** `Counter`
- **Unit:** Bytes
- **Use when:** the user asks about NVLink transmit traffic or NVLink
  transmit bandwidth specifically.
- **Do not use / confusable with:** NVLink receive traffic →
  `DCGM_FI_PROF_NVLINK_RX_BYTES`; PCIe transmit traffic →
  `DCGM_FI_PROF_PCIE_TX_BYTES` — see Confusable Measurements above; NVLink
  health/error activity (not traffic volume) → the `reliability` domain, once
  implemented — see Confusable Measurements above; a vague, link-unspecified
  "GPU bandwidth" request → treat as metric-ambiguous across all four traffic
  metrics rather than defaulting to this one.
- **Relevant scope:** GPU (not a label list).
- **Additional known labels:** sourced dynamically at query-generation time from the runtime — see `SKILL.md` §5 Principle 9. No additional dimension beyond the GPU-level scope is documented for this metric. Label keys are sourced dynamically from the runtime at query-generation time — do not invent one.
- **Intent examples:** "What is the NVLink transmit bandwidth?", "How much
  data is being sent over NVLink on GPU 0?"
- **Edge/confusable example:** user asks about PCIe transmit traffic rather
  than NVLink → use `DCGM_FI_PROF_PCIE_TX_BYTES`, not this metric.
- **Metric-specific query/result semantics:** the authoritative metric
  reference explicitly establishes `rate()` as the typical query intent for
  this metric — the raw counter value alone is not the meaningful quantity;
  a rate over time is normally needed to express "bandwidth." No further
  per-metric override of `SKILL.md` defaults is currently defined.
- **Query examples:** no verified DCGM PromQL query example is currently
  available. Do not invent a literal query example.

### `DCGM_FI_PROF_NVLINK_RX_BYTES`

- **Category:** NVLink
- **Purpose:** Measures bytes received over NVLink.
- **Type:** `Counter`
- **Unit:** Bytes
- **Use when:** the user asks about NVLink receive traffic or NVLink receive
  bandwidth specifically.
- **Do not use / confusable with:** NVLink transmit traffic →
  `DCGM_FI_PROF_NVLINK_TX_BYTES`; PCIe receive traffic →
  `DCGM_FI_PROF_PCIE_RX_BYTES` — see Confusable Measurements above; NVLink
  health/error activity (not traffic volume) → the `reliability` domain, once
  implemented — see Confusable Measurements above; a vague, link-unspecified
  "GPU bandwidth" request → treat as metric-ambiguous across all four traffic
  metrics rather than defaulting to this one.
- **Relevant scope:** GPU (not a label list).
- **Additional known labels:** sourced dynamically at query-generation time from the runtime — see `SKILL.md` §5 Principle 9. No additional dimension beyond the GPU-level scope is documented for this metric. Label keys are sourced dynamically from the runtime at query-generation time — do not invent one.
- **Intent examples:** "What is the NVLink receive bandwidth?", "How much
  data is coming in over NVLink on GPU 0?"
- **Edge/confusable example:** user asks about PCIe receive traffic rather
  than NVLink → use `DCGM_FI_PROF_PCIE_RX_BYTES`, not this metric.
- **Metric-specific query/result semantics:** the authoritative metric
  reference explicitly establishes `rate()` as the typical query intent for
  this metric — the raw counter value alone is not the meaningful quantity;
  a rate over time is normally needed to express "bandwidth." No further
  per-metric override of `SKILL.md` defaults is currently defined.
- **Query examples:** no verified DCGM PromQL query example is currently
  available. Do not invent a literal query example.

## Domain-Specific Guardrails

- Do not default to PCIe or NVLink, and do not default to TX or RX, when the
  user's request does not specify which — treat an unqualified "GPU
  bandwidth"/"interconnect traffic" request as metric-ambiguous across all
  four metrics in this domain.
- Do not combine TX and RX, or PCIe and NVLink, into a single reported "total
  bandwidth" figure — no such derived/composed measurement is currently
  defined in this skill.
- Do not substitute a traffic-volume metric in this domain for an NVLink
  health/error question (or vice versa) — traffic volume and link health are
  different measurement kinds, even for the same physical subsystem.
- Preserve the document-established `rate()` query intent for all four
  metrics in this domain — the raw counter value alone does not express
  "bandwidth."
- Do not invent Prometheus label names or label values — label keys must be
  confirmed by the runtime, never assumed from this reference (`SKILL.md` §5
  Principle 9).
- Do not assume a specific GPU, device, or node unless the user provides the
  relevant constraint.
- Do not invent a PromQL expression when the required query semantics have not
  been verified.
