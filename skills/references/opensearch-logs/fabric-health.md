Defines the Slingshot fabric health index family (`pattern_id: fabric_health`)
and the semantics needed to query it correctly.

## Quick Facts

- **Parent domain:** opensearch-logs ([overview.md](overview.md))
- **`pattern_id`:** `fabric_health` (index pattern resolved by the registry)
- **Covers:** Fabric health documents emitted per switch: periodic heartbeats
  and port/switch fault reports
- **Time field:** `@timestamp` (ingest time) — filter, sort and bucket on this only
- **Caveats that apply:** `duplicate_reemission`, `event_time_unreliable`,
  `heartbeat_is_document_kind`, `schema_varies_by_document_kind`,
  `severity_vocabulary_per_index`, `text_not_aggregatable`

## Routing Triggers

Route here for: fabric or switch health, switch or port faults, link errors,
flapping ports, ports disabled, heartbeat documents, which racks or chassis have
the most unhealthy events, switches that stopped sending heartbeats (gone
quiet), ports or locations with recurring faults.

Do not route here for: controller messages by service (`controller-log.md`),
BMC login or iLO events (`ilo-events.md`), CPU or GPU measurements (Prometheus).
"Link-down" messages written by a controller service belong to
`controller-log.md`; fault documents about port link errors belong here.

## Field Roles

The registry binds each role to a field path; the live field catalog confirms
which of them exist. Paths below are illustrative of what the registry binds.

| Role | Meaning | Illustrative path |
|---|---|---|
| `health_state` | The health/severity state of the document | `fields.PhysicalSubContext` |
| `event_kind` | What kind of event the document is | `fields.PhysicalContext` |
| `text` | Free-text recommendation and cause (search only) | `fields.Value` |
| `entity_switch` | The switch | `fields.Switch` |
| `entity_port` | The port, on fault documents | `fields.Port` |
| `entity_location` | Switch plus port, on fault documents | `fields.Location` |
| `group_rack`, `group_chassis` | Hardware grouping | `fields.Rack`, `fields.Chassis` |

## Document Kinds

Two kinds of document share the index. Always decide which kind a question is
about.

| Kind | Health state | Event kind | Notes |
|---|---|---|---|
| Heartbeat | `HEALTHY` | `runtime.heartbeat` | Emitted periodically per switch; carries an ASIC identifier and no port. The switch value includes an ASIC suffix |
| Fault | `CRITICAL` or `DEGRADED` | Fault-specific names such as `linkerrors.linkautorestartexhaustederror` | Carries a port and a location. The switch value may or may not carry an ASIC suffix: non-healthy documents in the 2026-11-10 test did (`x1000c4r5b0`). More fault kinds exist than any short list |

Heartbeats dominate document volume, so any question about problems must
exclude them by filtering on the health state or event kind. In observed data,
health state takes the values `HEALTHY`, `CRITICAL` and `DEGRADED` (see the severity section); the full set is not
confirmed, so a question about "other states" asks the data rather than
assuming a list.

A switch and its ASIC differ only by a suffix; compare by prefix, and do not
treat the two spellings as different switches.

## Query Intent Patterns

