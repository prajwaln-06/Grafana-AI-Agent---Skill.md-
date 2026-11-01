# Smoke test on the real cluster

Every automated test in this repository uses a **pretend** OpenSearch. The
fake network sits behind the real client code, and the field mappings come from
**synthetic** fixtures written from the documented document shapes
(`tests/fixtures/opensearch/README.md`). That proves the logic. It cannot prove
that the real cluster looks the way the registry says it does, that its
`.keyword` sub-fields really exist, or that the placeholder analysis
parameters are right. These steps do.

Run them once on the host that can reach OpenSearch (the "admin" host), and
again after any change to `app/opensearch_catalog/index_registry.json`. All of
it is read-only: nothing is written to OpenSearch, and the tool never prints a
document, a field value, a user name, an address or a credential. It prints
field names, counts and true/false only.

## 0. Prepare (about 5 minutes)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Then edit `.env`:

* `GEMINI_API_KEY` must be set for the settings to load. For steps 1 to 4 any
  non-empty value works (no AI is called); step 5 needs the real key.
* `OPENSEARCH_URL`: the REST endpoint, normally port **9200**. Port 9600 is
  the performance analyser and does not answer searches.
* Only if the cluster needs them: `OPENSEARCH_AUTH_USERNAME`,
  `OPENSEARCH_AUTH_PASSWORD`. (HTTPS with a private certificate authority is
  not supported yet. See `progress.md` flag F-3.)

## 1. Offline check: does the registry match the skill files? (no network)

```bash
python3 scripts/smoke_test_opensearch.py --check-skills
```

It passes when, for each of the three families, the registry loads, the domain
file exists, `SKILL.md` has a routing row with data source `opensearch`, the
file states its `pattern_id`, the file shows an example of exactly the analyses
the registry configures for it, and every JSON example is real JSON. It then
lists **every registry fact that is still unverified**. That list is your
checklist for steps 3 and 4.

## 2. Live check: do the families exist, and are their fields there?

```bash
python3 scripts/smoke_test_opensearch.py
python3 scripts/smoke_test_opensearch.py --queries      # + one bounded count query per family
python3 scripts/smoke_test_opensearch.py --rows         # + a few rows: are the projected fields returned?
```

Read the output family by family:

| Line | Meaning | What to do |
|---|---|---|
| `usable=True` | the live mapping confirms the time field and at least one permitted field | nothing |
| `registered fields NOT in the live mapping: ...` | the registry permits a field the cluster does not have | fix the field name in the registry (a typo, or it lives under another path) |
| `role bindings dropped: severity:Severity` | a role points at a missing field | same as above; until fixed, that role cannot be used |
| `variation=type_conflict` | a field has different types on different days | queries on that field are refused until the mapping is consistent |
| `no live mapping could be fetched` | unreachable, not authorised, or no matching index | check the URL, credentials and that the account may read `GET /<index>/_mapping` |
| `count={'value': N, 'relation': 'eq', ...}` | URL, credentials and time field work | nothing |
| `NOT SEEN in the sample` | a projected field never appeared in the few rows read | may be optional; look at a few documents in OpenSearch Dashboards |

## 3. Replace the synthetic mappings with the real ones

```bash
python3 scripts/smoke_test_opensearch.py --save-mappings /tmp/os-mappings
```

This writes one `mapping_<pattern_id>.json` per family, with field names and
types only (no documents). **Review them before committing anything.** Then,
on a development machine, copy them over `tests/fixtures/opensearch/` and run
`python -m pytest tests -q`. Any test that now fails is showing a place where
the real cluster differs from what the code and the skill files assumed. The
most likely example is a `.keyword` sub-field that does not exist. Fix the
registry or the skill example; do not weaken the test.

## 4. Do the skill examples and the analyses work on the real data?

```bash
python3 scripts/smoke_test_opensearch.py --examples     # every skill-file example vs the LIVE mapping (nothing runs)
python3 scripts/smoke_test_opensearch.py --analyses     # + run each analysis example once (counts only)
```

`--examples` puts every JSON example from the three domain files through the
guard against the real mapping. A rejection means the model would be shown an
example the system refuses. Fix the example in the skill file.

`--analyses` runs each of the five analysis examples once and prints only
counts. Use the numbers to confirm or correct the placeholder parameters:

