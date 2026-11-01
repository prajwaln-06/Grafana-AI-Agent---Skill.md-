# Current status: the OpenSearch part, explained from zero

This file explains the whole OpenSearch part of the assistant, assuming you have
never used OpenSearch before. It covers the friend's design (registry, field
catalog, guard, severity, redaction, results) and the five multi-step analyses
that were added on top of it in this merge. Every example below is **real
output of the code**, produced with the test fixtures. Technical words are
explained the first time they appear, and there is a glossary at the end.

---

## 1. Read this first (two minutes)

**What does it do?** A person types a question in plain English, such as
"Which controllers reported the most errors in the last 24 hours?". The system
works out which log data in OpenSearch can answer it, writes a safe query,
checks that query mechanically, runs it, and returns a clean answer with notes
about how far the numbers can be trusted.

**What is finished?**

* The friend's OpenSearch design, unchanged: three registered log families, a
  strict query checker (the "guard"), severity rules, masking of personal data,
  and result metadata.
* Five questions that one query cannot answer now work end to end: "which
  switches stopped sending heartbeats?", "what happened before and after this
  event?", "how long after creation were events ingested?", "messages with fewer
  switches available than expected", and "which ports have recurring faults?".
  Before this merge they were answered with "not covered" (`unmapped`).
* Platform fixes: the server no longer freezes while it waits for the AI or a
  database, the API key and allowed websites are enforced, alert confirmation
  is safer, and Prometheus label lookups are cached.
* Tests: **1385** in total. On this Windows laptop 1384 pass and one fails
  only because it checks a Linux file permission; on Linux all 1385 pass
  (checked in a Linux container, see `progress.md`). The starting point was
  1231.

**What is not finished, on purpose?** Alerting and correlations on OpenSearch
(see `docs/NEXT_PHASE.md`, which keeps all 63 open questions). Nothing has been
run against your real cluster or the real Gemini model. Every test uses a
pretend cluster, so `docs/SMOKE_TEST.md` tells you how to check the real one.

**Your most useful commands:**

```
python -m pytest tests -q                                   # all tests
python scripts/smoke_test_opensearch.py --check-skills      # offline: registry and skill files agree? what is unverified?
python scripts/smoke_test_opensearch.py --queries --rows    # live: do the families and fields exist on the real cluster?
python scripts/smoke_test_opensearch.py --examples --analyses   # live: do the examples and the five analyses work?
python run_server.py                                        # starts the service on port 8000
curl localhost:8000/api/v1/opensearch/status                # health of the OpenSearch part
```

---

## 2. OpenSearch in five minutes

Think of OpenSearch as a very fast search engine for records, like a giant
spreadsheet that answers questions in milliseconds.

**A document** is one record: one log line or one event. It is written in
JSON, which is a list of names and values. A controller log line looks like
this (simplified):

```json
{
  "@timestamp": "2026-10-28T10:00:00Z",
  "Severity": "error",
  "Body": "LinkStatus Down on j105p0",
  "Resource": { "host": { "name": "x1000c2r3b0" }, "service": { "name": "hmscd" } }
}
```

**A field** is one name inside a document. `Severity` is a field, and so is
`Resource.host.name`; the dots mean "inside" (`name` inside `host` inside
`Resource`).

**An index** is a collection of documents of one kind, like one sheet of a
spreadsheet. A new index is usually created every day, with the date in its
name. An **index pattern** with a star, such as `controllerlog-*`, means "all
the daily indices of this family".

**A mapping** describes the type of each field, and the type decides what you
can do with it:

| Type | In simple words | What you can do |
|---|---|---|
| `keyword` | a value kept whole, such as `hmscd` | match it exactly, count it, group by it |
| `text` | a sentence cut into words, such as the `Body` line | search for words or a phrase; you cannot group by it |
| `date` | a point in time | filter by a time range, sort, draw a timeline |
| numbers, `boolean` | counts, codes, true/false | match, compare |

A `text` field often has a hidden exact copy called a **keyword sub-field**,
written `Severity.keyword`. If it exists, it is used for exact matching and
grouping. The code never assumes it exists: it reads the real mapping first.

**Query DSL** is the JSON language for asking OpenSearch a question. "Error
lines in the last hour" is:

```json
{ "query": { "bool": { "filter": [
    { "range": { "@timestamp": { "gte": "now-1h", "lte": "now" } } },
    { "term":  { "Severity.keyword": "error" } } ] } },
  "size": 5 }
```

