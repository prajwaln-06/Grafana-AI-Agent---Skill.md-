Defines the metrics available for filesystem/disk capacity under Node Exporter,
and the semantics needed to select and query each one correctly.

## Contents

- Quick Facts
- Domain Fundamentals
- Metric Definitions
  - `node_filesystem_size_bytes`
  - `node_filesystem_avail_bytes`
  - `node_filesystem_free_bytes`
- Domain-Specific Guardrails

## Quick Facts

- **Parent exporter:** node-exporter
- **Domain:** filesystem
- **Covers:** Filesystem/disk capacity
- **Metric count:** 3
- **Merged from:** Filesystem — a new domain per the approved Phase 1 domain
  design. Not folded into the existing `memory` domain: disk capacity and RAM
  capacity are different physical resources, and `node-exporter/overview.md`'s
  own prior Do Not Use section already treated filesystem measurements as a
  distinct, previously-undefined category from memory.

## Domain Fundamentals

Concepts true across this functional domain only. A concept true across
multiple domains belongs in `overview.md`'s Exporter Fundamentals instead.

### Common Labels & Dimensions

Label keys for this domain's metrics are sourced dynamically at query-generation
time — see `SKILL.md` §5 Principle 9. The authoritative document has no label
information for any metric in this domain, and none is invented here. If the
user requests a specific filesystem/mountpoint/device scope, that scope must
be expressed using a label key the runtime confirms — not a name assumed by
convention (for example, `device`, `mountpoint`, or `fstype`) merely because
those names are plausible for a filesystem metric.

### Confusable Measurements

**Filesystem Size vs. Available vs. Free:**

| Metric | What it measures | Use for |
|---|---|---|
| `node_filesystem_size_bytes` | Filesystem size | Total disk capacity |
| `node_filesystem_avail_bytes` | Available disk space **for non-root** | Free disk space (as usable by a non-root process) |
| `node_filesystem_free_bytes` | Total free disk space | Remaining capacity |

The one distinction the authoritative document itself establishes is that
`node_filesystem_avail_bytes`'s definition explicitly says "for non-root,"
while `node_filesystem_free_bytes`'s definition does not include that
qualifier. Beyond that quoted distinction, the document does not explain why
the two figures might differ (for example, it does not describe any reserved-
space mechanism) — do not assert an explanation beyond the "for non-root"
wording itself. Do not treat `avail` and `free` as interchangeable, and do not
treat either as equivalent to `size`, which represents total capacity rather
than remaining space.

## Metric Definitions

### `node_filesystem_size_bytes`

- **Category:** Filesystem Capacity
- **Purpose:** Measures filesystem size.
- **Type:** `Gauge`
- **Unit:** bytes (per the metric name's `_bytes` suffix; no separate Unit
  field is given in the authoritative document).
- **Use when:** the user asks for total disk capacity; the total size of a
  filesystem; or wants to compare filesystem capacity.
- **Do not use / confusable with:** available disk space for non-root use →
  `node_filesystem_avail_bytes`; total free disk space →
  `node_filesystem_free_bytes`. Do not treat total capacity as remaining
  space.
- **Relevant scope:** Filesystem (not a label list). The specific
  device/mountpoint/filesystem this applies to is not established by the
  authoritative document — see Common Labels & Dimensions above.
- **Additional known labels:** not established by the authoritative document.
  Label keys are sourced dynamically from the runtime at query-generation time
  (`SKILL.md` §5 Principle 9) — do not invent a device/mountpoint/filesystem-
  type label name by convention.
- **Intent examples:** "What is the total disk capacity?", "How large is this
  filesystem?"
- **Edge/confusable example:** user asks how much space remains rather than
  total capacity → use `node_filesystem_avail_bytes` or
  `node_filesystem_free_bytes`, not this metric.
- **Metric-specific query/result semantics:** directly represents filesystem
  size as a Gauge. No per-metric override of `SKILL.md` defaults is currently
  defined.
- **Query examples:** no verified PromQL query example is currently available.
  Do not invent a literal query example.

### `node_filesystem_avail_bytes`

