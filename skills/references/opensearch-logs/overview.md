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

Not supported in this domain yet: absence or "no recent heartbeat" detection,
multi-step "before and after an event" lookups, and cross-family queries. Treat
those as `unmapped` rather than approximating them.

## Guardrails

- Route requests only to the three families defined here; anything else is
  `unmapped`.
- Use only field paths the runtime field catalog confirms for the selected
  family; never invent a field, a `.keyword` sub-field, or an index.
- Never assume a severity value; use the stored value for that family.
- Every query needs a bounded time range on `@timestamp`.
- Free-text search uses only `match` / `match_phrase` on the family's text role.
- State the relevant caveat whenever a count or ordering depends on it.