| Intent | Approved shape |
|---|---|
| Non-healthy components | `intent: "top_n"`, `size: 0`, time range on `@timestamp`, `exists` on `health_state`, `must_not` term `HEALTHY`, `terms` on `entity_switch` |
| Faults / CRITICAL events per switch ("How many CRITICAL events occurred per switch?") | `intent: "top_n"`, `size: 0`, time range, `term` on `health_state` = `"CRITICAL"`, `terms` on `entity_switch` (`size: 20`), with BOTH a `cardinality` sub-aggregation (`distinct_ports`) AND a `terms` sub-aggregation (`by_location`, `size: 10`) on `entity_location` (`fields.Location.keyword`) inside `by_switch.aggs` so distinct locations and per-location counts are always shown alongside switch counts |
| Which health states or event kinds exist ("What health states exist besides HEALTHY?") | `intent: "top_n"`, `size: 0`, `terms` on `health_state` (`fields.PhysicalSubContext.keyword`) with only the time range filter (or `terms` on `event_kind` for event kinds) |
| Ports disabled due to excessive flapping ("Which ports were disabled due to excessive flapping?") | `intent: "top_n"`, `size: 0` (NEVER `intent: "rows"`), time range, `match_phrase` on `text` (`{"match_phrase": {"fields.Value": "excessive flapping"}}`), and `terms` aggregation on `entity_location` (`fields.Location.keyword`) |
| Recommendations for a port ("What recommendations were reported for port j104p1?") | `intent: "rows"`, `size: 20`, time range, `term` on `entity_port` (`{"term": {"fields.Port.keyword": "j104p1"}}` — a bare port name such as `j104p1` belongs in `entity_port`, NOT in `entity_location`, which holds the composite switch-plus-port value and would not match a bare port), return `text` (`fields.Value`) in `_source` |
| Which switches reported a specific fault/event kind ("Which switches reported linkerrors.linkautorestartexhaustederror?") | `intent: "top_n"`, `size: 0` (NEVER `intent: "rows"`), time range, `term` on `event_kind` (`{"term": {"fields.PhysicalContext.keyword": "linkerrors.linkautorestartexhaustederror"}}`), and `terms` aggregation on `entity_switch` (`fields.Switch.keyword`) |
| Critical events for ONE switch ("Show critical events for switch x1000c3r1.") | `intent: "rows"`, `size: 20`, time range, `term` on `health_state` = `"CRITICAL"` (`{"term": {"fields.PhysicalSubContext.keyword": "CRITICAL"}}`), and `term` on `entity_switch` = `"x1000c3r1"` (`{"term": {"fields.Switch.keyword": "x1000c3r1"}}`). Note: fault/CRITICAL documents store the bare switch xname (`x1000c3r1`), whereas heartbeat documents carry an ASIC suffix (`x1000c3r1b0`), where `prefix` is the only partial match allowed (never `wildcard`) |
| Most unhealthy racks/chassis | `intent: "top_n"`, `size: 0`, time range, `{"named_condition": "abnormal_severity"}`, `terms` on `group_rack` (`fields.Rack.keyword`) with a nested `terms` on `group_chassis` (`fields.Chassis.keyword`) |
| Switches that stopped sending heartbeats | analysis `absence`: `size: 0`, time range = the baseline (for example the last 24 hours), term on `event_kind` = `runtime.heartbeat`, ONE `terms` on `entity_switch` with no sub-aggregation; `"analysis": {"type": "absence"}` (optional `recent_window`) |
| Ports with recurring faults | analysis `recurrence`: `size: 0`, time range, `{"named_condition": "abnormal_severity"}` (every non-HEALTHY state; do not hard-code `CRITICAL`), `terms` on `entity_location` with ONE `date_histogram` sub-aggregation on `@timestamp` using the registry interval (`1h`); `"analysis": {"type": "recurrence"}` |

Grouping uses only exact-value fields the catalog confirms are groupable; the
`text` role is never grouped. Disallowed everywhere: scripts, `query_string`,
regular expressions, wildcard queries, deep paging.

## Severity and "Unhealthy" Definition

"Unhealthy", "non-healthy" and "critical" are defined by the `health_state`
role: unhealthy means the stored state is not `HEALTHY`. This is the same
definition the source team's own queries use. An explicit "critical" request
filters on the stored value `CRITICAL`. **Observed states differ:** the 2026-11-10 manual test found
`HEALTHY` and `DEGRADED` (and no `CRITICAL` document in 7 days), while an earlier
sample showed `HEALTHY` and `CRITICAL`. A `CRITICAL` filter can therefore be
correctly empty while `DEGRADED` documents exist. When a user says "critical" or
"problem" without naming a state, use `abnormal_severity` (all non-`HEALTHY`
states) and say which states came back; use the literal `CRITICAL` only when the
user insists on that state, and then mention that other unhealthy states exist.

## Known Data Caveats

- **`duplicate_reemission`:** the same fault is re-emitted, sometimes several
  times at the same instant and again on a recurring schedule. A document count
  overstates the number of faults. For "how many ports/faults", report distinct
  locations (an approximate distinct count) alongside the document count, and
  label it approximate.
- **`event_time_unreliable`:** device-time fields can be years behind
  `@timestamp`. They are shown, never filtered on.
- **"Recurring" is undefined.** Hourly re-emission means "recurring faults" is
  ambiguous between a persistent state and repeated new occurrences. State
  which reading the answer used. The `recurrence` analysis reads it as "seen in
  several separate one-hour intervals" (the registry interval, unverified) and
  ranks by that count (`metrics.active_intervals`), not by document count; a
  fault that is simply re-emitted every hour also scores high, so say that the
  ranking cannot tell a persistent fault from repeated new ones.
- **Heartbeat interval** was about one per switch per minute in one sampled day;
  this is unconfirmed for production. The `absence` analysis uses the registry's
  heartbeat settings (60 seconds, missing after 3 missed heartbeats, so a
  default recent window of 3 minutes), which are **placeholders**: present a
  "stopped sending heartbeats" answer as provisional. A switch is compared
  without its ASIC suffix, so `x1000c0r1b0` and `x1000c0r1b1` count as the same
  switch. If nothing at all reported recently, the answer warns that this may
  be an ingestion outage rather than every switch being down.

## Safe Projection

Rows return only an allow-listed projection from the registry: the ingest
timestamp, switch, port, location, event kind, health state and the text field.
Never return a whole `_source`, and never return pipeline noise fields
(parse-failure tags, version, ids).

## Query Examples

No verified query example is currently available for this family: the examples
below are shape illustrations built from documented semantics and checked
against fixtures only, not run against the live cluster. Do not present them as
verified. Field paths and `.keyword` use depend on the live field catalog.

