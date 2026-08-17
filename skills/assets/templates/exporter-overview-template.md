<!--
TEMPLATE — exporter overview file.

Use this for a new exporter's overview.md. Fill in every [bracketed] placeholder.
Do NOT re-add a Metric Selection Procedure here — that procedure lives once, in
SKILL.md §6 Step 3, and applies identically to every exporter. Do NOT add YAML
frontmatter — this is a reference file, not a SKILL.md; only SKILL.md's
frontmatter is validated against the Agent Skills spec. Use the plain-markdown
"Quick Facts" block below instead.

After creating this file, add one row to SKILL.md §4's routing table linking
directly to it, and one row per domain file it points to (a domain file must be
directly reachable from SKILL.md — never only reachable by first opening this
overview file).
-->

Defines the [observability domain] covered by [Exporter Name] and routes requests
to the appropriate metric domain file. Does not contain detailed metric
definitions — see the linked domain files for those.

## Quick Facts

- **Data source:** [prometheus | opensearch]
- **Covers:** [one-line description of the observability domain this exporter covers]
- **Domain files:** [domain-a.md](domain-a.md), [domain-b.md](domain-b.md)

## Trigger Examples

Examples of user requests that route here:

- "[example]"
- "[paraphrased example]"
- "[example involving a specific entity]"
- "[example involving comparison or time]"

These illustrate intent and are not an exhaustive whitelist.

## Do Not Use

Do not use this reference for:

- [different observability domain] → `[appropriate reference]`
- [another unsupported domain] → `[appropriate handling, e.g. "not currently defined in this skill"]`
- Any metric not listed in the Metric Directory below.

## Metric Directory

Exhaustive for the metrics currently supported by this skill. Every supported
metric must appear here in addition to its detailed definition in the relevant
domain file; the domain file's detailed definition is authoritative for final
metric selection. A metric must never exist only inside a domain file without a
corresponding row here, or vice versa.

| Domain | Intent / Measurement | Metric | Detail File |
|---|---|---|---|
| [domain] | [what the user wants to measure] | `[metric_name]` | `[domain].md` |
| [domain] | [related but distinct measurement] | `[metric_name]` | `[domain].md` |

## Derived / Composed Measurements

Use this section only when a **single conceptual measurement requested by the
user requires multiple source metrics**, and only when that relationship is
supported by verified project reference information. Do not invent
relationships between metrics. All derived/composed measurements for this
exporter live here, even if their source metrics belong to the same domain file.

If none are currently defined, state exactly:

> No derived/composed measurements are currently defined for this exporter.

Do not create placeholder derived measurements.

<!-- If derived measurements exist, use this shape per measurement:

### [DERIVED_MEASUREMENT_NAME]

- **Purpose:** [the conceptual measurement this composition represents]
- **Use when:** [natural-language intent(s)]
- **Source metrics:** `[METRIC_A]`, `[METRIC_B]`
- **Relationship:** [how the source measurements conceptually relate]
- **Result semantics:** [what the resulting value represents, and its unit if known]
- **Relevant scope:** [node / CPU / GPU / device / etc.]
- **Intent example:** "[representative user request]"

Actual query construction remains SKILL.md's responsibility.
-->

## Exporter Fundamentals

Concepts true across the **entire exporter**, or across **two or more domains**.
A concept true for only one domain belongs in that domain file's Domain
Fundamentals instead. Do not duplicate datasource syntax, query-language rules,
aggregation behavior, or Counter/Gauge handling here — that lives in the relevant
datasource-fundamentals reference.

### Entity Scope Baseline

[Describe the exporter-wide entity model, e.g. "the primary entity is the node"
or "the primary entity is the GPU".]

Label keys for this exporter's metrics are sourced dynamically at
query-generation time — see `SKILL.md` §5 Principle 9. Do not enumerate a
static label-key catalog here; those keys are live schema information that
may vary by environment. If a semantic scope concept is genuinely useful for
understanding the exporter (e.g. "metrics are node-scoped" or "metrics may
span multiple nodes per GPU"), document that concept — never a specific label
key name presented as a verified catalog.

Preserve an explicitly specified scope when the user provides it, using a
label key confirmed by the runtime for the metric being queried. If the
runtime cannot confirm a label key for an explicitly requested scope, do not
guess one; use `declined` / `parameter_requires_clarification`
(`SKILL.md` §7.2, §8) rather than silently dropping the constraint.
Domain-specific dimension knowledge belongs only inside the corresponding
domain file.

### Cross-Domain Semantic Distinctions

Populate only when a distinction genuinely applies across multiple domains in
this exporter (e.g. capacity vs. utilization, total vs. component). If none
currently exist, state exactly:

> No cross-domain semantic distinctions are currently defined for this exporter.

## Guardrails

- Route requests only to metrics defined in this Metric Directory.
- Use derived/composed measurements only when explicitly defined here.
- Use only metric names, units, dimensions, relationships, and semantics
  established in this skill's references, and only label keys confirmed by
  the runtime — never a label key established solely by a reference file (see
  `SKILL.md` §5 Principle 9).
- When information hasn't been verified against the datasource or runtime,
  mark it as requiring verification rather than guessing.
- Apply only scope constraints the user provided and that the runtime can
  confirm a label key for.
- Preserve every measurement explicitly requested by the user.
- Treat the Metric Directory as a routing aid only — verify final metric
  selection using the detailed definition in the relevant domain file.
