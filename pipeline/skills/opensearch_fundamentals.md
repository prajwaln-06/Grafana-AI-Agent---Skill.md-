---
name: opensearch-fundamentals
description: Core OpenSearch DSL query shapes, leaf clauses, compound queries, and aggregations.
version: 1.0
---

# OpenSearch Fundamentals

## 1. Data Model
Documents live in indices; each field has a mapping type. `keyword` fields support exact match and aggregation; `text` fields are analyzed for full-text search and generally cannot be aggregated directly unless a `.keyword` sub-field exists.

Current index families include `consolelog-*`, `heartbeat`, and `syslog-*`. Common fields are `@timestamp`, `Attributes`, `Resource`, and `Body`; `Severity`, `SeverityText`, and `Timestamp` are document-dependent. Installed MCP exposes no index/mapping discovery operation. Inspect returned samples deterministically, preserve actual observed field names, and never claim those observations are complete mappings.

## 2. Query Shape
`{"size": N, "query": {...}, "aggs": {...}}`. Set `"size": 0` whenever only aggregated results are needed, to avoid pulling raw documents unnecessarily.

## 3. Leaf Clauses

| Clause | Use for |
|---|---|
| `term` / `terms` | Exact match on a keyword field |
| `match` | Full-text, analyzed match on a text field |
| `range` | Numeric or date bounds |
| `exists` | Field is present |
| `wildcard` / `prefix` | Pattern match — use cautiously, can be slow on large indices |

## 4. Compound Queries

| Clause | Behavior |
|---|---|
| `bool.must` | Contributes to relevance score |
| `bool.filter` | No scoring, cacheable — preferred for exact constraints like `level: ERROR` |
| `bool.should` | Optional match, boosts relevance |
| `bool.must_not` | Excludes |

## 5. Date Math

| Expression | Meaning |
|---|---|
| `now` | Current time |
| `now-1h`, `now-7d` | Relative time in the past |
| `now/d` | Rounds down to start of day |

Used inside `range` queries on date fields.

## 6. Aggregations

| Aggregation | Behavior |
|---|---|
| `terms` | Group by a keyword field (e.g. error count by service) |
| `date_histogram` | Time-bucketed counts — the OpenSearch equivalent of a Prometheus range query |
| `avg`, `sum`, `min`, `max` | Standard metric aggregations |
| `cardinality` | Approximate distinct count |
| Nested aggregations | An aggregation inside another, e.g. `date_histogram` with a `terms` sub-aggregation for per-service counts over time |

## 7. Dashboard panels

Keep OpenSearch identity separate from query semantics: `panel.index` is the index pattern, `panel.query` is the executable query, and `panel.timeField` is the observed time field. Normalize hits as `type: documents` with timestamp, index, ID, body, severity, resource, attributes, and original fields. Normalize bucket results to exact contracts (`scalar`, `categorical_buckets`, `time_series`, `histogram`, or `heatmap`) only when response contains required aggregations.

Select visualization from result shape and intent: recent event streams use Logs; last-N records use Table; a single aggregate uses Stat or Gauge; categorical buckets use Bar Chart or Pie Chart; date histograms use Time Series; numeric histogram buckets use Histogram; nested x/y buckets use Heatmap; observed latitude/longitude uses Geomap; observed source/target relationships use Node Graph. Do not treat documents as fake metric series. Use only the existing Grafana MCP OpenSearch wrapper path. Query generation must request the aggregation shape; do not synthesize buckets from documents. Installed MCP v0.17.2 cannot execute Query DSL against OpenSearch, so reject OpenSearch aggregation visualizations rather than sending Lucene and relabeling document hits.

## 8. Gotchas

| Gotcha | Correct handling |
|---|---|
| `terms`/exact match on a `text` field | Fails or produces meaningless per-token buckets — use the `.keyword` sub-field or a field mapped as `keyword` |
| `must` vs `filter` for exact constraints | `must` affects scoring and is slower — prefer `filter` for non-scored exact matches like a level or service name |
| Date math timezone | Evaluated in the cluster's configured timezone unless explicitly specified — do not assume UTC without confirming |
| Top-level hit count vs. aggregation completeness | A high `hits.total` does not imply aggregation buckets are complete — set `size: 0` when only bucket counts matter, and read `doc_count` from the aggregation, not `hits.total` |
