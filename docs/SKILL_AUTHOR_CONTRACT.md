# Skill author contract — OpenSearch (merged code)

This file says exactly what the OpenSearch skill files must contain so that the
code and the skill content fit together. It is for whoever edits
`skills/SKILL.md` or `skills/references/opensearch-logs/*.md`. It describes
the **merged** system: the friend's contract and guard (unchanged), plus the
optional `intent` / `analysis` keys added for the five multi-step analyses.

Two tests and one tool keep this file honest:

* `tests/test_skill_examples.py` runs every JSON example in the domain files
  and the concrete OpenSearch example in `SKILL.md` through the guard;
* `tests/test_opensearch_routing.py` checks the routing rows, the `pattern_id`
  statements, and that each file documents exactly the analyses the registry
  configures for its family;
* `python scripts/smoke_test_opensearch.py --check-skills` checks the same
  things on any machine, without a cluster.

## 1. The one rule: who owns what

| Owner | Owns | Never contains |
|---|---|---|
| `app/opensearch_catalog/index_registry.json` | index patterns, the time field, which fields may be queried, grouped on or returned, field **roles**, severity vocabularies, analysis parameters (heartbeat interval, the `n/m` pattern, intervals) | prose |
| The live field catalog (read from `_mapping`) | which fields really exist today and what each can do (for example whether a confirmed `.keyword` sub-field exists) | anything written by a person |
| Skill files (`SKILL.md`, domain files) | what each family **means**, what to route where, which query shape answers which question, the caveats an answer must state | index names, invented fields, values the registry does not know |
| Python | algorithms only (guard rules, the five analyses, normalisation) | index or field names |

If a skill file and the registry disagree, the registry wins at run time and
the tests fail until the skill file is fixed.

## 2. Files the code expects

| File | Must exist | Paired registry entry |
|---|---|---|
| `skills/references/opensearch-logs/overview.md` | yes (shared caveats, intents, metadata) | none (no `pattern_id`) |
| `skills/references/opensearch-logs/fabric-health.md` | yes | `pattern_id: fabric_health` |
| `skills/references/opensearch-logs/controller-log.md` | yes | `pattern_id: controller_log` |
| `skills/references/opensearch-logs/ilo-events.md` | yes | `pattern_id: ilo_events` |

Each domain file must contain the literal text `` `pattern_id: <id>` ``. The
registry refuses to load if a `domain_reference` file is missing, which
switches OpenSearch off as a whole (fail closed) rather than half-working.

## 3. Routing rows in `SKILL.md` §4

One row per file, data source exactly `opensearch`. Keep the trigger phrases
**specific and separate**: a phrase may appear in one family row only. Today:

| Family | Signature phrases (only in its own row) |
|---|---|
| fabric health | heartbeat, switch or port faults, flapping, *switches that stopped sending heartbeats*, *recurring port faults* |
| controller log | hmscd, hmpad, fabric-manager, LinkStatus Down, CRON, *before and after an event*, *fewer switches available than expected* |
| iLO events | BMC, IPMI, login/logout, event codes, *how long after creation … ingested* |

The phrases in italics were added for the analyses. A test fails if one leaks
into a sibling row.

## 4. What each domain file must say

Keep the friend's section layout: Quick Facts, Routing Triggers, Field Roles,
Document Kinds, Query Intent Patterns, Severity definition, Known Data Caveats,
Safe Projection, Query Examples, Guardrails. In particular:

* what "error", "unhealthy" or "abnormal" means **in that family's own
  values**, and that it is selected with `{"named_condition": "abnormal_severity"}`;
* the caveats an answer must state (re-emitted fabric faults, iLO ingest
  delay, lower-bound counts, provisional severity vocabularies);
* what the family cannot answer (left `unmapped`);
* for every analysis the registry configures for the family: when to use it,
  the exact query shape, its parameters and limits, and that its parameters are
  unverified until the smoke test confirms them;
* at least one JSON example per configured analysis, as a contract fragment
  (`pattern_id`, optional `intent`, `query`, `analysis`).

Describe fields by **role** where possible; the example field paths must exist
in the registry, and group-by paths must be the confirmed exact-match
(`.keyword`) paths.

## 5. The output contract (`SKILL.md` §9)

An OpenSearch result is an ordinary `ok` / `panic_mode_best_effort` result
with these differences. **Nothing here changed in the merge except the two
optional keys.**

| Key | Rule |
|---|---|
| `data_source` | `"opensearch"` |
| `pattern_id` | **one** family per result, chosen from the runtime context; the system sets `index` from it. Never write `index`. Several families means `mode: "multi"`. |
| `measurement_used` | `{"type": "raw_metric", "name": "<what is searched>", "source_metrics": []}` |
| `query` | a DSL object: `{"bool": {"filter": [<one range on @timestamp with both bounds>, ...]}}` plus `size`, `aggs`, `sort`, `_source`, `track_total_hits` as needed. No `time_range`, no `query_type`. |
| `intent` (optional, new) | `rows`, `count`, `top_n`, `trend`, `distinct` (descriptive only), or the name of an analysis |
| `analysis` (optional, new) | `{"type": "<analysis>", ...parameters}` for the five questions one query cannot answer; must match `intent` when both are present |

**Reject, never repair.** The system fills only documented defaults, and only
when a key is absent (they are listed in `guard_notes.applied`). A value the
model wrote is never changed: a window over 7 days, a terms size over the
limit, a severity value in the wrong case, a `must_not` on a severity field, an
analysis span over 60 minutes are all **rejected** with an explanation.

