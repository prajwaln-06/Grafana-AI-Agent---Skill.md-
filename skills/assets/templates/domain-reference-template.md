<!--
TEMPLATE — domain metric-reference file.

Use this for a new domain file under an exporter (e.g. references/<exporter>/<domain>.md).
Fill in every [bracketed] placeholder. Do NOT add YAML frontmatter — use the
plain-markdown "Quick Facts" block below instead; only SKILL.md's frontmatter is
validated against the Agent Skills spec.

Note the "Confusable Measurements" section below intentionally merges what earlier
project files called "Confusable Metric Families" and "Domain-Specific Semantic
Distinctions" into one section. Keep it that way — writing the same distinction
twice from two different angles adds no recognition value and is the single most
common source of duplication found in this project's earlier domain files. State
the distinction once, then reuse it (by reference, not by re-explanation) in the
Guardrails section at the bottom.

After creating this file, add its metrics to the parent exporter's Metric
Directory, and add one row to SKILL.md §4's routing table linking directly to
this file.
-->

Defines the metrics available for [domain scope] under [Exporter Name], and the
semantics needed to select and query each one correctly.

## Quick Facts

- **Parent exporter:** [exporter-name]
- **Domain:** [domain-id]
- **Covers:** [one-line scope — must exactly match the corresponding row in the
  parent overview's Metric Directory]
- **Metric count:** [number of metrics in this domain]
- **Merged from:** [original vendor category] — [reason for merging into this
  functional domain, so a future contributor doesn't need to reverse-engineer why
  these metrics are grouped together]

## Domain Fundamentals

Concepts true across this functional domain only. A concept true across multiple
domains belongs in the parent overview's Exporter Fundamentals instead.

### Common Labels & Dimensions

Document *semantic dimensions* a metric or group of metrics in this domain
varies along (for example, "this metric varies by CPU and mode/state," or
"this domain has no dimension beyond the exporter's entity-level scope") —
this is knowledge about what the metric means, useful for understanding and
disambiguating it. Do **not** document specific label *key* names as a
verified catalog: those are live schema information sourced dynamically from
the runtime at query-generation time (`SKILL.md` §5 Principle 9), not
something this reference enumerates.

If a metric has no additional documented dimension beyond the exporter-wide
entity scope, state exactly:

> No additional dimension beyond the exporter-wide entity scope is documented
> for this domain.

### Confusable Measurements

For each group of metrics in this domain that are commonly confused with each
other, state **once**: which metrics are involved, the key semantic difference
between them, and which to use for which kind of request. Do not also write a
separate "semantic distinctions" section restating the same difference in
different words — if a genuinely distinct semantic axis needs documenting
(e.g. "capacity vs. bandwidth" as opposed to "which metric is which"), add it as
its own entry in this same section, not a separate section.

Example format:

```
**[Metric A] vs. [Metric B] vs. [Metric C]**

| Metric | What it measures | Use for |
|---|---|---|
| `[metric_a]` | [...] | [...] |
| `[metric_b]` | [...] | [...] |

[One sentence on the boundary that must not be crossed, e.g. "A request about X
must not be answered with metric B merely because both are Y-related."]
```

If a domain genuinely has no confusable metrics (e.g. a single-metric domain),
state exactly:

> No confusable measurements are currently defined for this domain.

## Metric Definitions

Repeat this shape for every metric in the domain:

### `[METRIC_NAME]`

- **Category:** [a finer-grained semantic grouping within this domain; if the
  domain has no meaningful subdivision, this may simply restate the domain name.
  Where original vendor documentation had finer categories merged during
  consolidation, preserve that here.]
- **Purpose:** [precisely what this metric measures]
- **Type:** `Gauge` | `Counter`
- **Unit:** [exact unit from the verified project metric reference, e.g. bytes,
  seconds, percent, Celsius, Watts]
- **Use when:** [natural-language situations where this is the correct metric]
- **Do not use / confusable with:** [similar metric(s) that answer a different
  question, and why]
- **Relevant scope:** [what entity this measures — Node, GPU, Filesystem,
  Network Interface, etc. Not a label list.]
- **Additional known labels:** [describe any *semantic dimension* specific to
  this metric beyond the domain-common ones above — e.g. "varies by CPU and
  mode/state." Do NOT list specific label key names as a verified catalog;
  the exact keys are sourced dynamically from the runtime at query-generation
  time (`SKILL.md` §5 Principle 9). If this metric has no additional
  dimension beyond the domain-level scope, state exactly: "No additional
  dimension beyond the domain-level scope is documented for this metric.
  Label keys are sourced dynamically from the runtime at query-generation
  time — do not invent one." Never leave this field blank or omit it — an
  empty-looking field must never mean "not checked," and it must never be
  read as implying the exact label keys have already been verified and fixed
  by this reference.]
- **Intent examples:** ["representative user question", "another one"]
- **Edge/confusable example** *(optional)*: "user asks X → use metric Y, not
  this one"
- **Metric-specific query/result semantics:** [whether the raw value is
  meaningful; whether a rate/derivative is normally needed; whether a scope
  constraint is semantically required for the result to be meaningful (a
  semantic fact — the exact label key still comes from the runtime, per
  Principle 9); metric-specific aggregation behavior; interpretation of
  returned values. **This is also the designated location for any per-metric
  override of SKILL.md defaults** — e.g. a different comparison baseline, a
  different aggregation granularity, or — as with a metric whose exposed unit
  is unverified — an explicit instruction to resolve as `unsupported_metric`
  rather than build a query. If no override exists, state that SKILL.md
  defaults apply.]
- **Query examples** *(optional)*: only when construction is non-obvious or a
  verified example exists. State explicitly whether an example has been
  verified against a live datasource, or state exactly "No verified [...] query
  example is currently available. Do not invent a literal query example." —
  never leave this ambiguous by omission.
- **Alert query/threshold:** whether a verified alert *condition query* exists
  for this metric — i.e. an expression suitable for evaluating as a Grafana
  alert rule condition (SKILL.md §12), as distinct from a read-only Query
  Example above. Never leave this field blank or omit it — an empty-looking
  field must never be misread as "not checked." Two cases only:
  - **A verified Query Example exists for this metric** (per the field
    above): state explicitly that the SAME verified base query may be reused
    as the alert condition's expression, and explain the reasoning in one or
    two sentences. Never assert a default threshold *value* or comparison
    *direction* (`>`, `<`, etc.) unless the authoritative project material
    itself documents one — those almost never exist even when the base query
    is verified, so the overwhelmingly common phrasing is: "no default
    threshold value or comparison direction is established by the
    authoritative project material — both must be explicitly supplied by the
    user; do not invent either."
  - **No verified Query Example exists for this metric:** state exactly "No
    verified alert query or threshold is currently defined for this metric.
    Do not invent one." Do not attempt to derive an alert condition from an
    unverified query example — SKILL.md §5 Principle 1's "never fabricate"
    rule applies to alert conditions exactly as it does to read queries.

## Domain-Specific Guardrails

Guardrails unique to this domain — do not repeat exporter-wide guardrails from
the parent overview file, and do not re-explain a distinction already stated in
Confusable Measurements above; reference it instead (e.g. "Do not use X as a
substitute for Y — see Confusable Measurements above.").

If no domain-specific guardrails exist, this section may remain empty.
