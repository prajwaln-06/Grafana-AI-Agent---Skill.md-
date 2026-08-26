Defines the metrics available for GPU hardware error/degradation/health
signals — ECC errors, retired/pending-retirement pages, and NVLink
link-error counters — under DCGM Exporter, and the semantics needed to
select and query each one correctly.

## Contents

- Quick Facts
- Domain Fundamentals
- Metric Definitions
  - `DCGM_FI_DEV_ECC_SBE_VOL_TOTAL`
  - `DCGM_FI_DEV_ECC_DBE_VOL_TOTAL`
  - `DCGM_FI_DEV_RETIRED_SBE`
  - `DCGM_FI_DEV_RETIRED_DBE`
  - `DCGM_FI_DEV_RETIRED_PENDING`
  - `DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL`
  - `DCGM_FI_DEV_NVLINK_RECOVERY_ERROR_COUNT_TOTAL`
- Domain-Specific Guardrails

## Quick Facts

- **Parent exporter:** dcgm-exporter
- **Domain:** reliability
- **Covers:** Hardware error/degradation/health signals — ECC errors, retired
  and pending-retirement memory pages, and NVLink link-error counters
- **Metric count:** 7
- **Merged from:** ECC — retained as part of the broader Reliability
  functional domain because a general "is my GPU's memory degrading"
  question could plausibly mean ECC volume, retired pages, or pending
  retirement, and all three share this one Confusable Measurements
  relationship. Reliability — retained for the same reason; contains the
  retired/pending-retirement page metrics. NVLink Health — merged in because
  its query-intent question ("is something failing/degrading") is shared
  with ECC and retired-page metrics, not with NVLink's own traffic-volume
  metrics in `interconnect.md` — see this file's Domain-Specific Guardrails
  and `overview.md`'s Cross-Domain Semantic Distinctions for the boundary
  against `interconnect.md`.

## Domain Fundamentals

Concepts true across this functional domain only. A concept true across multiple
domains belongs in `overview.md`'s Exporter Fundamentals instead.

### Common Labels & Dimensions

Label keys for this domain's metrics are sourced dynamically at query-generation
time — see `SKILL.md` §5 Principle 9 and `overview.md` § Entity Scope Baseline.
This domain has no intrinsic dimension beyond the GPU-level entity scope
itself — each metric in this domain represents one error/degradation count
or gauge per GPU, without an additional semantic sub-dimension documented
here.

### Confusable Measurements

**ECC Errors vs. Retired Pages vs. Pending Retirement — three related but
distinct memory-degradation signals:**

| Metric | What it measures | Type | Use for |
|---|---|---|---|
| `DCGM_FI_DEV_ECC_SBE_VOL_TOTAL` | Total single-bit ECC errors | Counter | Error trend |
| `DCGM_FI_DEV_ECC_DBE_VOL_TOTAL` | Total double-bit ECC errors | Counter | Critical hardware errors |
| `DCGM_FI_DEV_RETIRED_SBE` | Pages retired due to single-bit errors | Counter | Memory degradation (page-retirement view) |
| `DCGM_FI_DEV_RETIRED_DBE` | Pages retired due to double-bit errors | Counter | Serious hardware degradation (page-retirement view) |
| `DCGM_FI_DEV_RETIRED_PENDING` | Pages currently pending retirement | **Gauge** | Pending memory failures (current outstanding count) |

Three distinctions in this table must be preserved exactly, not flattened:

1. **ECC volume vs. retired-page count are related but not established as
   causally linked.** `DCGM_FI_DEV_ECC_SBE_VOL_TOTAL` measures a cumulative
   *error count*; `DCGM_FI_DEV_RETIRED_SBE` measures a cumulative *page
   count*. A reader might assume retired pages are a downstream consequence
   of accumulated ECC volume — the authoritative metric reference places
   them in separate categories with no stated relationship. **Do not present
   one as causing, deriving from, or being redundant with the other.**
