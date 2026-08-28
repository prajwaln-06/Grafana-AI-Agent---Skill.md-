Defines the host-level system observability domain covered by Node Exporter and
routes requests to the appropriate metric domain file. Does not contain detailed
metric definitions — see `cpu.md`, `memory.md`, and `filesystem.md` for those.

## Contents

- Quick Facts
- Trigger Examples
- Do Not Use
- Derived / Composed Measurements
- Exporter Fundamentals
- Guardrails

## Quick Facts

- **Data source:** prometheus
- **Covers:** Host-level system observability provided by Node Exporter
- **Domain files:** [cpu.md](cpu.md), [memory.md](memory.md),
  [filesystem.md](filesystem.md)

## Trigger Examples

Examples of user requests that route here:

- "What is the CPU utilization?"
- "How busy is the CPU?"
- "How much CPU time is idle?"
- "Are there a lot of context switches?"
- "How many interrupts are being handled?"
- "Is the system overloaded?"
- "What's the load average?"
- "How much memory is available?"
- "How much free RAM is there?"
- "How much memory is being used for cache?"
- "How much memory is being used for buffers?"
- "How much swap space is available?"
- "How much total swap space does the system have?"
- "How much disk space is available?"
- "What is the total disk capacity?"
- "Is this filesystem running low on space?"

These illustrate intent and are not an exhaustive whitelist.

## Do Not Use

Do not use this reference for:

- GPU utilization, GPU temperature, GPU power, GPU clocks, or other DCGM
  measurements → `dcgm-exporter/overview.md`
- Any Node Exporter metric not listed in the Metric Directory below.

Metric names and structured metadata are maintained in the generated catalog.
Metric-specific semantics remain in the linked domain files.

## Derived / Composed Measurements

> No derived/composed measurements are currently defined for this exporter.

## Exporter Fundamentals

Concepts true across the entire exporter, or across two or more domain files.
A concept true for only one domain belongs in that domain file's Domain
Fundamentals instead. Datasource syntax, PromQL language rules, aggregation
behavior, and Counter/Gauge handling live in `prometheus-fundamentals.md`, not
here.

### Entity Scope Baseline

The primary entity represented by the currently supported Node Exporter metrics
is the **node**, except for the `filesystem` domain, whose metrics scope to a
filesystem rather than the whole node — see `filesystem.md`.

Label keys for Node Exporter metrics are sourced dynamically at query-generation
time — see `SKILL.md` §5 Principle 9. This reference intentionally does not
enumerate a static label-key catalog for any domain, because those keys are
live schema information that may vary by environment.

Preserve an explicitly specified node, CPU, or filesystem scope when the user
provides it, using a label key confirmed by the runtime for the metric being
queried — never a label key assumed from this reference or by analogy with
another metric. If the runtime cannot confirm a label key for an explicitly
requested scope, do not guess one; use the existing `declined` /
`parameter_requires_clarification` path (`SKILL.md` §7.2, §8) rather than
silently dropping the constraint. Domain-specific dimension knowledge (which
semantic axes a metric varies along, such as `node_cpu_seconds_total`'s CPU
and mode dimensions) belongs in the corresponding domain file's Domain
Fundamentals.

### Cross-Domain Semantic Distinctions

> No cross-domain semantic distinctions are currently defined for this
> exporter. CPU utilization vs. system load is documented within the `cpu`
> domain file's Confusable Measurements section rather than here, since both
> metrics live in that one domain per the approved Phase 1 domain design.

## Guardrails

- Route requests only to metrics defined in the catalog.
- Use derived/composed measurements only when explicitly defined here.
- Use only metric names, units, dimensions, relationships, and semantics
  established in this skill's references, and only label keys confirmed by
  the runtime — never a label key established solely by a reference file (see
  `SKILL.md` §5 Principle 9).
- When information hasn't been verified against the datasource or runtime, mark
  it as requiring verification rather than guessing.
- Apply only node, CPU, filesystem, or other scope constraints the user
  provided and that the runtime can confirm a label key for.
- Preserve every measurement explicitly requested by the user.
- Verify final metric selection using the detailed definition in the relevant
  domain file.
