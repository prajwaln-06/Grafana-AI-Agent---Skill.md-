---
name: [exporter-name]
description: [Briefly describe the observability domain covered by this sub-skill.]
data_source: [prometheus | opensearch]
version: [version-number]
---

# [Exporter Name] Metrics Sub-Skill

## 1. File-Level Routing

### Purpose

[Define the observability domain covered by this file and the exporter/data source it represents.]

### Trigger Examples

Examples of user requests that should route to this sub-skill:

- "[example]"
- "[paraphrased example]"
- "[example involving a specific entity]"
- "[example involving comparison or time]"

These examples illustrate intent and are not an exhaustive whitelist.

### Do Not Use

Do not use this sub-skill for:

- [different observability domain] -> `[appropriate sub-skill]`
- [another excluded or unsupported domain] -> `[appropriate handling]`

---

## 2. Metric Selection Procedure

After the main skill routes a request to this sub-skill:

1. Identify **all measurements explicitly requested by the user** before selecting a metric or metric composition.

2. Use the Metric Directory to identify the relevant semantic category and candidate metric(s).

3. Verify each candidate using its detailed:
   - Purpose
   - Use When
   - Do Not Use / Confusable With
   - Intent Examples

4. Check whether the requested measurement is:
   - directly represented by a single metric;
   - explicitly requesting multiple independent measurements; or
   - a derived/composed measurement requiring multiple source metrics.

5. Preserve all constraints explicitly provided by the user, such as:
   - node
   - CPU / GPU / device
   - direction
   - time range
   - comparison scope

6. Do not invent scope constraints that the user did not provide.

7. Select multiple independent metrics **only when the user explicitly requests multiple distinct measurements**.

8. A single requested measurement may legitimately require multiple source metrics when it is defined as a **derived/composed measurement** in this sub-skill. This is not the same as the user requesting multiple independent measurements.

9. Do not convert a vague or underspecified request into a multi-metric request merely to provide a more comprehensive answer.

10. If multiple materially different measurements could plausibly satisfy the request and the user has not indicated which one(s) they want, classify the request as **AMBIGUOUS** and request clarification.

    Ambiguity must not be resolved by:
    - arbitrarily choosing one plausible metric;
    - selecting all plausible metrics;
    - assuming the user wants a comprehensive overview.

11. If no metric or derived/composed measurement defined in this file represents the requested measurement, classify the request as unsupported.

    Do not invent:
    - metric names;
    - labels;
    - measurements;
    - relationships between metrics;
    - query expressions.

12. Once the metric(s), metric-specific semantics, and relevant scope are resolved, defer shared query construction, time handling, aggregation, final structured output, and generic error handling to the main skill.

---

## 3. Metric Directory

Use this directory for initial metric discovery.

**The Metric Directory must be exhaustive: every raw metric supported by this sub-skill must appear here.**

A metric must not exist only in the detailed Metric Definitions without a corresponding Metric Directory entry.

The detailed metric definitions below are authoritative for final metric selection.

| Category | Intent / Measurement | Metric |
|---|---|---|
| [category] | [what the user wants to measure] | `[metric_name]` |
| [category] | [related but distinct measurement] | `[metric_name]` |

---

## 4. Derived / Composed Measurements

Use this section when a **single conceptual measurement requested by the user requires multiple source metrics**.

This is different from a request that explicitly asks for multiple independent measurements.

Only define derived/composed measurements when the relationship is supported by the provided metric/reference information or has otherwise been explicitly verified for the project.

Do not invent relationships between metrics.

This section must always be retained to preserve a consistent sub-skill structure.

If this exporter currently has no supported derived/composed measurements, state:

> No derived/composed measurements are currently defined for this exporter.

In that case, do not add placeholder derived-measurement definitions below.

### 4.1 [DERIVED_MEASUREMENT_NAME]

**Purpose:**  
[Describe the conceptual measurement represented by this composition.]

**Use When:**
- [natural-language intent]
- [optional paraphrased intent]

**Source Metrics:**
- `[METRIC_A]`
- `[METRIC_B]`

**Relationship:**  
[Describe how the source measurements conceptually relate to produce the requested measurement.]

**Result Semantics:**  
[Describe what the resulting value represents and its expected unit, if known.]

**Relevant Scope / Dimensions:**
- [node / CPU / GPU / device / filesystem / interface / etc.]

**Intent Example:**
- "[representative user request for this derived measurement]"

Actual query-expression construction remains the responsibility of the main skill.

---

## 5. Local Fundamentals

This section contains concepts that apply across multiple metrics in this exporter but are not general query-language rules.

Do not duplicate general Counter/Gauge handling, range-vector behavior, aggregation, time-window handling, or shared query-construction rules from the main skill.

### 5.1 Entity Scope and Dimensions

[Describe the entities represented by this exporter and how user-provided scope should be preserved.]

For example:

- Preserve explicitly specified node/device constraints.
- Do not invent entity constraints that the user did not provide.
- When the user requests multiple entities, retain the applicable series.
- When entities must be compared, preserve the relevant dimension so they remain distinguishable.

Concrete label names should only be used when they are verified from the actual datasource/schema or provided project reference.

### 5.2 Metric Ambiguity vs Parameter Vagueness

Request clarification at the sub-skill level when the **requested measurement itself cannot be determined**.

For example, if several materially different metrics could represent what the user means and the request does not distinguish between them, the metric intent is ambiguous.

Do not classify a request as metric-ambiguous merely because query parameters or scope details were omitted.

Examples of omitted parameters may include:

- time range;
- node;
- device index;
- aggregation scope.

