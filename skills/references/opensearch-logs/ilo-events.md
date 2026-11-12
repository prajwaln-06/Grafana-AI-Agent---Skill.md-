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
| Who logged into a BMC ("Who logged into leif-login03-bmc in last 24 h?") | `intent: "rows"`, `size: 20`, time range, `term` on `entity_host` (`{"term": {"host.keyword": "leif-login03-bmc"}}`) AND `match_phrase` on `text` = `"login"` (`{"match_phrase": {"Message": "login"}}`). Do NOT use `terms` on `Oem.Hpe.Code: [1017, 1018]` here, because code 1018 is logout and IPMI logins use code 1024 |
| Which BMCs were accessed from an IP ("Which BMCs were accessed from `<ip_address>`?") | `status: "ok"` (NEVER `unmapped`!), `intent: "top_n"`, `size: 0`, time range, `match_phrase` on `text` (`{"match_phrase": {"Message": "<ip_address>"}}` using the exact IP string from the user's question), and `terms` aggregation on `entity_host` (`{"by_host": {"terms": {"field": "host.keyword", "size": 20}}}`). Although the IP address lives inside `Message` text rather than a dedicated IP field, searching `Message` with `match_phrase` and grouping by `host.keyword` directly answers which BMCs were accessed from that IP |
| Root (or any named user) login/logout events ("Show all root login/logout events.") | `intent: "rows"`, `size: 20`, time range, `terms` on `event_code` = `[1017, 1018]` (verified: 1017 = Browser login, 1018 = Browser logout) AND `match_phrase` on `text` with the user name (for example `root`). Never search the literal phrase "root login/logout". The user name is stored in the message text; result rows show it masked, but the filter still works |
| Hardware events named in the message (e.g. overheating) | time range, term on `entity_host` if a BMC is named, `match_phrase` on `text` with the user's own words (other tenses and plurals are matched by the system) |
| Most frequent event codes per BMC ("Event codes recurring most per host.") | `intent: "top_n"`, `size: 0`, ONE top-level `terms` on `entity_host` (`host.keyword`) with the `terms` on `event_code` (`Oem.Hpe.Code`) NESTED inside its `aggs` (never two aggregations side by side; see the example below) |
| Which BMCs log in / out most / unusually frequent login/logout ("Which BMCs have unusually frequent login/logout?") | `status: "ok"` (NEVER `unmapped`!), `intent: "top_n"`, `size: 0`, same nested shape (`host.keyword` -> `Oem.Hpe.Code`), plus `terms` on `event_code` = `[1017, 1018]` in the filter: counts per BMC and per code, so logins (1017) and logouts (1018) are never mixed. This is a FREQUENCY ranking only (see "Frequency is not anomaly" below) |
| Security and administration events ("Security & administration events for a BMC.") | `intent: "rows"`, `size: 20`, time range, filter on `category` (`Oem.Hpe.Categories.keyword`) using `{"terms": {"Oem.Hpe.Categories.keyword": ["Security", "Administration"]}}` (add `term` on `host.keyword` if a specific BMC is named); never search the phrase "Security administration" in `text` |
| Browser versus IPMI/RMCP login counts ("Browser vs IPMI/RMCP login counts.") | `intent: "top_n"`, `size: 0`, time range, `filters` aggregation with two `match_phrase` clauses on `Message`: `"browser": {"match_phrase": {"Message": "Browser login"}}` and `"ipmi_rmcp": {"match_phrase": {"Message": "RMCP"}}` (always include `"RMCP"` in the second filter clause) |
| Severity other than OK or Informational ("Severity other than OK or Informational.") | `intent: "rows"`, `size: 20`, time range, `{"named_condition": "abnormal_severity"}` (expanded by the system to include `Caution`, `Warning`, `Critical`), rows with `_source` |
| Warning/critical events marked unrepaired + recommended actions ("Warning/critical events marked unrepaired + recommended actions.") | `intent: "rows"`, `size: 20`, time range, `term` on `repaired` set to JSON boolean `false` (`{"term": {"Oem.Hpe.Repaired": false}}`), together with explicit Warning/Critical filter `{"bool": {"should": [{"terms": {"Severity.keyword": ["Warning", "Critical"]}}, {"terms": {"Oem.Hpe.Severity.keyword": ["Warning", "Critical"]}}], "minimum_should_match": 1}}` (do NOT include `"Caution"` when the question specifically asks for **warning/critical** events). `repaired` is a boolean field: write `Oem.Hpe.Repaired`, NEVER `Oem.Hpe.Repaired.keyword` and never the string `"false"`. Always include `Oem.Hpe.RecommendedAction` in `_source` |
| How long after creation events were ingested ("How long after creation were events ingested?") | analysis `ingest_lag`: a rows query (time range, any filters), `size` up to 100; `_source` MUST include `@timestamp` and `Created`; `"analysis": {"type": "ingest_lag"}` |

## Frequency is not anomaly

"Which BMCs have unusually frequent login/logout?" can be answered only as a
**frequency ranking**: login/logout counts per BMC over the window. No baseline,
normal rate or threshold is defined for this family, so nothing can be called
*unusual* and the highest count is not an anomaly. Run the frequency query,
and say in the explanation that it ranks BMCs by volume and that no BMC is
flagged unusual because no baseline exists. The result carries
`metadata.aggregation.interpretation.anomaly_detection.performed = false`; never
contradict it. Do not invent a baseline, a threshold or a statistic. Codes
1017 and 1018 are browser sessions only: IPMI/RMCP access and failed logins are
not covered by them.

## Failed logins are not supported

The failure event has never been seen in real data: no event code, category or
message wording for a failed login is verified, so none may be invented
(do not guess "login failed", "authentication failure" or similar). A
question such as "Any failed login attempts?" therefore gets
`{"mode": "single", "status": "unmapped", "explanation": "..."}`: the status is
**`unmapped`** (never `unsupported_metric`, which is for Prometheus
measurements), and the explanation says the failed-login representation has not
yet been verified. Say that, and send no query.

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
- **Still not supported by a plain anomaly query:** detecting "unusually frequent"
  access as an anomaly (no baseline is defined; answer with the `top_n` login/logout
  frequency ranking query above and `status: "ok"`, never `unmapped`) and failed
  login detection (the failure wording has not been confirmed; treat failed logins
  as `unmapped`).

## Safe Projection

Rows return only an allow-listed projection from the registry: ingest timestamp,
creation time, host, both severities, message, event code and recommended
action. Never return a whole `_source`, and never return the Redfish noise
fields.

## Query Examples

No verified query example is currently available for this family; the examples
are fixture-checked shape illustrations, not run against the live cluster.

Warning/critical events marked unrepaired with recommended actions (`Oem.Hpe.Repaired: false` and explicit `Warning`/`Critical` severities):

```json
{
  "pattern_id": "ilo_events",
  "intent": "rows",
  "query": {
    "size": 20,
    "sort": [ { "@timestamp": "desc" } ],
    "query": { "bool": {
      "filter": [
        { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
        { "term": { "Oem.Hpe.Repaired": false } },
        { "bool": {
          "should": [
            { "terms": { "Severity.keyword": [ "Warning", "Critical" ] } },
            { "terms": { "Oem.Hpe.Severity.keyword": [ "Warning", "Critical" ] } }
          ],
          "minimum_should_match": 1
        } }
      ] } },
    "_source": [ "@timestamp", "Created", "host", "Severity", "Message", "Oem.Hpe.Severity",
                 "Oem.Hpe.Code", "Oem.Hpe.RecommendedAction" ]
  }
}
```

Who logged into `leif-login03-bmc` in the last 24 hours (`term` on `host.keyword` + `match_phrase` `"login"` on `Message`):

```jsonc
{
  "pattern_id": "ilo_events",
  "intent": "rows",
  "query": {
    "size": 20,
    "sort": [ { "@timestamp": "desc" } ],
    "query": { "bool": { "filter": [
      { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
      { "term": { "host.keyword": "leif-login03-bmc" } },
      { "match_phrase": { "Message": "login" } } ] } }
  }
}
```

Which BMCs were accessed from a source IP (`top_n` on `host.keyword` with `match_phrase` `"<ip_address>"` on `Message`):

```jsonc
{
  "pattern_id": "ilo_events",
  "intent": "top_n",
  "query": {
    "size": 0,
    "query": { "bool": { "filter": [
      { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
      { "match_phrase": { "Message": "<ip_address>" } } ] } },
    "aggs": { "by_host": { "terms": { "field": "host.keyword", "size": 20 } } }
  }
}
```

Browser vs IPMI/RMCP login counts (`filters` aggregation with `"Browser login"` and `"RMCP"`):

```jsonc
{
  "pattern_id": "ilo_events",
  "intent": "top_n",
  "query": {
    "size": 0,
    "query": { "bool": { "filter": [
      { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } } ] } },
    "aggs": {
      "login_channels": {
        "filters": {
          "filters": {
            "browser": { "match_phrase": { "Message": "Browser login" } },
            "ipmi_rmcp": { "match_phrase": { "Message": "RMCP" } }
          }
        }
      }
    }
  }
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

Event codes per BMC: the host breakdown with the event code nested inside it
(one top-level aggregation; the second `terms` goes in the first one's `aggs`):

```json
{
  "pattern_id": "ilo_events",
  "intent": "top_n",
  "query": {
    "size": 0,
    "query": { "bool": { "filter": [
      { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } } ] } },
    "aggs": { "by_host": {
      "terms": { "field": "host.keyword", "size": 20 },
      "aggs": { "by_event_code": { "terms": { "field": "Oem.Hpe.Code", "size": 10 } } } } }
  }
}
```

Login and logout frequency per BMC (browser sessions; codes 1017 and 1018 are
counted separately):

```json
{
  "pattern_id": "ilo_events",
  "intent": "top_n",
  "query": {
    "size": 0,
    "query": { "bool": { "filter": [
      { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
      { "terms": { "Oem.Hpe.Code": [1017, 1018] } } ] } },
    "aggs": { "by_host": {
      "terms": { "field": "host.keyword", "size": 20 },
      "aggs": { "by_event_code": { "terms": { "field": "Oem.Hpe.Code", "size": 2 } } } } }
  }
}
```

Root login and logout events (event code AND the user name in the message):

```json
{
  "pattern_id": "ilo_events",
  "intent": "rows",
  "query": {
    "size": 20,
    "sort": [ { "@timestamp": "desc" } ],
    "query": { "bool": { "filter": [
      { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
      { "terms": { "Oem.Hpe.Code": [1017, 1018] } },
      { "match_phrase": { "Message": "root" } } ] } }
  }
}
```

## Guardrails

- Group by TWO fields with ONE top-level aggregation and the second `terms` nested inside it.
- `Oem.Hpe.Repaired` is a boolean: no `.keyword`, a JSON `false`.
- Failed logins and "unusual" frequency: see the two sections above; answer `unmapped` / a frequency ranking, never a guess.
- Never treat the BMC hostname as an xname.
- Never infer a user name or address as a field; they exist only in message text.
- Never write a `must_not` on a severity field; use `abnormal_severity`.
- Never treat a value outside the recognised sets as abnormal or normal.
- Never filter on `Created`.
