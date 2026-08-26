<!--
TEMPLATE — datasource fundamentals file.

Use this when adding a new backend (e.g. Loki, Tempo) this skill can query.
Fill in every [bracketed] placeholder. Do NOT add YAML frontmatter — use the
one-line purpose statement below instead. This file documents query-language
mechanics only — never metric/field meaning, which belongs in exporter/domain
files.

After creating this file, add one row to SKILL.md §4's routing table linking
directly to it. If no exporter or domain currently routes to this backend yet
(as with opensearch-fundamentals.md at time of writing), add an explicit status
note at the top of the file and in the routing table row — do not let the
routing table imply metric coverage that doesn't exist yet.
-->

Core [backend name] query-language mechanics — the query shape, clauses/
functions, and gotchas used to build any [backend name]-backed query,
independent of which exporter or metric is involved.

<!-- If this backend currently has no exporter/domain that routes to it, keep
this status block. Otherwise delete it. -->
> **Status: infrastructure only, no implemented consumer.** No exporter or
> domain reference in this skill currently has `data_source: [backend]`. This
> file is real, verified query-language content, kept available for when a
> [backend]-backed exporter or domain is added.

## Contents

<!-- Required once this file exceeds 100 lines. -->

- Data Model
- Query Shape
- [Core clause/function categories]
- Gotchas

## Data Model

[How data is organized in this backend — e.g. series/labels/samples, or
indices/documents/mappings.]

## Query Shape

[The top-level shape of a query in this backend, with a minimal example.]

## [Core clause/function categories]

[One table or subsection per category of query building block — selectors,
functions, clauses, aggregations, etc. Use a table of {construct, behavior /
use for} rather than prose where the content is enumerable.]

## Gotchas

| Gotcha | Correct handling |
|---|---|
| [a specific, verified pitfall] | [the correct handling — not a vague warning] |

Only include gotchas that are specific and verified, in the same spirit as this
skill's other guardrails: a vague "be careful with X" adds no recognition value.