2. **SBE (single-bit) vs. DBE (double-bit) applies to both the ECC pair and
   the retired-page pair.** Preserve the document's own severity wording —
   "Error trend" / "Memory degradation" for the SBE variants vs. "Critical
   hardware errors" / "Serious hardware degradation" for the DBE variants —
   as the severity distinction; do not invent additional severity language
   beyond it.
3. **`DCGM_FI_DEV_RETIRED_PENDING` is a Gauge; its two Reliability-category
   siblings (`RETIRED_SBE`, `RETIRED_DBE`) are Counters.** `RETIRED_SBE`/
   `RETIRED_DBE` are cumulative totals to date; `RETIRED_PENDING` is the
   current outstanding count awaiting retirement. **Do not flatten this to
   "all three reliability metrics are counters"** — this is a concrete,
   easily-missed risk called out explicitly in the project's extension
   design.

**NVLink Health — CRC errors vs. recovery events:**

| Metric | What it measures | Use for |
|---|---|---|
| `DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL` | NVLink CRC (flit) errors | NVLink error/health trend |
| `DCGM_FI_DEV_NVLINK_RECOVERY_ERROR_COUNT_TOTAL` | NVLink recovery events | Link stability |

**NVLink Health vs. NVLink Traffic (cross-domain — see also
`overview.md`'s Cross-Domain Semantic Distinctions):** the two NVLink health
metrics above are a different measurement kind from
`DCGM_FI_PROF_NVLINK_TX_BYTES`/`RX_BYTES` in `interconnect.md` — traffic
volume (how much data moved) vs. health/error signals (is the link
degrading). Do not substitute an NVLink traffic metric for a link-health
question, or vice versa.

**Asymmetric `rate()`/`increase()` query-intent guidance — must be preserved
exactly, not symmetrized:** the authoritative metric reference explicitly
states `(increase())` as the typical query intent for
`DCGM_FI_DEV_ECC_SBE_VOL_TOTAL` and `DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL`
specifically — but states **no** function for
`DCGM_FI_DEV_ECC_DBE_VOL_TOTAL`, `DCGM_FI_DEV_NVLINK_RECOVERY_ERROR_COUNT_TOTAL`,
`DCGM_FI_DEV_RETIRED_SBE`, `DCGM_FI_DEV_RETIRED_DBE`, or
`DCGM_FI_DEV_RETIRED_PENDING`, even though each of these has an
obviously-parallel sibling that does state one. **Do not give an unstated
sibling a matching `rate()`/`increase()` note just because its parallel
metric has one** — see each metric's own Metric-Specific Query/Result
Semantics below for which function, if any, applies.

## Metric Definitions

### `DCGM_FI_DEV_ECC_SBE_VOL_TOTAL`

- **Category:** ECC
- **Purpose:** Measures the total count of single-bit ECC errors.
- **Type:** `Counter`
- **Unit:** Count (errors)
- **Use when:** the user asks about single-bit ECC error trends, single-bit
  memory error volume, or general ECC error activity without specifying
  double-bit errors.
- **Do not use / confusable with:** double-bit ECC errors →
  `DCGM_FI_DEV_ECC_DBE_VOL_TOTAL`; retired pages due to single-bit errors →
  `DCGM_FI_DEV_RETIRED_SBE` — related but not established as causally linked,
  see Confusable Measurements above.
- **Relevant scope:** GPU (not a label list).
- **Additional known labels:** sourced dynamically at query-generation time from the runtime — see `SKILL.md` §5 Principle 9. No additional dimension beyond the GPU-level scope is documented for this metric. Label keys are sourced dynamically from the runtime at query-generation time — do not invent one.
- **Intent examples:** "What is the single-bit ECC error trend?", "How many
  single-bit memory errors has this GPU had?"
- **Edge/confusable example:** user asks about retired pages rather than raw
  error count → use `DCGM_FI_DEV_RETIRED_SBE`; do not present this metric as
  the cause of retired pages.
- **Metric-specific query/result semantics:** the authoritative metric
  reference explicitly establishes `increase()` as the typical query intent
  for this metric — the raw counter value alone is not the meaningful
  quantity for an "error trend" question. No further per-metric override of
  `SKILL.md` defaults is currently defined.
- **Query examples:** no verified DCGM PromQL query example is currently
  available. Do not invent a literal query example.

### `DCGM_FI_DEV_ECC_DBE_VOL_TOTAL`

- **Category:** ECC
- **Purpose:** Measures the total count of double-bit ECC errors.
- **Type:** `Counter`
- **Unit:** Count (errors)
- **Use when:** the user asks about double-bit ECC errors specifically, or
  critical memory error activity.
- **Do not use / confusable with:** single-bit ECC errors →
  `DCGM_FI_DEV_ECC_SBE_VOL_TOTAL`; retired pages due to double-bit errors →
  `DCGM_FI_DEV_RETIRED_DBE` — related but not established as causally linked,
  see Confusable Measurements above.
- **Relevant scope:** GPU (not a label list).
- **Additional known labels:** sourced dynamically at query-generation time from the runtime — see `SKILL.md` §5 Principle 9. No additional dimension beyond the GPU-level scope is documented for this metric. Label keys are sourced dynamically from the runtime at query-generation time — do not invent one.
- **Intent examples:** "How many double-bit ECC errors has this GPU had?",
  "Are there any critical memory errors?"
- **Edge/confusable example:** user asks about single-bit
  errors rather than double-bit → use `DCGM_FI_DEV_ECC_SBE_VOL_TOTAL`.
- **Metric-specific query/result semantics:** the authoritative metric
  reference does **not** state a `rate()`/`increase()` function for this
  metric, unlike its SBE sibling — do not apply `increase()` to this metric
  merely by analogy with `DCGM_FI_DEV_ECC_SBE_VOL_TOTAL` (see the asymmetric
  query-intent note in Confusable Measurements above). No per-metric override
  of `SKILL.md` defaults beyond this is currently defined.
- **Query examples:** no verified DCGM PromQL query example is currently
  available. Do not invent a literal query example.

### `DCGM_FI_DEV_RETIRED_SBE`

- **Category:** Reliability
- **Purpose:** Measures the count of pages retired due to single-bit errors.
- **Type:** `Counter`
- **Unit:** Count (pages)
- **Use when:** the user asks how many memory pages have been retired due to
  single-bit errors, or about memory degradation from a
  page-retirement perspective.
- **Do not use / confusable with:** raw single-bit ECC error volume →
  `DCGM_FI_DEV_ECC_SBE_VOL_TOTAL` — related but not established as causally
  linked; pages retired due to double-bit errors →
  `DCGM_FI_DEV_RETIRED_DBE`; pages currently pending retirement (not yet
  retired) → `DCGM_FI_DEV_RETIRED_PENDING` — note the Type difference
  (Counter vs. Gauge), see Confusable Measurements above.
- **Relevant scope:** GPU (not a label list).
- **Additional known labels:** sourced dynamically at query-generation time from the runtime — see `SKILL.md` §5 Principle 9. No additional dimension beyond the GPU-level scope is documented for this metric. Label keys are sourced dynamically from the runtime at query-generation time — do not invent one.
- **Intent examples:** "How many pages have been retired due to single-bit
  errors?", "Is there memory degradation on this GPU?"
- **Edge/confusable example:** user asks about pages awaiting retirement
  rather than already retired → use `DCGM_FI_DEV_RETIRED_PENDING`.
- **Metric-specific query/result semantics:** the authoritative metric
  reference does **not** state a `rate()`/`increase()` function for this
  metric — do not apply one by analogy with
  `DCGM_FI_DEV_ECC_SBE_VOL_TOTAL`'s `increase()` note (see the asymmetric
  query-intent note in Confusable Measurements above). No per-metric override
  of `SKILL.md` defaults beyond this is currently defined.
