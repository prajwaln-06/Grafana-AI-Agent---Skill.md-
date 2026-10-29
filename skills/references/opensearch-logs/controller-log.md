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
routine noise.

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
| Hosts with errors | `size: 0`, time range, term on `severity` for the error set, `terms` on `entity_host` with a small nested `terms` on `service` |
| Services producing most errors | same filter, `terms` on `service` |
| Error volume over time | same filter, `date_histogram` on `@timestamp` with an interval chosen from the window |
| Messages mentioning a phrase | `match_phrase` on `text` inside `filter` with the time range |
| Messages for a service on a host | two exact-term filters (`service`, `entity_host`) plus the time range |
| Exclude routine noise | `must_not` term on `service` for the noisy service |

The text role is search-only. Values that live only inside the message text
(a bus number, a port token) can be returned in rows but not grouped.

## Severity and "Error" Definition

"Error" means stored severity in the error-and-worse set for this family: the
stored values `error` and `critical` (and any more severe syslog value the
catalog confirms exists). `warning` is **not** an error unless the user asks for
warnings. Compare against the exact stored case.

## Known Data Caveats

- **`event_time_unreliable`:** the `Timestamp` field is a syslog string with no
  year and can be months from `@timestamp`. Never filter on it.
- **`ingest_lag_variable`:** use a window that tolerates a little lag.
- **Not supported by a plain query:** "fewer switches available than expected"
  requires comparing two numbers inside the message text, and "what was reported
  before and after an event" needs two dependent queries. Treat these as
  `unmapped`; do not approximate with a text match.

## Safe Projection

Rows return only an allow-listed projection from the registry: ingest timestamp,
host, service, severity and message. Never return a whole `_source`.

## Query Examples

No verified query example is currently available for this family; the example
is a fixture-checked shape illustration, not run against the live cluster. Field
paths, case and `.keyword` use come from the live field catalog.

```json
{
  "size": 0,
  "query": { "bool": { "filter": [
      { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
      { "term":  { "Severity.keyword": "error" } } ] } },
  "aggs": { "by_host": { "terms": { "field": "Resource.host.name.keyword", "size": 20 },
            "aggs": { "by_service": { "terms": { "field": "Resource.service.name.keyword", "size": 5 } } } } }
}
```

## Guardrails

- Never filter or bucket on `Timestamp`.
- Never group on the message text.
- Never assume a severity value or its case.
- Never treat a message-text token as a structured field.
