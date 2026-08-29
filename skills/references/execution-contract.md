Defines the output shape attached to a query after it has been validated and run —
this is a downstream stage, never part of query construction itself.

## Scope

An `execution` block is attached only to results whose `status` is `ok` or
`panic_mode_best_effort` (see [SKILL.md §9](../SKILL.md)). No other status ever
receives one. If validation (SKILL.md §6 Step 7) fails, this stage does not run the
query at all.

## The `execution` block

```json
{
  "execution_status": "success",
  "resolved_time_range": {"start": "2026-08-01T10:15:00Z", "end": "2026-08-01T10:30:00Z", "step_seconds": 60},
  "series": [
    {"labels": {"instance": "node-1:9100"}, "points": [{"timestamp": "2026-08-01T10:15:00Z", "value": 34.2}]}
  ],
  "series_count": 1,
  "endpoint": "http://localhost:9090",
  "fetched_at": "2026-08-01T10:30:02Z"
}
```

`execution_status` is a closed enum:

* **`success`** — Query executed, at least one series returned.
* **`empty_result`** — Query executed validly, zero series returned.
* **`endpoint_unreachable`** — Could not connect to the backend at all.
* **`endpoint_error`** — Backend reachable but rejected the query, or returned a
  non-2xx/non-JSON response.
* **`timeout`** — Request exceeded the configured timeout.
* **`not_executed`** — Backend not wired up yet.

Any status other than `success` includes an `error` string; `series` is `[]` and
`series_count` is `0`.

## The `series` shape (backend-agnostic)

A list of `{labels, points}` objects, identical in shape regardless of backend.
`labels` are the returned dimension key/value pairs. `points` is
`{timestamp (ISO-8601 UTC), value (number)}`.

## Relationship to the construction-time `time_range`

`time_range.from` / `.to` remain relative (`"now-1h"`, `"now"`) as SKILL.md §6
Step 5 produces them. `resolved_time_range` in the execution block is the only
place absolute timestamps appear, resolved at the moment this stage runs.

For a `query_type: "instant"` result (SKILL.md §8 "Instant vs. range"), the
construction-time `time_range` is `{"time": "<relative expression>"}` instead
of `{"from", "to", "step"}`, and `resolved_time_range` is correspondingly
`{"instant": "<resolved ISO-8601 UTC timestamp>"}` instead of the
`{"start", "end", "step_seconds"}` shape shown above. `series[].points` for an
instant result normally holds exactly one point per series (the value at that
instant), not a series of points across a window.

## Time expression grammar

See [prometheus-fundamentals.md](prometheus-fundamentals.md) for the Prometheus
time-expression grammar and resolution rules this stage must honor. OpenSearch
date-math resolution follows [opensearch-fundamentals.md](opensearch-fundamentals.md)
once an OpenSearch-backed reference exists (see that file's status note).