- **Query examples:** no verified DCGM PromQL query example is currently
  available. Do not invent a literal query example.

### `DCGM_FI_DEV_RETIRED_DBE`

- **Category:** Reliability
- **Purpose:** Measures the count of pages retired due to double-bit errors.
- **Type:** `Counter`
- **Unit:** Count (pages)
- **Use when:** the user asks how many memory pages have been retired due to
  double-bit errors, or about serious hardware degradation
  from a page-retirement perspective.
- **Do not use / confusable with:** raw double-bit ECC error volume →
  `DCGM_FI_DEV_ECC_DBE_VOL_TOTAL` — related but not established as causally
  linked; pages retired due to single-bit errors →
  `DCGM_FI_DEV_RETIRED_SBE`; pages currently pending retirement (not yet
  retired) → `DCGM_FI_DEV_RETIRED_PENDING` — note the Type difference
  (Counter vs. Gauge), see Confusable Measurements above.
- **Relevant scope:** GPU (not a label list).
- **Additional known labels:** sourced dynamically at query-generation time from the runtime — see `SKILL.md` §5 Principle 9. No additional dimension beyond the GPU-level scope is documented for this metric. Label keys are sourced dynamically from the runtime at query-generation time — do not invent one.
- **Intent examples:** "How many pages have been retired due to double-bit
  errors?", "Is there serious hardware degradation on this GPU?"
