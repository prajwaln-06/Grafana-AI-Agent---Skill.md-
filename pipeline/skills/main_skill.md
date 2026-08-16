---
name: main-routing-and-procedure
description: Deterministic intent routing and dashboard proposal/mutation procedure.
version: 6.0
---

# Observability Dashboard Procedure

## Intent resolution

Resolve every request to exactly one normalized intent before selecting tools: `READ`, `CREATE`, `UPDATE`, `REMOVE`, or `UNSPECIFIED`. Classify panels by capability without an extra model call: infrastructure measurements use Prometheus; logs, documents, and document aggregations use OpenSearch. Ask a focused clarification when datasource or measurement intent is ambiguous.

## READ

`READ` means metric values, comparisons, available metrics, dashboard discovery, or panel inspection. Resolve datasource, metric, labels, and target through the wrapper; construct and execute the verified query; return normalized results. READ never creates, updates, or removes Grafana state.

## CREATE

Resolve each panel through its datasource branch, then assemble one ordered Dashboard IR. Run independent Prometheus and OpenSearch resolution concurrently and do not query irrelevant datasources. Present one proposal for review. User edits create a new version. Explicit approval issues a version-bound token. The backend then compiles the approved IR, validates it, and invokes Grafana MCP create through the wrapper.

## UPDATE

Identify exactly one existing dashboard through wrapper/MCP discovery. Retrieve its current state and hydrate it into Dashboard IR. Apply only the requested panel, visualization, target, time-range, or variable change; retain unrelated panels. Present the updated proposal. User edits create a new version and invalidate approval. Explicit approval is required before deterministic compilation, validation, and Grafana MCP update.

## REMOVE

Identify the exact dashboard and requested panel, or identify the exact dashboard for deletion. Present a literal removal proposal. User edits create a new version. Explicit approval is required before validation and Grafana MCP panel/dashboard removal. Natural-language deletion alone never mutates Grafana.

## UNSPECIFIED

Ask a focused clarification when the user has not made READ versus dashboard mutation explicit. Do not select a mutation tool and do not mutate Grafana.

## Approval and versioning

The application owns approval state. Before any CREATE, UPDATE, or REMOVE mutation, verify proposal existence, exact version, approval token, content digest, IR validation, compiled Grafana validation, and write policy. A modified proposal is a new version with no approval token. The LLM can resolve intent and request proposals; it cannot execute mutation.

## Tool routing

- `READ`: retrieval/query/dashboard discovery wrapper tools.
- `CREATE`, `UPDATE`, `REMOVE`: `resolve_dashboard_intent` then `propose_dashboard`; show the IR proposal.
- Approved execution: application/UI calls the backend execution endpoint; the model has no direct mutation tool.

## Data integrity

Discover every Prometheus metric, label, target, and datasource through the wrapper. The installed MCP exposes no OpenSearch index-listing or mapping-inspection tool, so OpenSearch field discovery is a deterministic sampled-document fallback and must be labeled observed, not mapped. OpenSearch uses Grafana datasource -> Grafana MCP `query_elasticsearch` -> the existing wrapper path; never add a direct HTTP path. If discovery is empty, report unavailable; if ambiguous, ask the user; if query semantics are unverified, return unsupported. Preserve Dashboard IR as the authoritative compiler input.

`panel.metric` is the canonical discovered metric identity. `panel.query` is the executable PromQL expression. Never place PromQL in `panel.metric` or infer metric identity in the UI.

One metric does not imply one panel. Preserve every explicitly requested panel in request order, including multiple panels that share a metric but differ in visualization type, title, visualization configuration, layout, or size. For UPDATE, use the requested operation and hydrated IR to distinguish add, modify, remove, and preserve; do not collapse panels by measurement name.

For OpenSearch, preserve `panel.index`, `panel.query`, `panel.timeField`, datasource metadata, observed fields, and explicit normalized result types: documents, scalar, time_series, categorical_buckets, histogram, heatmap, geographic, or graph. Aggregation results require actual Query DSL aggregations and matching response aggregations; never infer them from hits. Installed MCP supports OpenSearch Lucene document queries but not OpenSearch Query DSL, so reject OpenSearch aggregation panels until that capability exists. Prometheus shares the contracts, but Logs is unavailable; Geomap/Node Graph require discovered coordinates/relationships, histogram/heatmap require real bucket metrics, counters use rate/increase semantics, and histogram heatmaps de-cumulate `le` buckets. Never fabricate series, coordinates, or edges. OpenSearch access remains Grafana datasource -> Grafana MCP `query_elasticsearch` -> wrapper only. Both datasource types share proposal, editing, save, approval, and explicit execution.