| Analysis | Look at | It confirms |
|---|---|---|
| `absence` (fabric) | `baseline_entities` vs `recent_entities` | the heartbeat placeholder (60 s × 3 = 3 minutes). If many switches look "missing" while the fabric is healthy, the interval is longer than 60 s: change `heartbeat_interval_seconds` in the registry. |
| `recurrence` (fabric) | `intervals_in_window`, `result_count` | that one-hour intervals give a sensible ranking (what "recurring" should mean is a question for the mentor) |
| `context_window` (controller) | `anchors_found`, `anchors_expanded` | that a "LinkStatus Down" event is found and its neighbours come back |
| `text_ratio_shortfall` (controller) | `rows_scanned` vs `rows_matching_pattern` | the `n/m switches available` pattern. If rows are scanned but none match, the real wording differs: correct `regex` in the registry. |
| `ingest_lag` (iLO) | `rows_measured`, `median_seconds`, `p90_seconds`, `negative_lag_rows` | that `Created` is a usable event clock (many negative lags mean it is not) |

When a value is confirmed, set `"verified": true` for that analysis in
`index_registry.json` (and update its `source` note). The results then stop
saying the parameter is unverified.

## 5. Start the service and ask the real questions

```bash
python3 run_server.py
curl -s localhost:8000/api/v1/opensearch/status        # add -H "X-API-Key: ..." if API_KEY is set
```

Expect `"registry_valid": true`, three patterns with their `analyses`, and
`field_catalogs` filling in after the first question. The startup log says
`OpenSearch registry loaded: 3 enabled index families`.

```bash
curl -s localhost:8000/api/v1/query -H 'content-type: application/json' \
  -d '{"question": "Which controllers reported the most errors in the last 24 hours?"}'
```

For each of the mentor's questions (they are listed in
`tests/test_opensearch_mentor_questions.py`), record: did it route to the
right family; did it pass validation (if not, the result is
`validation_failed` and the server log names the reason code); and do the
numbers match the same query in OpenSearch Dashboards. Check on purpose:

| Check | Where to see it |
|---|---|
| The model never wrote an index | `result.index` equals the registry's pattern for `result.pattern_id` |
| Defaults the system filled in | `result.guard_notes.applied` |
| "At least N" counts are labelled | `execution.metadata.count.exact` is `false` and `relation` is `gte` |
| Duplicate fabric faults are mentioned | the explanation, when a count depends on them |
| Severity verdicts on provisional vocabulary are labelled | `execution.metadata.severity_vocabulary_provisional` |
| User names and addresses are masked | `execution.metadata.redaction` |
| An analysis ran, and on which parameters | `execution.analysis` (`threshold_verified`, `pattern_verified`, ...) |
| A 30-day window is refused, not shortened | ask for "the last 30 days": the answer asks you to narrow it to 7 days |
| A 2-hour before/after span is refused, not shortened | ask what hmscd logged "two hours before and after" a link-down |

## 6. Record what you learned

* Set `"verified": true` on each registry fact you confirmed (`date_rotation`,
  `analyses.*`; severity vocabularies move from `provisional` / `documented`
  to `observed`).
* Write the answers you got into `docs/NEXT_PHASE.md` (questions A5 to A9, A14,
  D3 and D4 are the ones this smoke test touches).
* Run `python3 scripts/smoke_test_opensearch.py --check-skills` again; it
  lists what is still unverified.

## If something fails

| Symptom | Likely cause |
|---|---|
| `endpoint_unreachable` | wrong `OPENSEARCH_URL` (9600 is not the REST port), firewall, or HTTPS that needs a CA bundle (not supported yet) |
| `discovery=unavailable reason=fetch_failed` | the account may not be allowed `GET /<index>/_mapping`; ask for read access on the three families |
| `discovery=unavailable reason=no_matching_indices` | the index pattern in the registry does not match any real index name |
| `validation_failed` with `unknown_field` | the registry permits a field the live mapping lacks (step 2 shows which) |
| `validation_failed` with `operation_not_supported` | a text field without a confirmed `.keyword` was used for grouping or exact match; the skill example must change |
| `validation_failed` with `analysis_invalid` | the model wrote an analysis parameter outside its limit, or an analysis the family does not support; the reason says which |
| `window_exceeds_max` (asked to narrow) | working as designed: windows over 7 days are never shortened silently |