If the requested measurement can still be identified confidently, select the appropriate metric or derived measurement and defer handling of missing parameters, defaults, or additional query requirements to the main skill.

### 5.3 Confusable Metric Families

When three or more closely related metrics represent different aspects of the same broader concept, describe the distinction once in this section rather than duplicating every possible pairwise comparison across individual metric definitions.

For each family, explain:

- the broader concept shared by the metrics;
- what measurement each metric specifically represents;
- the semantic cues that distinguish them;
- when a broad user request is too ambiguous to select one safely.

Individual metric definitions may still reference the most important confusable metrics where useful.

### 5.4 [Exporter-Specific Semantic Distinction]

[Explain another concept shared by several metrics that helps distinguish user intents.]

For example:

- directional semantics;
- capacity vs utilization;
- hardware-component distinctions;
- total vs component measurements.

### 5.5 [Another Exporter-Specific Concept]

[Add only when the concept applies across multiple metrics and materially helps intent interpretation or query construction.]

---

## 6. Metric Definitions

### 6.1 `[METRIC_NAME]`

**Category:**  
[semantic category]

**Purpose:**  
[Precisely state what this metric measures.]

**Type:**  
`Gauge | Counter`

**Unit:**  
[unit exactly as specified by the project metric/reference source]

**Use When:**
- [natural-language intent]
- [paraphrased intent]
- [another intent represented by this metric]

**Do Not Use / Confusable With:**
- [similar but different intent] -> `[correct_metric]`
- [another confusable intent] -> `[correct_metric]`

For large confusable metric families, avoid repeating every pairwise relationship here when the distinction is already defined under Local Fundamentals.

**Relevant Scope / Dimensions:**
- [node / CPU / GPU / device / filesystem / interface / etc.]

**Known Labels:**
- `[label_name]` — [what this label represents]
- `[label_name]` — [what this label represents]

Only include labels verified from the actual datasource/schema or explicitly provided project reference.

If labels have not yet been verified, state:

`Not yet verified from the available datasource/schema.`

Do not infer or invent label names.

**Intent Examples:**
- "[one representative natural-language request]"
- "[optional second example using different wording]"

Keep examples minimal. They demonstrate metric usage and semantic intent; they are not intended to enumerate every possible user phrasing.

**Edge / Confusable Example:**
- "[example that could easily be confused with this metric]" -> `[expected metric / derived measurement / AMBIGUOUS]`

Include this only when it materially helps distinguish the metric from a closely related measurement or demonstrates an important edge case.

**Metric-Specific Query / Result Semantics:**  
[Describe metric-specific information required to construct or interpret the eventual query correctly.]

This field should describe **what the metric needs semantically**, not duplicate general query-language syntax from the main skill.

For example, where supported by the project reference, this may specify:

- whether the raw value itself represents the requested measurement;
- whether the meaningful user-facing measurement represents change/rate over time;
- whether the meaningful result represents an event count over a period;
- whether a particular label value or dimension is semantically required;
- whether metric-specific aggregation or interpretation is required;
- what the resulting value represents.

For **Counter** metrics, this section must explicitly state the intended user-facing interpretation when that interpretation cannot be determined solely from the shared Counter/query rules in the main skill.

If the supplied reference does not provide enough information to determine the required semantics, state that the behavior requires verification rather than guessing.

### Query Examples (Optional)

Use this subsection only when the query construction or required
transformation for this metric is non-obvious and the example teaches
something not already conveyed by Purpose, Use When, Known Labels, or
Metric-Specific Query / Result Semantics.

Examples are particularly valuable for:
- Counter metrics requiring specific PromQL functions (e.g., `rate()`,
  `increase()`) or arithmetic transformations.
- Metrics requiring non-obvious aggregations or filtering.
- Derived/composed measurements.
- Cases where vector matching or similar query-construction details are
  important for correctness.

For simple metrics whose retrieval is straightforward, this subsection
may be omitted. In such cases, the preceding sections already provide
sufficient information for correct query construction.

Until the datasource's label schema has been verified, examples should
describe the expected query pattern or transformation in natural
language rather than using literal PromQL with assumed label names.

Once the metric labels have been verified from the datasource/schema,
literal query examples may be added where they materially improve
correctness or reduce ambiguity.

---

### 6.2 `[NEXT_METRIC_NAME]`

[Repeat the same structure.]

---

## 7. Sub-Skill Guardrails

- Only select raw metrics explicitly defined in this sub-skill.
- Only use derived/composed measurements explicitly defined in this sub-skill.
- Treat the metric names, types, units, and metric facts supplied by the project reference as authoritative for this implementation.
- Do not silently replace supplied metric metadata with assumptions from general domain knowledge.
- Do not fabricate metric names, labels, units, dimensions, relationships, or metric semantics.
- If required information is absent from the supplied reference and has not been verified against the datasource/schema, mark it as requiring verification rather than guessing.
- Do not assume scope constraints that the user did not specify.
- Preserve all explicitly requested measurements.
- Multiple plausible metrics do **not** imply that the user requested multiple metrics.
- A derived measurement requiring multiple source metrics is distinct from a request for multiple independent measurements.
- If multiple materially different measurements remain plausible, request clarification.
- Do not treat missing optional query parameters as metric ambiguity when the requested measurement is already clear.
- Treat the Metric Directory as a routing aid; verify final metric selection using the detailed metric definition.
- Use Local Fundamentals for semantic distinctions shared across groups of related metrics instead of unnecessarily duplicating them in every metric definition.
- Defer shared query-construction rules to the main skill.
- Defer default handling for missing query parameters to the main skill.
- Defer final structured-output formatting to the main skill.
- Defer generic malformed-input, adversarial-input, and cross-cutting error handling to the main skill.