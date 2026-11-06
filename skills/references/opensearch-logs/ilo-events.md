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
recommended actions, ingestion lag (how long after creation the BMC events were
ingested).

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
| Hardware events named in the message (e.g. overheating) | time range, term on `entity_host` if a BMC is named, `match_phrase` on `text` with the user's own words (other tenses and plurals are matched by the system) |
| Most frequent event codes per BMC | `size: 0`, `terms` on `entity_host` with nested `terms` on `event_code` |
| Browser versus IPMI counts | `filters` aggregation with two `match_phrase` clauses on `text` |
| Abnormal events with actions | time range, `{"named_condition": "abnormal_severity"}` (expanded by the system), rows with `recommended_action` |
| Unrepaired events | term on `repaired` set to false |
| How long after creation events were ingested | analysis `ingest_lag`: a rows query (time range, any filters), `size` up to 100; if `_source` is given it must include `@timestamp` and `Created`; `"analysis": {"type": "ingest_lag"}` |

## Severity and "Abnormal" Definition

There are two severity fields, and **either** one being abnormal makes an event
abnormal (an OR). Write the condition as `{"named_condition":
"abnormal_severity"}`; the system expands it deterministically to "the top-level
`Severity` is one of the recognised abnormal values **or** the vendor
`Oem.Hpe.Severity` is one of the recognised abnormal values". Never write the
expansion yourself and never use `must_not` on a severity field: that would also
match documents where a field is missing or holds an unrecognised value, which
are not known to be abnormal.

| Field | Normal | Recognised abnormal | Verification |
|---|---|---|---|
| `Severity` | `OK` (observed) | `Warning`, `Critical` | **Documented**: the DMTF Redfish `LogEntry` severity enumeration (`OK`, `Warning`, `Critical`). Only `OK` was observed in the sampled data |
| `Oem.Hpe.Severity` | `Informational` (observed) | `Caution`, `Warning`, `Critical` | **Provisional**: only `Informational` was observed. HPE's iLO log-services documentation names `Caution` and `Critical` (as severities of events that can be marked repaired); `Warning` is not documented there and is an assumption. The full vocabulary is unverified |

Values outside these sets are neither normal nor abnormal; they are reported on
the row as unrecognised rather than guessed at, and an event is `normal` only
when **both** fields hold a normal value (a missing or unrecognised field makes
it `unknown`). Results that rest on the provisional vocabulary say so. Result rows carry a severity
assessment that says which field decided the state, and flags a missing field
or a conflict between the two fields. Match severity values in their stored
case.

## Known Data Caveats

- **`ingest_lag_variable`:** events were ingested minutes after creation, with a
  long tail; recent events may be missing from a short window.
- **`event_time_unreliable`:** use `@timestamp` for windows; show `Created` as
  information only.
- **Sensitive content:** messages contain user names and internal addresses.
  Which of these may be shown to users is an open policy question; return only
  the allow-listed projection.
- **Per-document ingestion lag is not computable by a plain query** (it needs
  `@timestamp` minus `Created` per event, and scripts are not allowed). Use the
  `ingest_lag` analysis: the system computes the lag on the returned rows and
  reports the smallest, median, 90th-percentile and largest values. It is a
  sample of the returned rows (newest first), not every event, and `Created` is
  display-only and **unverified** as an event clock; say both in the answer. A
  negative lag (ingested "before" it was created) is counted and reported.
- **Still not supported by a plain query:** detecting "unusually frequent"
  access (no baseline is defined) and failed login detection (the failure
  wording has not been confirmed). Treat these as `unmapped`.

## Safe Projection

Rows return only an allow-listed projection from the registry: ingest timestamp,
creation time, host, both severities, message, event code and recommended
action. Never return a whole `_source`, and never return the Redfish noise
fields.

## Query Examples

No verified query example is currently available for this family; the examples
are fixture-checked shape illustrations, not run against the live cluster.

```json
{
  "size": 20,
  "sort": [ { "@timestamp": "desc" } ],
  "query": { "bool": {
    "filter": [ { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
                { "named_condition": "abnormal_severity" },
                { "term": { "Oem.Hpe.Repaired": false } } ] } },
  "_source": [ "@timestamp", "Created", "host", "Severity", "Message", "Oem.Hpe.Severity",
               "Oem.Hpe.Code", "Oem.Hpe.RecommendedAction" ]
}
```

How long after creation the last day's events were ingested (`ingest_lag`):

```json
{
  "pattern_id": "ilo_events",
  "intent": "ingest_lag",
  "query": {
    "size": 100,
    "query": { "bool": { "filter": [
      { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } } ] } },
    "_source": [ "@timestamp", "Created", "host", "Message" ]
  },
  "analysis": { "type": "ingest_lag" }
}
```

## Guardrails

- Never treat the BMC hostname as an xname.
- Never infer a user name or address as a field; they exist only in message text.
- Never write a `must_not` on a severity field; use `abnormal_severity`.
- Never treat a value outside the recognised sets as abnormal or normal.
- Never filter on `Created`.