- **Edge/confusable example:** user asks about single-bit-caused retirements
  rather than double-bit → use `DCGM_FI_DEV_RETIRED_SBE`.
- **Metric-specific query/result semantics:** the authoritative metric
  reference does **not** state a `rate()`/`increase()` function for this
  metric — do not apply one by analogy with its siblings' notes (see the
  asymmetric query-intent note in Confusable Measurements above). No
  per-metric override of `SKILL.md` defaults beyond this is currently
  defined.
- **Query examples:** no verified DCGM PromQL query example is currently
  available. Do not invent a literal query example.

### `DCGM_FI_DEV_RETIRED_PENDING`

- **Category:** Reliability
- **Purpose:** Measures the count of pages currently pending retirement.
- **Type:** `Gauge`
- **Unit:** Count (pages)
- **Use when:** the user asks how many pages are currently awaiting
  retirement, or about pending/imminent memory failures.
- **Do not use / confusable with:** pages already retired due to single-bit
  or double-bit errors → `DCGM_FI_DEV_RETIRED_SBE`/`DCGM_FI_DEV_RETIRED_DBE`
  — note this metric is a **Gauge** (current outstanding count), not a
  Counter like its two Reliability-category siblings; do not treat all three
  as interchangeable Counters, see Confusable Measurements above.
- **Relevant scope:** GPU (not a label list).
- **Additional known labels:** sourced dynamically at query-generation time from the runtime — see `SKILL.md` §5 Principle 9. No additional dimension beyond the GPU-level scope is documented for this metric. Label keys are sourced dynamically from the runtime at query-generation time — do not invent one.
- **Intent examples:** "How many pages are pending retirement?", "Are there
  any pending memory failures on this GPU?"
- **Edge/confusable example:** user asks about pages already retired rather
  than pending → use `DCGM_FI_DEV_RETIRED_SBE` or `DCGM_FI_DEV_RETIRED_DBE`
  depending on error type.
- **Metric-specific query/result semantics:** as a Gauge, this metric
  represents the current outstanding count directly — `rate()`/`increase()`
  are not applicable in the way they are for the Counter metrics in this
  domain. This is a Type-driven distinction, not an override of `SKILL.md`
  defaults.
- **Query examples:** no verified DCGM PromQL query example is currently
  available. Do not invent a literal query example.

### `DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL`

- **Category:** NVLink Health
- **Purpose:** Measures the count of NVLink CRC (flit) errors.
- **Type:** `Counter`
- **Unit:** Count (errors)
- **Use when:** the user asks about NVLink error trends, CRC errors
  specifically, or general NVLink health.
- **Do not use / confusable with:** NVLink recovery events →
  `DCGM_FI_DEV_NVLINK_RECOVERY_ERROR_COUNT_TOTAL`; NVLink traffic volume (a
  different measurement kind) → `DCGM_FI_PROF_NVLINK_TX_BYTES`/`RX_BYTES` in
  `interconnect.md` — see the NVLink Health vs. NVLink Traffic note in
  Confusable Measurements above and `overview.md`'s Cross-Domain Semantic
  Distinctions.
