Defines the iLO / BMC event log index family (`pattern_id: ilo_events`) and the
semantics needed to query it correctly.

## Quick Facts

- **Parent domain:** opensearch-logs ([overview.md](overview.md))
- **`pattern_id`:** `ilo_events` (index pattern resolved by the registry)
- **Covers:** Redfish event log entries from iLO baseboard management
  controllers: access events, configuration changes, and hardware events
- **Time field:** `@timestamp` (ingest time) — filter, sort and bucket on this only
- **Caveats that apply:** `ingest_lag_variable`, `event_time_unreliable`,
  `severity_vocabulary_per_index`, `text_not_aggregatable`

## Routing Triggers

Route here for: BMC or iLO login and logout events, browser versus IPMI access,
events by code or category, event severity other than OK, unrepaired events and
recommended actions, ingestion lag.

Do not route here for: controller syslog messages (`controller-log.md`), fabric
health (`fabric-health.md`), numeric metrics (Prometheus).

## Field Roles

| Role | Meaning | Illustrative path |
|---|---|---|
| `severity` (two fields) | Top-level severity and the vendor severity; both matter | `Severity`, `Oem.Hpe.Severity` |
| `text` | The event message (search only) | `Message` |
| `entity_host` | The BMC hostname (not an xname) | `host` |
| `event_code` | Numeric event code, a stable group key | `Oem.Hpe.Code` |
| `category` | Event categories (list) | `Oem.Hpe.Categories` |
| `repaired` | Whether the event is marked repaired | `Oem.Hpe.Repaired` |
| `recommended_action` | Vendor recommended action text | `Oem.Hpe.RecommendedAction` |

`Created` is the event's creation time at the BMC. It is display-only; filtering
uses `@timestamp`.

## Document Kinds

Event kinds are identified by their code. Observed codes include login and
logout events (browser access), IPMI access events, and a small number of
configuration events. Codes are better group keys than message text. Login user
names and source addresses appear only inside the message text, not as fields.

## Query Intent Patterns

| Intent | Approved shape |
|---|---|
| Login/logout events for a BMC | time range, term on `entity_host`, `match_phrase` on `text` or term on `event_code` |
| Most frequent event codes per BMC | `size: 0`, `terms` on `entity_host` with nested `terms` on `event_code` |
| Browser versus IPMI counts | `filters` aggregation with two `match_phrase` clauses on `text` |
| Abnormal events with actions | time range, `must_not` of a `bool` that requires both severity fields to hold their normal values (so a document is kept when either field is abnormal), rows with `recommended_action` |
| Unrepaired events | term on `repaired` set to false |

## Severity and "Abnormal" Definition

There are two severity fields. In the sampled data every document had the
normal values `OK` and `Informational`, so the abnormal value domain is
**unobserved**. "Abnormal" or "severity other than OK or Informational" means a
document where the top-level severity is not `OK`, the vendor severity is not
`Informational`, or both. Whether the two conditions are combined with "or" or
"and" is a pending decision: use "or" (either field abnormal) and say so in the
explanation. Never assume the set of abnormal values.

## Known Data Caveats

- **`ingest_lag_variable`:** events were ingested minutes after creation, with a
  long tail; recent events may be missing from a short window.
- **`event_time_unreliable`:** use `@timestamp` for windows; show `Created` as
  information only.
- **Sensitive content:** messages contain user names and internal addresses.
  Which of these may be shown to users is an open policy question; return only
  the allow-listed projection.
- **Not supported by a plain query:** computing per-document ingestion lag,
  detecting "unusually frequent" access (no baseline is defined), and failed
  login detection (the failure wording has not been confirmed). Treat these as
  `unmapped`.

## Safe Projection

Rows return only an allow-listed projection from the registry: ingest timestamp,
creation time, host, both severities, message, event code and recommended
action. Never return a whole `_source`, and never return the Redfish noise
fields.

## Query Examples

No verified query example is currently available for this family; the example is
a fixture-checked shape illustration, not run against the live cluster.

```json
{
  "size": 20,
  "sort": [ { "@timestamp": "desc" } ],
  "query": { "bool": {
    "filter":   [ { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
                  { "term": { "Oem.Hpe.Repaired": false } } ],
    "must_not": [ { "bool": { "filter": [
                      { "term": { "Severity.keyword": "OK" } },
                      { "term": { "Oem.Hpe.Severity.keyword": "Informational" } } ] } } ] } },
  "_source": [ "@timestamp", "Created", "host", "Severity", "Message", "Oem.Hpe.Severity",
               "Oem.Hpe.Code", "Oem.Hpe.RecommendedAction" ]
}
```

## Guardrails

- Never treat the BMC hostname as an xname.
- Never infer a user name or address as a field; they exist only in message text.
- Never assume the abnormal severity values.
- Never filter on `Created`.