```json
{
  "size": 0,
  "track_total_hits": true,
  "query": { "bool": {
    "filter":   [ { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
                  { "exists": { "field": "fields.PhysicalSubContext" } } ],
    "must_not": [ { "term": { "fields.PhysicalSubContext.keyword": "HEALTHY" } } ] } },
  "aggs": { "by_switch": { "terms": { "field": "fields.Switch.keyword", "size": 50 } } }
}
```

Switches that stopped sending heartbeats (`absence`): the query is the baseline
(who sent heartbeats in the last 24 hours); the system runs the "recent" query
itself and returns the switches missing from it.

```json
{
  "pattern_id": "fabric_health",
  "intent": "absence",
  "query": {
    "size": 0,
    "query": { "bool": { "filter": [
      { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
      { "term":  { "fields.PhysicalContext.keyword": "runtime.heartbeat" } } ] } },
    "aggs": { "by_switch": { "terms": { "field": "fields.Switch.keyword", "size": 500 } } }
  },
  "analysis": { "type": "absence", "recent_window": "3m" }
}
```

Ports with recurring faults (`recurrence`), over the last 7 days:

```json
{
  "pattern_id": "fabric_health",
  "intent": "recurrence",
  "query": {
    "size": 0,
    "query": { "bool": { "filter": [
      { "range": { "@timestamp": { "gte": "now-7d", "lte": "now" } } },
      { "named_condition": "abnormal_severity" } ] } },
    "aggs": { "by_location": { "terms": { "field": "fields.Location.keyword", "size": 20 },
              "aggs": { "per_hour": { "date_histogram": { "field": "@timestamp", "fixed_interval": "1h" } } } } }
  },
  "analysis": { "type": "recurrence" }
}
```

CRITICAL events per switch, showing both distinct locations and location breakdown inside `by_switch`:

```json
{
  "pattern_id": "fabric_health",
  "intent": "top_n",
  "query": {
    "size": 0,
    "query": { "bool": { "filter": [
      { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
      { "term": { "fields.PhysicalSubContext.keyword": "CRITICAL" } } ] } },
    "aggs": { "by_switch": {
      "terms": { "field": "fields.Switch.keyword", "size": 20 },
      "aggs": {
        "distinct_ports": { "cardinality": { "field": "fields.Location.keyword" } },
        "by_location": { "terms": { "field": "fields.Location.keyword", "size": 10 } }
      }
    } }
  }
}
```

Ports disabled due to excessive flapping (`top_n` on `fields.Location.keyword` with `match_phrase` `"excessive flapping"`):

```jsonc
{
  "pattern_id": "fabric_health",
  "intent": "top_n",
  "query": {
    "size": 0,
    "query": { "bool": { "filter": [
      { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
      { "match_phrase": { "fields.Value": "excessive flapping" } } ] } },
    "aggs": { "by_location": { "terms": { "field": "fields.Location.keyword", "size": 50 } } }
  }
}
```

Recommendations reported for port `j104p1` (`rows` with `term` on `fields.Port.keyword`):

```jsonc
{
  "pattern_id": "fabric_health",
  "intent": "rows",
  "query": {
    "size": 20,
    "sort": [ { "@timestamp": "desc" } ],
    "query": { "bool": { "filter": [
      { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
      { "term": { "fields.Port.keyword": "j104p1" } } ] } },
    "_source": [ "@timestamp", "fields.Switch", "fields.Port", "fields.Location", "fields.PhysicalContext", "fields.PhysicalSubContext", "fields.Value" ]
  }
}
```

Switches that reported `linkerrors.linkautorestartexhaustederror` (`top_n` on `fields.Switch.keyword`):

```jsonc
{
  "pattern_id": "fabric_health",
  "intent": "top_n",
  "query": {
    "size": 0,
    "query": { "bool": { "filter": [
      { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
      { "term": { "fields.PhysicalContext.keyword": "linkerrors.linkautorestartexhaustederror" } } ] } },
    "aggs": { "by_switch": { "terms": { "field": "fields.Switch.keyword", "size": 50 } } }
  }
}
```

Critical events for switch `x1000c3r1` (`rows` with exact `term` on `fields.PhysicalSubContext.keyword: "CRITICAL"` and `fields.Switch.keyword: "x1000c3r1"`):

```jsonc
{
  "pattern_id": "fabric_health",
  "intent": "rows",
  "query": {
    "size": 20,
    "sort": [ { "@timestamp": "desc" } ],
    "query": { "bool": { "filter": [
      { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
      { "term": { "fields.PhysicalSubContext.keyword": "CRITICAL" } },
      { "term": { "fields.Switch.keyword": "x1000c3r1" } } ] } }
  }
}
```

## Guardrails

- A bare port name goes in `entity_port`; the composite `entity_location` is never matched with a bare port.
- For general unhealthy events across ASIC-suffixed switch spellings, `prefix` is used (never `wildcard`); for explicit critical fault documents on a bare switch xname such as `x1000c3r1`, use `term` on `fields.Switch.keyword`.
- Every question about faults excludes heartbeat documents.
- Never filter or bucket on a device-time field.
- Never group on the text role.
- Never present a document count as a count of distinct faults without saying so.
