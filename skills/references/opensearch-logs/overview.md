Defines the OpenSearch log-search domain: the three supported index families,
what questions route to them, and the caveats shared by all three. Does not
contain per-index schema detail — see `fabric-health.md`, `controller-log.md`,
and `ilo-events.md` for that.

## Contents

- Quick Facts
- Trigger Examples
- Do Not Use
- Entity Model
- Shared Data Caveats
- Query Intents
- Result metadata and limits
- Guardrails

## Quick Facts

- **Data source:** opensearch
- **Covers:** Read-only log and event search over three index families. Each
  family is identified by a stable `pattern_id`, which the runtime index
  registry (`app/opensearch_catalog/index_registry.json`) maps to the only
  index pattern that may ever be queried.
- **Index families:**

| `pattern_id` | What it holds | Domain file |
|---|---|---|
| `fabric_health` | Slingshot fabric health documents: heartbeats and port/switch faults | [fabric-health.md](fabric-health.md) |
| `controller_log` | Controller syslog-style messages per host and service | [controller-log.md](controller-log.md) |
| `ilo_events` | iLO / BMC (Redfish) event log entries | [ilo-events.md](ilo-events.md) |

- **Index names:** this skill refers to families by `pattern_id`. A query's
  `index` value is resolved from the registry by the pipeline; never write an
  index expression of your own, and never use a wildcard, `_all`, a
  `.`-prefixed system index, or a `cluster:index` expression.
- **Schemas differ per family.** There is no universal log envelope: the
  severity, text, host and time-related fields have different names, types and
  vocabularies in each family. Each domain file describes the *role* a field
  plays; the live field catalog confirms which field paths actually exist
  (`SKILL.md` §5 Principle 9 applies to OpenSearch fields exactly as it does to
  Prometheus labels).

## Trigger Examples

Examples of user requests that route to this domain:

- "Which fabric components reported a non-healthy status in the last 24 hours?"
- "Which ports were disabled due to excessive flapping?"
- "Which switches reported link auto-restart errors?"
- "Which controllers reported errors in the last 24 hours?"
- "Which hosts reported LinkStatus Down?"
- "Show all hmpad errors for a given controller."
- "Show controller logs excluding routine CRON messages."
- "Who logged into this BMC in the last 24 hours?"
- "Show iLO events with a severity other than OK."
- "Which event codes recur most on each BMC?"
- "Which switches stopped sending heartbeats?"
- "What did hmscd log before and after the link-down event?"
- "How long after creation were the iLO events ingested?"

These illustrate intent and are not an exhaustive whitelist.

## Do Not Use

Do not use this reference for:

- Numeric measurements — CPU, memory, filesystem, GPU utilization, temperature,
  power, clocks → the Prometheus references (`node-exporter/`, `dcgm-exporter/`).
- Any action against existing data or alerts (deleting indices, silencing or
  acknowledging alerts, restarting services) → `out_of_scope_action`
  (`SKILL.md` §3). This domain is strictly read-only.