- **Relevant scope:** GPU (not a label list).
- **Additional known labels:** sourced dynamically at query-generation time from the runtime — see `SKILL.md` §5 Principle 9. No additional dimension beyond the GPU-level scope is documented for this metric. Label keys are sourced dynamically from the runtime at query-generation time — do not invent one.
- **Intent examples:** "What is the NVLink CRC error trend?", "Is this GPU's
  NVLink healthy?"
- **Edge/confusable example:** user asks how much data NVLink is carrying
  (not errors) → use `DCGM_FI_PROF_NVLINK_TX_BYTES`/`RX_BYTES` in
  `interconnect.md`, not this metric.
- **Metric-specific query/result semantics:** the authoritative metric
  reference explicitly establishes `increase()` as the typical query intent
  for this metric — the raw counter value alone is not the meaningful
  quantity for a "health trend" question. No further per-metric override of
  `SKILL.md` defaults is currently defined.
- **Query examples:** no verified DCGM PromQL query example is currently
  available. Do not invent a literal query example.

### `DCGM_FI_DEV_NVLINK_RECOVERY_ERROR_COUNT_TOTAL`

- **Category:** NVLink Health
- **Purpose:** Measures the count of NVLink recovery events.
- **Type:** `Counter`
- **Unit:** Count (events)
- **Use when:** the user asks about NVLink link stability, recovery events,
  or link-recovery activity specifically.
- **Do not use / confusable with:** NVLink CRC errors →
  `DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL`; NVLink traffic volume (a
  different measurement kind) → `DCGM_FI_PROF_NVLINK_TX_BYTES`/`RX_BYTES` in
  `interconnect.md` — see the NVLink Health vs. NVLink Traffic note in
  Confusable Measurements above and `overview.md`'s Cross-Domain Semantic
  Distinctions.
- **Relevant scope:** GPU (not a label list).
- **Additional known labels:** sourced dynamically at query-generation time from the runtime — see `SKILL.md` §5 Principle 9. No additional dimension beyond the GPU-level scope is documented for this metric. Label keys are sourced dynamically from the runtime at query-generation time — do not invent one.
- **Intent examples:** "How many NVLink recovery events has this GPU had?",
  "Is this GPU's NVLink connection stable?"
- **Edge/confusable example:** user asks about CRC errors rather than
  recovery events → use `DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL`.
- **Metric-specific query/result semantics:** the authoritative metric
  reference does **not** state a `rate()`/`increase()` function for this
  metric, unlike its CRC-error sibling — do not apply `increase()` to this
  metric merely by analogy with
  `DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL`'s note (see the asymmetric
  query-intent note in Confusable Measurements above). No per-metric override
  of `SKILL.md` defaults beyond this is currently defined.
- **Query examples:** no verified DCGM PromQL query example is currently
  available. Do not invent a literal query example.

## Domain-Specific Guardrails

- Do not present ECC error volume as the cause of, or as redundant with,
  retired-page counts — the authoritative reference does not establish that
  relationship (see Confusable Measurements above).
- Do not flatten `DCGM_FI_DEV_RETIRED_PENDING` (Gauge) into "all three
  reliability page-retirement metrics are counters" — verify Type per metric.
- Do not apply `rate()`/`increase()` to a metric whose Metric-Specific
  Query/Result Semantics does not state one, merely because a parallel
  sibling metric does — the document's asymmetric guidance must be preserved
  exactly, not symmetrized.
- Do not substitute an NVLink traffic metric (`interconnect.md`) for an
  NVLink health/error question in this domain, or vice versa.
- Do not invent Prometheus label names or label values — label keys must be
  confirmed by the runtime, never assumed from this reference (`SKILL.md` §5
  Principle 9).
- Do not assume a specific GPU, device, or node unless the user provides the
  relevant constraint.
- Do not invent a PromQL expression when the required query semantics have not
  been verified.