`now-1h` is **date math** for "one hour before now". `filter` means
"documents must match this". `size` means "give me at most five documents".

**An aggregation** asks OpenSearch to count instead of returning documents.
"Count the errors per host" is a `terms` aggregation, and each group in the
answer is a **bucket**: a key (the host) and a `doc_count` (how many
documents).

**The time field matters most.** Every document has several times, and they
disagree. Here `@timestamp` is when the document was **ingested** (arrived in
OpenSearch); fields such as `Created` or `Timestamp` are when the event
supposedly happened, and they can be stale or wrong. So every filter uses
`@timestamp`, and the event times are only shown.

---

## 3. The three log families

The registry (section 6) describes three **families**, each with a stable
name called a `pattern_id`.

| `pattern_id` | Index pattern | What it holds |
|---|---|---|
| `fabric_health` | `slingshot_crayfabrichealth-*` | Slingshot network switch health: regular heartbeat documents and fault reports |
| `controller_log` | `controllerlog-*` | syslog-style messages from fabric controllers, with host and service |
| `ilo_events` | `log_iml-*` | the iLO / BMC hardware event log (Redfish) |

Three facts explain many design choices:

1. **Each family uses different field names and different severity words.**
   Fabric says `HEALTHY` / `CRITICAL`, the controller log says lower-case
   `info` / `error` / `critical`, and iLO has two severity fields (`OK`, and the
   vendor's `Informational` / `Caution` / ...). There is no common format.
2. **The fabric family mixes two kinds of document.** A heartbeat names a switch
   with an ASIC suffix (`x1000c3r1b0`), a fault without it (`x1000c3r1`). They
   are the same switch.
3. **Fabric faults are re-sent about every hour**, so "how many events" can be
   much larger than "how many different problems".

---

## 4. The journey of one question

**The question:** "Which controllers reported the most errors in the last 24
hours?"

**Step 1. The Router (an AI call) picks the reference file.** It reads the
routing table in `skills/SKILL.md` §4 and chooses
`references/opensearch-logs/controller-log.md`.

**Step 2. The system builds the context for the query writer.** `context.py`
combines what the registry **allows** with what the live mapping says
**exists**, and writes a rules block for the next AI call: the fields it may
use, which of them can be grouped, the exact severity words, the limits, and
"never write an index name".

**Step 3. The Generator (a second AI call) writes a contract.** The part that
matters here is:

```json
{
  "pattern_id": "controller_log",
  "measurement_used": {"type": "raw_metric", "name": "controller errors", "source_metrics": []},
  "query": {
    "query": { "bool": { "filter": [
      { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
      { "named_condition": "abnormal_severity" } ] } },
    "aggs": { "by_host": { "terms": { "field": "Resource.host.name.keyword" } } }
  }
}
```

Two things to notice. The model wrote a `pattern_id`, not an index name. And
instead of guessing which severity words mean "error", it wrote the **named
condition** `abnormal_severity`.

**Step 4. The guard checks it and fills only documented defaults.** The real
output is the query below, plus a list of what was filled in (`applied`):

```json
{
  "query": { "bool": { "filter": [
    { "range": { "@timestamp": { "gte": "now-24h", "lte": "now" } } },
    { "bool": { "filter": [ { "terms": { "Severity.keyword":
        ["alert", "crit", "critical", "emerg", "err", "error"] } } ] } } ] } },
  "aggs": { "by_host": { "terms": { "field": "Resource.host.name.keyword", "size": 10 } } },
  "size": 0,
  "timeout": "10s"
}
```

```
applied:
  expanded named_condition 'abnormal_severity' using the family's documented severity definition
  terms size defaulted to 10
  size defaulted to 0 for an aggregation query (buckets only)
  server-side timeout 10s added (an expired timeout is reported as a partial result)
```

The system set the index to `controllerlog-*` from the registry. The guard
changed nothing the model wrote. It only expanded the named condition into the
family's real error words and added values that were missing.

**Step 5. The validator runs the guard again.** If the query is not exactly
in the guard's canonical form, it is refused. This means no step can be
skipped by accident.

**Step 6. The executor runs it.** `app/executor.py` is the only code that
talks to OpenSearch.

**Step 7. The results module turns the raw answer into a clean result.** With
a pretend answer of 412 and 97 errors for two hosts, and 130 more errors on
other hosts, the real output is:

```json
{
  "execution_status": "success",
  "result_type": "buckets",
  "buckets": [ { "key": "x1000c2r3b0", "doc_count": 412 }, { "key": "x1000c6s7b1", "doc_count": 97 } ],
  "truncated": true,
  "metadata": {
    "count": { "value": 639, "relation": "eq", "exact": true },
    "aggregation": {
      "approximate": true,
      "approximate_reasons": [
        "terms counts may be off by up to 3 documents per bucket",
        "130 matching documents fall outside the returned buckets" ],
      "sum_other_doc_count": 130, "doc_count_error_upper_bound": 3, "type": "terms" },
    "partial": false,
    "redaction": { "applied": true, "redacted_usernames": 0, "redacted_ip_addresses": 0 },
    "severity_vocabulary": { "Severity": "documented" }
  }
}
```

The answer says honestly that only the top 2 of the hosts are shown
(`truncated: true`) and that bucket counts can be off by up to 3. The full
result also carries the query window.

**Where to look next:** `run_pipeline` in `app/pipeline.py` runs these steps;
`tests/test_opensearch_mentor_questions.py` runs this journey for each of the
mentor's 35 questions.

---

## 5. The code map: what each file does

All OpenSearch logic is in `app/opensearch_catalog/`. Each file has one job.

| File | Its job, in plain words | Open it when |
|---|---|---|
| `index_registry.json` | the reviewed list of the only three families the system may query, what each field means (its **role**), which fields may be searched, grouped and returned, what "abnormal" means, and the analysis parameters | an index or field changes, or an analysis parameter is confirmed |
| `registry.py` | reads that file and refuses to start if anything is inconsistent | you want to know which mistakes are caught |
| `field_catalog.py` | reads the live mapping and works out what each field can do (for example whether `Severity.keyword` exists in every recent daily index); caches it for 10 minutes | a query is refused with `unknown_field` or `operation_not_supported` |
| `context.py` | combines registry and live catalog for one question and writes the rules block the query-writing AI reads; holds the `Limits` | you want to know exactly what the AI is told |
| `guard.py` | the strict gate: allowed or rejected, with a reason code; fills only documented defaults | a question is refused |
| `canonical.py` | the pipeline hook that runs the guard before validation, and turns a too-long window into a polite "please narrow it" | you trace how a contract changes before it runs |
| `severity.py` | expands `abnormal_severity` into real values and labels every returned row `abnormal` / `normal` / `unknown` | a severity verdict looks wrong |
| `redaction.py` | masks user names, e-mail and IP addresses in everything returned | you need to know what is masked and what is not |
| `results.py` | turns the raw answer into rows/buckets/series plus metadata (exact or "at least", partial, approximate, observed range) | you read a result's `metadata` |
| `analysis.py` (new) | the five multi-step recipes (section 11) | you work on absence, context windows, lag, shortfalls or recurrence |
| `runtime.py` | keeps the registry and catalog caches for the whole process | rarely |

Files outside the folder that take part: `app/opensearch_client.py` (the HTTP
calls), `app/executor.py` (runs queries and analyses), `app/validator.py`
(re-runs the guard), `app/pipeline.py` (the conductor), `app/config.py` (every
setting, from the environment).

---

## 6. The registry: the one place that names indices and fields

`app/opensearch_catalog/index_registry.json` is the only place that names
indices and fields. **No index or field name is written in the Python code.**
That is why you can correct a field name without touching Python.

For each family it says: the index pattern; the time field (`@timestamp`);
display-only time fields (never filtered on); field **roles** such as
"`severity` is `Severity`", "`text` is `Body`", "`entity_host` is
`Resource.host.name`"; which fields may be searched, grouped and returned; which
caveats apply; what "abnormal" means; and, new, which **analyses** it supports
with their parameters. For example:

```json
"analyses": {
  "absence": { "entity_role": "entity_switch", "normalise_lowercase": true,
               "normalise_strip_suffix_regex": "b\\d+$",
               "heartbeat_interval_seconds": 60, "dead_after_intervals": 3,
               "verified": false, "source": "PLACEHOLDER. About one heartbeat per switch per minute ..." }
}
```

The loader checks the file at startup and refuses it on the first mistake:
for example a pattern that is just `*`, a grouped field that is not
searchable, a role pointing at an unlisted field, a regex that does not
compile, or an analysis without its `verified` flag. If the file is broken,
**every** OpenSearch question is refused with a clear message. The system never
runs half-configured.

A good way to think about the two sources of truth: the **registry** says what
is *allowed*; the **field catalog** says what is *possible right now*. A field
must be in both before a query may use it.

---

## 7. The guard: reject and explain, never repair

The friend's central rule is that **the guard never changes a value the model
wrote**. A value that is out of range is rejected with an explanation. Only a
value that is *missing* is filled, from a documented default, and that is
recorded in `applied`. Real examples:

| What the model wrote | Code | What the guard says |
|---|---|---|
| a 30-day window | `window_exceeds_max` | "The time range you asked for (about 30d) is longer than the 7d maximum for this log source. I have not changed it. Please narrow it to 7d or less (for example, the last 24 hours)." This is shown to the user as a clarification. |
| `"Severity.keyword": "ERROR"` | `severity_invalid` | "severity value 'ERROR' differs only in case from the stored value 'error'; compare the exact stored case." |
| a field called `Resource.node_id` | `unknown_field` | "field 'Resource.node_id' is not a field this index family permits." |
| `must_not` on an iLO severity field | `severity_invalid` | "a must_not on a severity field is not allowed ... because it would treat every unrecognised or empty value as abnormal. Use {"named_condition": "abnormal_severity"} instead." |
| a before/after span of 2 hours | `analysis_invalid` (new) | "`before` '2h' is longer than the maximum of 60m; it has not been shortened". The user is asked for 60 minutes or less. |

Other rules: only the three registered families, chosen by `pattern_id`; only
fields the registry permits *and* the live mapping confirms, each only for the
operations its type supports; exactly one time range on `@timestamp` with both
ends; no scripts, `query_string`, wildcards, regular expressions, nested
queries or paging; at most 100 rows, 50 groups (500 only for the absence
analysis), 200 timeline buckets, 3 levels of aggregation; a 10-second
server-side timeout.

The reason codes are: `context_unavailable`, `unknown_pattern`,
`index_mismatch`, `forbidden_key`, `unknown_field`, `operation_not_supported`,
`bad_clause`, `bad_value`, `time_range_invalid`, `window_exceeds_max`,
`limit_exceeded`, `aggregation_invalid`, `severity_invalid`,
`projection_violation`, and (new) `intent_invalid`, `analysis_invalid`.

If the cluster cannot be reached when the mapping is read, the last good
mapping is used for up to an hour and marked `stale`. After that the family is
refused instead of guessed.

---

## 8. Severity: what "abnormal" means, per family (`severity.py`)

The registry says, for each family, which stored values are normal, which are
abnormal, which are known but neither, and how far each list is trusted:
`observed` (seen in real data), `documented` (a published list, not seen here)
or `provisional` (an assumption).

**For the query**, `{"named_condition": "abnormal_severity"}` is expanded by
Python. For the controller log it becomes the six error words shown in
section 4. For iLO it becomes "the top-level severity is abnormal **or** the
vendor severity is abnormal". For fabric it uses the source team's own
definition: "the health field exists and is not `HEALTHY`".

**For each returned row**, a `severity_assessment` says which state the row is
in. There are four states, and a missing or unknown value never counts as
healthy. A real iLO row:

```json
"severity_assessment": {
  "state": "abnormal", "abnormal": true, "conflict": true,
  "basis": ["Oem.Hpe.Severity"],
  "fields": {
    "Severity":         { "value": "OK",      "class": "normal",   "vocabulary": "documented" },
    "Oem.Hpe.Severity": { "value": "Caution", "class": "abnormal", "vocabulary": "provisional" } },
  "missing_fields": [], "unrecognised_fields": [],
  "provisional_basis": ["Oem.Hpe.Severity"]
}
```

In words: one field says OK and the other says Caution, so the row is abnormal
(one abnormal field is enough), the two fields disagree (`conflict`), and the
verdict rests on a provisional vocabulary, so it must be presented as such. A
controller `warning` row is `unknown`, because `warning` is a known value that
is neither normal nor an error.

---

## 9. Masking personal data (`redaction.py`)

Every string that leaves the system passes through a masking step. Real
example:

```
in:  Login by user admin from 10.0.0.9 (session for alice@example.com)
out: Login by user [REDACTED_USER] from [REDACTED_IP] (session for [REDACTED_USER])
```

The result's `metadata.redaction` counts what was masked. IP addresses can be
kept when they are internal (setting `OPENSEARCH_REDACT_IP_MODE=keep_internal`)
or not masked at all (`off`).

Be clear about its limits: it is a **heuristic second layer, not complete
personal-data detection**. It masks a name only after a cue such as "user",
"login by" or "password for". It does not detect a bare name, `DOMAIN\name`,
host names, MAC addresses or serial numbers. The first layer is that rows only
ever contain the registry's allow-listed fields (`output_projection`).

---

## 10. What comes back: results and metadata (`results.py`)

The three shapes the frontend already knows are unchanged: `hits` (rows),
`buckets` (groups) and `series` (timelines). Each OpenSearch result adds a
`metadata` object; read it before stating a number:

| Field | What it tells you |
|---|---|
| `count` | `value`, `relation` (`eq` exact, `gte` "at least") and `exact` |
| `rows` | rows returned vs documents matched, and whether more exist |
| `aggregation` | whether group counts are estimates and why (`cardinality` is an estimate; `sum_other_doc_count` documents are outside the shown buckets) |
| `partial`, `partial_reasons`, `shard_failures` | some parts of the cluster failed or timed out, so data may be missing; if **all** fail, the result is an error, not "no data" |
| `query_window` | the window that was asked for |
| `observed_range` | earliest and latest `@timestamp` among the **returned** rows only, not the full data range |
| `severity_vocabulary`, `severity_vocabulary_provisional` | how far the severity words are trusted |
| `redaction` | what was masked, and the policy |

A count-only question ("how many critical events?") is a success carrying
`metadata.count`, not an "empty" result.

The entry itself also carries `guard_notes` (the defaults that were filled and
any warnings), so you can always see what the system added.

---

## 11. The five analyses (new in this merge)

Some questions cannot be answered by one query. Each has a small recipe in
`analysis.py`. The model writes the ordinary first query and a small
`analysis` object. Python builds every further query **from the already checked
query**. The guard checks each further query again before it is sent, and only
the executor sends it. All parameters come from the registry and are marked
**unverified** until you confirm them on the real cluster (the result says so,
for example `threshold_verified: false`).

### 11.1 `absence`: "Which switches stopped sending heartbeats?" (fabric)

A query can only find documents that exist; it cannot find "missing". So the
recipe lists the switches that sent heartbeats during the question's window
(the baseline), lists those that sent one in the last 3 minutes, and subtracts.
The 3 minutes come from the registry: one heartbeat every 60 seconds, missing
after 3 missed beats. These are **placeholders**.

Real example: baseline `x1000c0r1b0`, `x1000c1r2b0`, `x1000c2r3b0`; recent
`x1000c0r1b1`, `x1000c1r2b0`. The answer is that only `x1000c2r3b0` is missing.
`x1000c0r1` is alive, because its heartbeat arrived as `...b1` and the ASIC
suffix is ignored when comparing.

```json
"analysis": { "type": "absence", "recent_window": "3m", "baseline_entities": 3, "recent_entities": 2,
              "missing_count": 1, "complete": true, "heartbeat_interval_seconds": 60,
              "dead_after_intervals": 3, "threshold_verified": false, "queries_executed": 2 }
```

The second query was built by Python by narrowing the time range to
`now-3m .. now`; everything else was kept exactly. If nothing at all reported
recently, the answer warns that this may be an ingestion outage. If there are
more than 500 switches, it says the list is incomplete.

### 11.2 `context_window`: "What did hmscd log before and after the link-down?" (controller)

First the recipe finds the event (at most 3 events). Then, for each event, it
fetches the messages from `before` to `after` around it (default 5 minutes
each, at most 60), oldest first. The surrounding messages come from the same
host, unless a `context_filter` such as "service is hmscd" is given. Real rows,
with 2 minutes either side:

| time | severity | message | `analysis.position` | severity state |
|---|---|---|---|---|
| 09:59:10 | warning | port j105p0 flapping | before (−50 s) | unknown |
| 10:00:00 | error | LinkStatus Down on j105p0 | anchor | abnormal |
| 10:01:30 | info | LinkStatus Up on j105p0 | after (+90 s) | normal |

The follow-up query used the host value copied from the event
(`"Resource.host.name.keyword": "x1000c2r3b0"`). Only plain values (text,
numbers, true/false) are ever copied from a document into a query, never an
object.

### 11.3 `ingest_lag`: "How long after creation were events ingested?" (iLO)

Subtracting two times inside OpenSearch would need a script, and scripts are
forbidden. So the recipe fetches up to 100 rows and computes ingest time minus
`Created` in Python. Real result for three rows: smallest 90 s, median 180 s,
90th percentile and largest 2640 s (44 minutes). Each row carries its own
`{"lag_seconds": ...}`. It is a sample of the returned rows, not every event,
and `Created` is not yet confirmed as a reliable clock. The answer says both.

### 11.4 `text_ratio_shortfall`: "Fewer switches available than expected" (controller)

A message such as "30/32 switches available" holds two numbers, and a text
search cannot compare them. The recipe fetches the matching messages, reads
the two numbers with the registry's pattern, and keeps the rows where the
first is smaller. Real result: "30/32" is kept with
`{"available": 30, "expected": 32}`, and "32/32" is dropped. The pattern is a
**placeholder**: the real wording is not confirmed. If more messages matched
than were scanned, the count is labelled "at least".

### 11.5 `recurrence`: "Which ports have recurring faults?" (fabric)

Counting documents misleads here, because a fault re-sent every hour looks like
hundreds of events. The recipe counts in how many **separate hours** each port
appeared. Real result: a port with 30 documents across 3 hours ranks **above**
a port with 500 documents in 1 hour.

```json
"buckets": [ { "key": "x1000c3r1j9p2",   "doc_count": 30,  "metrics": { "active_intervals": 3.0 } },
             { "key": "x1000c3r1j104p1", "doc_count": 500, "metrics": { "active_intervals": 1.0 } } ]
```

What "recurring" should mean is still an open question (one-hour intervals is
the current placeholder, and a fault simply re-emitted every hour also scores
high).

### 11.6 What did not become an analysis

"Unusually frequent logins" (no baseline is defined), "failed logins" (the
wording is not confirmed) and questions that join two families or logs with
metrics stay `unmapped`, exactly as the friend's skill files say.

---

## 12. Platform fixes (Stage 1)

| Problem | What changed |
|---|---|
| While one person waited for the AI or a database, the whole server froze for everyone. | The AI calls, the context building (label lookups, mapping reads), query execution and Grafana calls now run in worker threads (`asyncio.to_thread`). A test proves the server keeps responding. |
| Prometheus labels were looked up one metric at a time on every request. | Answers are kept for 5 minutes (failures are never kept), and missing ones are fetched 8 at a time. |
| Reference files were read from disk on every request. | They are kept until the file changes. |
| `API_KEY` and `CORS_ALLOW_ORIGINS` existed but did nothing. | Both are enforced (section 13). |
| Confirming an alert after the feature was switched off still worked, and a Grafana outage threw the proposal away. | The switch is checked on every confirmation, and the proposal is kept when Grafana fails, so you can just confirm again. |
| `.env.example` was missing. | It is generated from the settings and a test stops it from going out of date. |

---

## 13. Security and operation

* **API key.** If `API_KEY` is set, every route except `/readyz` and `/health`
  needs the header `X-API-Key`. If it is empty the service is open (handy on
  your own machine) and the log warns at startup. **Set it before exposing the
  service.**
* **Allowed websites (CORS).** `CORS_ALLOW_ORIGINS` is applied. The default
  `["*"]` is only sensible locally.
* **Status page.** `GET /api/v1/opensearch/status` shows whether the registry
  is valid, the three families and their analyses, the limits in force, and how
  fresh each cached field catalog is. It never queries OpenSearch.
* **Startup log.** It says either "OpenSearch registry loaded: 3 enabled index
  families" or why OpenSearch is disabled.
* **Every limit is a setting** (names in `.env.example`), including the new
  `OPENSEARCH_ANALYSIS_MAX_TERMS`, `OPENSEARCH_ANALYSIS_ANCHOR_LIMIT` and
  `OPENSEARCH_ANALYSIS_CONTEXT_MAX_MINUTES`.
* **Not yet supported:** HTTPS with a private certificate authority (TLS
  options).

---

## 14. What has not been verified (and how to verify it)

Nothing here has touched your real OpenSearch, Grafana or Gemini. In
particular:

* **The mappings are synthetic.** The test fixtures were written from the
  documented document shapes; the `.keyword` sub-fields in them are
  assumptions. `smoke_test_opensearch.py --save-mappings` fetches the real ones.
* **The analysis parameters are placeholders:** heartbeat 60 s × 3, the
  "n/m switches available" wording, "recurring" = separate one-hour intervals,
  `Created` as the iLO event clock.
* **Severity vocabularies** are `documented` or `provisional` except fabric's
  observed `HEALTHY` / `CRITICAL`.
* **Daily index rotation** (`-YYYY.MM.dd`) is unconfirmed.
* **Whether the real AI model routes the questions correctly and writes these
  query shapes.** The tests fake both AI calls.

`docs/SMOKE_TEST.md` gives the exact commands, and `--check-skills` prints the
current list of unverified facts.

---

## 15. Decisions that need your attention (flags)

The full list with reasons is in `progress.md`. The short version:

| Flag | What to decide |
|---|---|
| F-1 | Prometheus label sets are cached for 5 minutes; a brand-new label can take that long to appear. |
| F-2 | With `API_KEY` unset the service is open. Set it before real use. |
| F-3 | No TLS options for OpenSearch yet (needed if the cluster uses HTTPS with a private certificate). |
| F-4 | The analysis parameters are placeholders until the smoke test confirms them. |
| F-5 | The frontend should learn the new optional fields (`execution.analysis`, a row's `analysis` note); nothing existing changed meaning. |
| F-6 | One test fails on Windows only (a Linux file-permission check). On Linux all 1385 pass. |

---

## 16. What to do next, in order

1. Run `python -m pytest tests -q` and confirm the count.
2. On the cluster host, follow `docs/SMOKE_TEST.md` steps 1 to 4.
3. Replace the synthetic fixtures with the real mappings and fix anything the
   tests then report (registry or skill examples, never the tests' rules).
4. Set `"verified": true` for each confirmed registry fact.
5. Ask the mentor's questions through the real service and compare a few
   answers with OpenSearch Dashboards.
6. Fill in the answers in `docs/NEXT_PHASE.md` and start alerting and
   correlations.

| If you want to know ... | Open |
|---|---|
| why something was decided, and what is flagged | `progress.md` |
| how the whole OpenSearch part works | this file |
| what the skill files must contain | `docs/SKILL_AUTHOR_CONTRACT.md` |
| how to test against the real cluster | `docs/SMOKE_TEST.md` |
| what is left for alerts and correlations | `docs/NEXT_PHASE.md` |
| which indices and fields the system knows | `app/opensearch_catalog/index_registry.json` |
| why a query was refused | the reason code in the server log, then `guard.py` |
| how each mentor question behaves | `tests/test_opensearch_mentor_questions.py` |
| the API contract for the frontend | `HANDOFF.md` |

---

## 17. Glossary

* **Aggregation:** asking OpenSearch to count or summarise instead of returning documents.
* **Analysis:** one of the five multi-step recipes for questions a single query cannot answer.
* **Anchor:** the event a context window is built around.
* **Bucket:** one group in an aggregation: a key and a count.
* **Canonical form:** the exact query the guard accepts (defaults filled in); the validator refuses anything else.
* **Date math:** relative times such as `now-24h`.
* **Document:** one record, such as one log line.
* **DSL (Query DSL):** the JSON language used to ask OpenSearch a question.
* **Executor:** the only part of the system that talks to OpenSearch.
* **Family / `pattern_id`:** one of the three registered kinds of log, named by a stable id.
* **Field catalog:** the live list of fields and what each can do, read from the mapping.
* **Guard:** the strict checker; it rejects and explains, and fills only documented defaults.
* **Index / index pattern:** a collection of documents (often one per day) / all indices of a family (`controllerlog-*`).
* **Ingest time:** when a document reached OpenSearch (`@timestamp`).
* **Keyword (sub-field):** a field matched whole, often an exact copy of a text field (`Severity.keyword`).
* **Mapping:** the description of every field's type in an index.
* **Named condition:** `abnormal_severity`, a placeholder the system expands into the family's real "abnormal" values.
* **Redaction:** masking user names, e-mail and IP addresses.
* **Registry:** the reviewed JSON file listing the allowed families and the meaning of their fields.
* **Role:** the meaning of a field ("severity", "free text", "host"), independent of its real name.
* **`unmapped`:** the honest answer "this is not covered", instead of a guess.
* **Validator:** the final gate before execution; for OpenSearch it re-runs the guard.
* **xname:** the hardware naming scheme, such as `x1000c3r1`.
