Core OpenSearch DSL query shapes, leaf clauses, compound queries, and
aggregations — the query language mechanics for an OpenSearch-backed query,
independent of which index or field is involved.

> **Status: infrastructure only, no implemented consumer.** No exporter or domain
> reference in this skill currently has `data_source: opensearch`. This file is
> real, verified query-language content, kept available for when an
> OpenSearch-backed exporter or domain is added — it does not mean OpenSearch
> metric coverage exists today. If a request needs an OpenSearch-backed
> measurement, route it as `unmapped` per [SKILL.md §7.5](../SKILL.md), not to a
> metric definition that doesn't exist.

## Data Model

Documents live in indices; each field has a mapping type. `keyword` fields
support exact match and aggregation; `text` fields are analyzed for full-text
search and generally cannot be aggregated directly unless a `.keyword` sub-field
exists.

## Query Shape

`{"size": N, "query": {...}, "aggs": {...}}`. Set `"size": 0` whenever only
aggregated results are needed, to avoid pulling raw documents unnecessarily.

## Leaf Clauses

| Clause | Use for |
|---|---|
| `term` / `terms` | Exact match on a keyword field |
| `match` | Full-text, analyzed match on a text field |
| `range` | Numeric or date bounds |
| `exists` | Field is present |
| `wildcard` / `prefix` | Pattern match — use cautiously, can be slow on large indices |

## Compound Queries

| Clause | Behavior |
|---|---|
| `bool.must` | Contributes to relevance score |
| `bool.filter` | No scoring, cacheable — preferred for exact constraints like `level: ERROR` |
| `bool.should` | Optional match, boosts relevance |
| `bool.must_not` | Excludes |

## Date Math

| Expression | Meaning |
|---|---|
| `now` | Current time |
| `now-1h`, `now-7d` | Relative time in the past |
| `now/d` | Rounds down to start of day |

Used inside `range` queries on date fields.

## Aggregations

| Aggregation | Behavior |
|---|---|
| `terms` | Group by a keyword field (e.g. error count by service) |
| `date_histogram` | Time-bucketed counts — the OpenSearch equivalent of a Prometheus range query |
| `avg`, `sum`, `min`, `max` | Standard metric aggregations |
| `cardinality` | Approximate distinct count |
| Nested aggregations | An aggregation inside another, e.g. `date_histogram` with a `terms` sub-aggregation for per-service counts over time |

## Gotchas

| Gotcha | Correct handling |
|---|---|
| `terms`/exact match on a `text` field | Fails or produces meaningless per-token buckets — use the `.keyword` sub-field or a field mapped as `keyword` |
| `must` vs `filter` for exact constraints | `must` affects scoring and is slower — prefer `filter` for non-scored exact matches like a level or service name |
| Date math timezone | Evaluated in the cluster's configured timezone unless explicitly specified — do not assume UTC without confirming |
| Top-level hit count vs. aggregation completeness | A high `hits.total` does not imply aggregation buckets are complete — set `size: 0` when only bucket counts matter, and read `doc_count` from the aggregation, not `hits.total` |
