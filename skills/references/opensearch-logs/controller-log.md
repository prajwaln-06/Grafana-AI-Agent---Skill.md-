Defines the controller log index family (`pattern_id: controller_log`) and the
semantics needed to query it correctly.

## Quick Facts

- **Parent domain:** opensearch-logs ([overview.md](overview.md))
- **`pattern_id`:** `controller_log` (index pattern resolved by the registry)
- **Covers:** Syslog-style messages from fabric controllers, with the emitting
  host and service
- **Time field:** `@timestamp` (ingest time) — filter, sort and bucket on this only
- **Caveats that apply:** `event_time_unreliable`, `ingest_lag_variable`,
  `schema_varies_by_document_kind`, `severity_vocabulary_per_index`,
  `text_not_aggregatable`

## Routing Triggers

Route here for: controller errors or warnings, messages from a named service
(for example `hmscd`, `hmpad`, `fabric-manager`), "LinkStatus Down", I²C
failures, which hosts or services log the most errors, controller logs excluding
routine noise, what a controller or service logged before and after a given
event, fabric-manager messages with fewer switches available than expected.

Do not route here for: fabric health state or fault documents
(`fabric-health.md`), BMC login or iLO events (`ilo-events.md`), numeric
metrics (Prometheus).

## Field Roles

| Role | Meaning | Illustrative path |
|---|---|---|
| `severity` | Syslog-style severity, lower-case | `Severity` |
| `text` | The log message (search only) | `Body` |
| `entity_host` | The controller's xname | `Resource.host.name` |
| `service` | The emitting service or program | `Resource.service.name` |
| `entity_port` | Structured port name, present on a small subset of documents | `Attributes.PortName` |

Only part of the `Attributes` object is stable; its other keys vary by document
and are confirmed by the live field catalog, never assumed. `Attributes` also
contains a second severity-like key on a subset of documents; it is distinct
from the top-level `Severity` and is not the severity role.

## Document Kinds

- **Ordinary syslog documents**: severity, message, host, service. By far the
  majority.
- **Port-event documents**: a small subset that also carry structured port
  attributes. Port information for most documents exists only as a token inside
  the message text.

Observed severity values are lower-case: `info`, `notice`, `warning`, `error`,
`critical`. The full syslog set may be larger, so read stored values rather than
assuming them. Message content varies by service; for example, the fabric
manager reports switch availability, `hmscd` reports link status changes, and
`hmpad` reports bus-level failures.

## Query Intent Patterns

| Intent | Approved shape |
|---|---|
| Hosts with errors | `size: 0`, time range, `{"named_condition": "abnormal_severity"}` for the error set (expanded by the system), `terms` on `entity_host` with a small nested `terms` on `service` |
| Services producing most errors | same filter, `terms` on `service` |
| Error volume over time | same filter, `date_histogram` on `@timestamp` with an interval chosen from the window |
| Messages mentioning a phrase | `match_phrase` on `text` inside `filter` with the time range |
| Messages for a service on a host | two exact-term filters (`service`, `entity_host`) plus the time range |
| Exclude routine noise | `must_not` term on `service` for the noisy service |
| What was logged before and after an event | analysis `context_window`: a rows query that finds the event (`match_phrase` on `text`, time range), `size` 1 to 3; `"analysis": {"type": "context_window"}` with optional `before` / `after` (default 5 minutes each, at most 60) and an optional `context_filter` (one clause, no time range) that selects the surrounding messages, for example a term on `service` |
| Fewer switches available than expected | analysis `text_ratio_shortfall`: a rows query with `match_phrase` on `text` for the availability message, `size` up to 100; `"analysis": {"type": "text_ratio_shortfall"}` |

The text role is search-only. Values that live only inside the message text
(a bus number, a port token) can be returned in rows but not grouped.

## Severity and "Error" Definition

"Error" means stored severity in the error-and-worse set for this family: the
observed values `error` and `critical`, plus the syslog names `err`, `crit`,
`alert` and `emerg` (documented in RFC 5424, not observed in sampled data). Select
it with the named condition `{"named_condition": "abnormal_severity"}`; the
system expands it to exactly that set from the registry. Do not write the list
by hand: a hand-written `term` on `error` alone misses `critical` rows.
`warning` is **not** an error unless the user asks for warnings; then add an
explicit `terms` on the stored values `warning` and `warn` alongside the named
condition (in a `bool.should` with `minimum_should_match` 1). Compare against
the exact stored case.

## Known Data Caveats

- **`event_time_unreliable`:** the `Timestamp` field is a syslog string with no
  year and can be months from `@timestamp`. Never filter on it.
- **`ingest_lag_variable`:** use a window that tolerates a little lag.
- **Not answerable by a plain query, answered by an analysis instead:**
  "fewer switches available than expected" requires comparing two numbers
  inside the message text, and "what was reported before and after an event"
  needs two dependent queries. Do not approximate either with a text match.
  - Use `text_ratio_shortfall`: the system reads the `n/m` count out of each
    returned message (pattern `n / m switches available`, a **placeholder**: the
    real wording is unconfirmed) and keeps the rows where n is below m. Only the
    returned rows are scanned; when more messages matched than were returned,
    the answer says the scan was limited and the count is a lower bound.
  - Use `context_window`: the system finds the event(s) (at most 3), then
    fetches the messages from `before` to `after` around each one, oldest
    first, marking each row `before`, `anchor` or `after`. Without a
    `context_filter` the surrounding messages come from the same host as the
    event. A span longer than 60 minutes is rejected, not shortened.

## Safe Projection

Rows return only an allow-listed projection from the registry: ingest timestamp,
host, service, severity and message. Never return a whole `_source`.

## Query Examples

No verified query example is currently available for this family; the examples
are fixture-checked shape illustrations, not run against the live cluster. Field
paths, case and `.keyword` use come from the live field catalog.

```json
{
  "size": 0,
  "query": { "bool": { "filter": [
      { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
      { "named_condition": "abnormal_severity" } ] } },
  "aggs": { "by_host": { "terms": { "field": "Resource.host.name.keyword", "size": 20 },
            "aggs": { "by_service": { "terms": { "field": "Resource.service.name.keyword", "size": 5 } } } } }
}
```

What `hmscd` logged in the five minutes before and after the most recent
link-down message (`context_window`):

```json
{
  "pattern_id": "controller_log",
  "intent": "context_window",
  "query": {
    "size": 1,
    "query": { "bool": { "filter": [
      { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
      { "match_phrase": { "Body": "LinkStatus Down" } } ] } }
  },
  "analysis": { "type": "context_window", "before": "5m", "after": "5m",
                "context_filter": { "term": { "Resource.service.name.keyword": "hmscd" } } }
}
```

Fabric-manager messages with fewer switches available than expected
(`text_ratio_shortfall`):

```json
{
  "pattern_id": "controller_log",
  "intent": "text_ratio_shortfall",
  "query": {
    "size": 100,
    "query": { "bool": { "filter": [
      { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
      { "term": { "Resource.service.name.keyword": "fabric-manager" } },
      { "match_phrase": { "Body": "switches available" } } ] } }
  },
  "analysis": { "type": "text_ratio_shortfall" }
}
```

## Guardrails

- Never filter or bucket on `Timestamp`.
- Never group on the message text.
- Never assume a severity value or its case.
- Never treat a message-text token as a structured field.