- Creating alert rules on log conditions — not supported by this domain.
- Questions that need a join between two index families or between logs and
  metrics in one query (for example "what was the controller logging when this
  node's temperature peaked"). Answer each part separately when the user names
  the entity for each; do not infer that a Prometheus label value equals a log
  host value.
- Logs from sources that are not one of the three families above → `unmapped`.

## Entity Model

- **xname**: fabric health and controller logs identify hardware by xname
  (rack/chassis/router/ASIC, optionally with a port suffix). Compare xnames
  case-insensitively and by prefix: a switch and its ASIC share a prefix.
- **BMC hostname**: iLO events identify the BMC by hostname, **not** an xname.
  Do not assume an iLO host equals a fabric or controller xname.
- **Port tokens** appear in different forms across sources (a dedicated field in
  fabric health, a token inside free text in controller logs). Do not translate
  between forms.

## Shared Data Caveats

These are fixed, named caveats. Each domain file lists which apply to it, and
an answer that relies on a flagged behavior must say so plainly.

| Caveat code | Meaning for an answer |
|---|---|
| `ingest_lag_variable` | `@timestamp` is when the event was ingested, not when it happened; lag varies by source |
| `event_time_unreliable` | Device or event time fields can be far from `@timestamp`; they are display-only and never used to filter |
| `duplicate_reemission` | The same fault can be emitted repeatedly, so a document count is not a count of distinct faults |
| `heartbeat_is_document_kind` | Heartbeats are ordinary documents inside the fabric health index, not a separate index |
| `schema_varies_by_document_kind` | Field presence differs between document kinds inside one index; "has the field" is not "has the value" |
| `severity_vocabulary_per_index` | Severity values differ in case and vocabulary per family; read exact stored values, never assume them |
| `text_not_aggregatable` | Free-text message fields are for search (`match` / `match_phrase`), never for grouping |

All filtering, sorting and time bucketing uses the ingest time field `@timestamp`.

## Query Intents

Every OpenSearch question is one of the following intents. Use the approved
query shapes in the relevant domain file and the DSL mechanics in
[opensearch-fundamentals.md](../opensearch-fundamentals.md).

| Intent | Typical question | Result shape |
|---|---|---|
| `rows` | "Show recent critical events for switch X" | hits |
| `count` | "How many errors in the last hour?" | hits total, with exact/lower-bound flag |
| `top_n` | "Which hosts report the most errors?" | buckets |
| `trend` | "Error volume over time" | time series |
| `distinct` | "How many different ports are affected?" | bucketed or approximate count |

Some questions cannot be answered by one query. For those, write the ordinary
query for the family and add an `analysis` object (and, optionally, the same
name as `intent`); the system then runs the follow-up queries itself, checks
every one of them the same way, and returns one combined result. Use an
analysis **only** where the family's domain file (and the runtime context)
lists it:

| Analysis (`analysis.type`) | Typical question | Family | Result shape |
|---|---|---|---|
| `absence` | "Which switches stopped sending heartbeats?" | `fabric_health` | buckets: the entities that went quiet |
| `recurrence` | "Which ports have recurring faults?" | `fabric_health` | buckets ranked by how many separate intervals they appear in |
| `context_window` | "What was logged before and after the link-down event?" | `controller_log` | hits around each event, marked before / anchor / after |
| `text_ratio_shortfall` | "Messages with fewer switches available than expected" | `controller_log` | hits whose `n/m` count has n below m |
| `ingest_lag` | "How long after creation were events ingested?" | `ilo_events` | hits with a lag per row, plus min / median / p90 / max |

Each domain file gives the exact query shape and an example. A parameter outside
its limit (for example a context window longer than the maximum) is rejected
and the user is asked to change it; it is never shortened. The parameters
behind these analyses (heartbeat interval, the `n/m` message wording, what one
"occurrence" interval is, which time field holds the event time) are recorded
in the registry and are **unverified** until confirmed on the real cluster; an
answer that relies on them must say so.

Still not supported: cross-family queries (one question that joins two
families, or logs with metrics), and any analysis a family does not list. Treat
those as `unmapped` rather than approximating them.

## Result metadata and limits

Every executed OpenSearch result carries a `metadata` object. Read it before
stating a count or a time range.

| Field | Meaning |
|---|---|
| `count` | `value`, `relation` (`eq` exact, `gte` at least) and `exact`. A `gte` total is a lower bound, never an exact count |
| `rows` | rows returned vs documents matched, and whether more are available (row results are capped) |
| `aggregation` | type, `approximate` and why: `cardinality` is an estimate; terms counts may be off by `doc_count_error_upper_bound`; `sum_other_doc_count` documents fall outside the returned buckets |
| `partial`, `partial_reasons`, `shards`, `shard_failures` | the search timed out or some shards failed, so results may be incomplete. If every shard fails the result is an error, not an empty answer |
| `query_window` | the window that was asked for (always within the 7-day cap) |
| `observed_range` | the earliest and latest `@timestamp` **among the returned rows (or non-empty buckets) only**. It is not the full range of data available in OpenSearch and not the query window; absent data at the edges of the window cannot be told from this |
| `severity_vocabulary`, `severity_vocabulary_provisional` | how far each severity vocabulary is trusted (`observed`, `documented`, `provisional`); a verdict that rests on a provisional vocabulary must be presented as such |
| `redaction` | counts of masked user names and IP addresses and the policy used |

An analysis result also carries `execution.analysis`: its type, the parameters
actually used, the counts it is based on (for example how many entities
reported in each window), whether its parameters are verified, how many queries
ran and why, and any warnings (for example that a list was incomplete). Rows of
an analysis may carry a per-row `analysis` note such as
`{"position": "before"}` or `{"lag_seconds": 300}`. All of the metadata above
(count exactness, partial results, redaction, severity) applies to analysis
results exactly as to ordinary ones.

Severity assessment per row has four distinct states: `abnormal`, `normal`,
`unknown` (including a missing, empty or unrecognised value) and, per field,
`unrecognised`. A row is `normal` only when every severity field positively holds
a normal value; absence of a field is never evidence of health. When two
severity fields disagree, `conflict` is set.

Redaction is a heuristic second layer, **not comprehensive PII detection**. It
masks a user name that follows a cue (`user X`, `login by X`, `logged in as X`,
`password for X`), e-mail addresses, and IPv4/IPv6 addresses (internal addresses
can be kept by configuration). It does not detect a name with no cue, a
`DOMAIN\name` form, hostnames, MAC addresses, serial numbers or other
identifiers, and it can mask an ordinary word that happens to follow a cue. The
primary control is that rows contain only the registry's allow-listed
projection. The `endpoint` shown on an execution block is the configured service
URL, as it is for Prometheus.

## Guardrails

- Route requests only to the three families defined here; anything else is
  `unmapped`.
- Use only field paths the runtime field catalog confirms for the selected
  family; never invent a field, a `.keyword` sub-field, or an index.
- Never assume a severity value; use the stored value for that family.
- Every query needs a bounded time range on `@timestamp`.
- Free-text search uses only `match` / `match_phrase` on the family's text role.
- State the relevant caveat whenever a count or ordering depends on it.