- **Category:** Filesystem Capacity
- **Purpose:** Measures available disk space for non-root use, per the
  authoritative document's own "for non-root" qualifier.
- **Type:** `Gauge`
- **Unit:** bytes (per the metric name's `_bytes` suffix; no separate Unit
  field is given in the authoritative document).
- **Use when:** the user asks for free disk space; how much space is
  available for use; or whether a filesystem is running low on usable space.
- **Do not use / confusable with:** total filesystem capacity →
  `node_filesystem_size_bytes`; total free disk space (a different, unqualified
  figure per the authoritative document) → `node_filesystem_free_bytes`. Do
  not treat available and free space as interchangeable — see Confusable
  Measurements above.
- **Relevant scope:** Filesystem (not a label list). The specific
  device/mountpoint/filesystem this applies to is not established by the
  authoritative document — see Common Labels & Dimensions above.
- **Additional known labels:** not established by the authoritative document.
  Label keys are sourced dynamically from the runtime at query-generation time
  (`SKILL.md` §5 Principle 9) — do not invent a device/mountpoint/filesystem-
  type label name by convention.
- **Intent examples:** "How much disk space is available?", "Is this
  filesystem running low on space?"
- **Edge/confusable example:** user asks for total free space without the
  non-root qualification → use `node_filesystem_free_bytes`, not this metric,
  unless the distinction is not meaningful to the request.
- **Metric-specific query/result semantics:** directly represents available
  disk space for non-root use as a Gauge. No per-metric override of
  `SKILL.md` defaults is currently defined.
- **Query examples:** no verified PromQL query example is currently available.
  Do not invent a literal query example.

### `node_filesystem_free_bytes`

- **Category:** Filesystem Capacity
- **Purpose:** Measures total free disk space.
- **Type:** `Gauge`
- **Unit:** bytes (per the metric name's `_bytes` suffix; no separate Unit
  field is given in the authoritative document).
- **Use when:** the user asks for remaining filesystem capacity, or total free
  disk space without a non-root qualification.
- **Do not use / confusable with:** total filesystem capacity →
  `node_filesystem_size_bytes`; available space for non-root use (a distinct,
  qualified figure per the authoritative document) →
  `node_filesystem_avail_bytes`. Do not treat free and available space as
  interchangeable — see Confusable Measurements above.
- **Relevant scope:** Filesystem (not a label list). The specific
  device/mountpoint/filesystem this applies to is not established by the
  authoritative document — see Common Labels & Dimensions above.
- **Additional known labels:** not established by the authoritative document.
  Label keys are sourced dynamically from the runtime at query-generation time
  (`SKILL.md` §5 Principle 9) — do not invent a device/mountpoint/filesystem-
  type label name by convention.
- **Intent examples:** "What is the remaining disk capacity?", "How much free
  disk space is there?"
- **Edge/confusable example:** user's wording implies non-root usable space
  specifically → use `node_filesystem_avail_bytes`, not this metric.
- **Metric-specific query/result semantics:** directly represents total free
  disk space as a Gauge. No per-metric override of `SKILL.md` defaults is
  currently defined.
- **Query examples:** no verified PromQL query example is currently available.
  Do not invent a literal query example.

## Domain-Specific Guardrails

- Do not treat filesystem size, available space, and free space as
  interchangeable measurements.
- Do not assert a mechanism (such as reserved space) for why available and
  free space might differ beyond the authoritative document's own "for
  non-root" qualifier.
- Do not invent filesystem-related labels (device, mountpoint, filesystem
  type, or otherwise) by convention — label keys must be confirmed by the
  runtime, never assumed from this reference (`SKILL.md` §5 Principle 9).
- Do not assume a specific filesystem, device, or mountpoint unless the user
  provides the relevant constraint.
- If the user explicitly requests a specific filesystem/device/mountpoint
  scope and the runtime cannot confirm a label key for it, do not silently
  drop the constraint and answer unfiltered — use the `declined` /
  `parameter_requires_clarification` path (`SKILL.md` §7.2, §8) instead.
- Do not invent a PromQL expression when the required query semantics have not
  been verified.