### The five analyses

| `analysis.type` | Family | Query the model writes | Parameters (defaults are filled when absent) | Limits |
|---|---|---|---|---|
| `absence` | fabric_health | `size: 0`, the baseline window, a heartbeat filter, ONE `terms` on the switch (`fields.Switch.keyword`), no sub-aggregation | `recent_window` (default heartbeat interval × missed beats = `3m`) | `recent_window` ≥ one heartbeat interval and < the window; terms size ≤ 500 (default 500) |
| `recurrence` | fabric_health | `size: 0`, ONE `terms` on the location with ONE `date_histogram` sub-aggregation on `@timestamp` | none | `fixed_interval` must equal the registry interval (`1h`); total buckets ≤ 5000 |
| `context_window` | controller_log | a rows query that finds the event(s) | `before`, `after` (default `5m` each); optional `context_filter` (one clause, no time range) | `size` 1 to 3 (default 3); `before`/`after` ≤ 60 minutes |
| `text_ratio_shortfall` | controller_log | a rows query with a `match_phrase` on `Body` | none | `size` ≤ 100; `_source`, if given, includes `Body` |
| `ingest_lag` | ilo_events | a rows query | none | `size` ≤ 100; `_source`, if given, includes `@timestamp` and `Created` |

An analysis the family does not list is rejected with the advice to answer
`unmapped`.

## 6. Worked examples

Each of these passes the guard (they are tested against the fixture mappings).

A simple question ("Which controllers reported errors in the last 24 hours, by
host and service?"):

```json
{
  "pattern_id": "controller_log",
  "intent": "top_n",
  "query": {
    "size": 0,
    "query": { "bool": { "filter": [
      { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
      { "named_condition": "abnormal_severity" } ] } },
    "aggs": { "by_host": { "terms": { "field": "Resource.host.name.keyword", "size": 20 },
              "aggs": { "by_service": { "terms": { "field": "Resource.service.name.keyword", "size": 5 } } } } }
  }
}
```

"Which switches stopped sending heartbeats?":

```json
{
  "pattern_id": "fabric_health",
  "intent": "absence",
  "query": {
    "size": 0,
    "query": { "bool": { "filter": [
      { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
      { "term": { "fields.PhysicalContext.keyword": "runtime.heartbeat" } } ] } },
    "aggs": { "by_switch": { "terms": { "field": "fields.Switch.keyword" } } }
  },
  "analysis": { "type": "absence" }
}
```

"What did hmscd log in the ten minutes before the last link-down?":

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
  "analysis": { "type": "context_window", "before": "10m", "after": "1m",
                "context_filter": { "term": { "Resource.service.name.keyword": "hmscd" } } }
}
```

More examples, one per analysis, are in the three domain files.

## 7. What the result looks like

Every executed OpenSearch result carries `execution.metadata` (count
exactness, rows returned vs matched, aggregation approximation, partial
results, query window, observed range, severity vocabulary, redaction). An
analysis adds `execution.analysis` (type, parameters used, counts, whether its
parameters are verified, warnings, `queries_executed`, `query_purposes`), and
its rows may carry a per-row `analysis` note (`position`, `lag_seconds`,
`available` / `expected`). Rows also carry the friend's `severity_assessment`.
Skill text that explains an answer should point at these fields rather than
promise more.

## 8. Things the model must be told never to do (the guard rejects them anyway)

Write an `index`; use a wildcard, `_all`, a system index or another cluster;
use `script`, `query_string`, `regexp`, `wildcard`, `fuzzy`, `nested`, paging;
filter or group on a display-only time field (`Created`, `Timestamp`,
`fields.Timestamp`); group or sort on free text; assume a `.keyword`
sub-field; change a window to fit a limit; write `must_not` on a severity field;
compare severity in a different case; invent an analysis or a parameter.

## 9. Facts that are still unverified (confirm them with the smoke test)

`python scripts/smoke_test_opensearch.py --check-skills` prints the live list.
Today:

1. Heartbeat interval and "missing" threshold (registry: 60 s × 3).
2. The real wording of the fabric-manager availability message (registry
   pattern `n / m switches available`, case-insensitive).
3. What "recurring" should mean for faults re-sent every hour (registry: seen
   in separate one-hour intervals).
4. Whether the iLO `Created` field is a usable event clock for ingest lag.
5. The wording of a failed-login message, and what "unusually frequent" means
   (both still `unmapped`).
6. The full value lists of the severity fields (`documented` / `provisional`
   in the registry).
7. Daily index rotation (`-YYYY.MM.dd`), and the real mappings (`.keyword`
   sub-fields in the fixtures are assumptions).
8. Whether `fields.Port` in fabric health and the port token in controller
   text use the same naming.

## 10. Adding a fourth family later

1. Add a pattern to `index_registry.json` (copy a block; set `time_field`, the
   field lists, roles, `output_projection`, `severity_semantics`, and any
   `analyses` with `"verified": false`).
2. Write its domain file (with `` `pattern_id: <id>` `` and examples) and its
   routing row.
3. Run `python scripts/smoke_test_opensearch.py --check-skills`, then the live
   checks in `docs/SMOKE_TEST.md`.
4. Restart, or `POST /api/v1/admin/reload-skill`. No Python change is needed
   unless a new kind of analysis is required.
