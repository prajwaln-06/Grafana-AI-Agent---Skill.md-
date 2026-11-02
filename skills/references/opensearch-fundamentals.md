Core OpenSearch DSL query shapes, leaf clauses, compound queries, and
aggregations — the query language mechanics for an OpenSearch-backed query,
independent of which index or field is involved.

The index families that use these mechanics are defined under
[opensearch-logs/overview.md](opensearch-logs/overview.md). A request for log
data from a source that is not one of those families is `unmapped` per
[SKILL.md §7.5](../SKILL.md).

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
| `prefix` | "Starts with" match on an exact-match (keyword) field; a plain string, no `*` or `?` |

`wildcard`, `regexp`, `fuzzy`, `query_string` and `simple_query_string` are
**not allowed**: a query that uses them is rejected before it runs. Use
`prefix` for "starts with", `match` / `match_phrase` on the family's text field
for words in a message, and `term` / `terms` for exact values.

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
| `now/d` | Start of the current UTC day as a `gte` or `lt` bound; end of the current UTC day as an `lte` or `gt` bound (see "Rounding direction" below) |

Used inside `range` queries on date fields. `/d` is the only rounding unit
accepted.

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
| Date math time zone | `now` is an instant (time zones do not change it), and rounding such as `/d` happens in UTC unless the range sets a `time_zone`. Setting `time_zone` is not allowed here, so every window is UTC: `now/d` means 00:00 UTC today. Times written without an offset are read as UTC |
| Rounding direction | A rounded bound is widened, not narrowed: `gte`/`lt` round down to the start of the day, `lte`/`gt` round up to its end. So `"gte": "now-7d/d", "lte": "now/d"` covers about 8 days and is rejected against a 7-day maximum; use `"lt": "now/d"` (whole past days) or plain `now-7d` / `now` |
| Total hit count is a lower bound | By default the total is counted only up to 10,000 and reported with relation `gte` ("at least"). Read `hits.total.relation`; never present a `gte` total as an exact count. Exact counting needs `track_total_hits` |
| `terms` buckets are truncated | `terms` returns only its top `size` buckets (default 10). `sum_other_doc_count` reports how many documents fell outside them — surface it rather than implying the buckets are complete |
| `cardinality` is approximate | A distinct count is an estimate; label it approximate |
| `.keyword` is not guaranteed | A `text` field has an exact-match sub-field only if the mapping defines one, and very long strings may be left out of it. Use only a sub-field the runtime field catalog confirms |
| Dotted keys expand to objects | A field written `a.b.c` is the same as nested objects `a` → `b` → `c`; query it by the dotted path |
| Histogram size | `date_histogram` interval against window length decides the bucket count; too many buckets is rejected by the cluster. Choose the interval from the window |
| Deep paging | Do not request more than a small `size` and do not page past it; use aggregations to summarize |
| Top-level hit count vs. aggregation completeness | A high `hits.total` does not imply aggregation buckets are complete — set `size: 0` when only bucket counts matter, and read `doc_count` from the aggregation, not `hits.total` |
