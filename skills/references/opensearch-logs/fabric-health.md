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
the most unhealthy events.

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
| Fault | `CRITICAL` | Fault-specific names such as `linkerrors.linkautorestartexhaustederror` | Carries a port and a location; the switch value has no ASIC suffix. More fault kinds exist than any short list |

Heartbeats dominate document volume, so any question about problems must
exclude them by filtering on the health state or event kind. In observed data,
health state takes the values `HEALTHY` and `CRITICAL`; the full set is not
confirmed, so a question about "other states" asks the data rather than
assuming a list.

A switch and its ASIC differ only by a suffix; compare by prefix, and do not
treat the two spellings as different switches.

## Query Intent Patterns

| Intent | Approved shape |
|---|---|
| Non-healthy components | `size: 0`, time range on `@timestamp`, `exists` on `health_state`, `must_not` term `HEALTHY`, `terms` on `entity_switch` |
| Faults per switch | time range, term on `health_state`, `terms` on `entity_switch`, with a `cardinality` sub-aggregation on `entity_location` for distinct affected ports |
| Which event kinds exist | `terms` on `event_kind` (and on `health_state` for the state list) |
| Ports disabled for flapping | `match_phrase` on `text`, `terms` on `entity_location` |
| Recommendations for a port | term on `entity_port`, return `text` in rows |
| Most unhealthy racks/chassis | `terms` on `group_rack` with a nested `terms` on `group_chassis` |

Grouping uses only exact-value fields the catalog confirms are groupable; the
`text` role is never grouped. Disallowed everywhere: scripts, `query_string`,
regular expressions, leading wildcards, deep paging.

## Severity and "Unhealthy" Definition

"Unhealthy", "non-healthy" and "critical" are defined by the `health_state`
role: unhealthy means the stored state is not `HEALTHY`. This is the same
definition the source team's own queries use. An explicit "critical" request
filters on the stored value `CRITICAL`.

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
  which reading the answer used.
- **Heartbeat interval** was about one per switch per minute in one sampled day;
  this is unconfirmed for production and no "heartbeat missing" logic is
  supported yet.

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

## Guardrails

- Every question about faults excludes heartbeat documents.
- Never filter or bucket on a device-time field.
- Never group on the text role.
- Never present a document count as a count of distinct faults without saying so.
