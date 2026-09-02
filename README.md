# observability-query-builder backend

Standalone FastAPI service that takes a natural-language question, runs it
through the `observability-query-builder` skill package (Router → Generator
→ deterministic Validator → Executor), executes the resulting query against
Prometheus and/or OpenSearch, and returns frontend-ready, chart-shaped JSON.
For ordinary requests this is one Router and one Generator call. An opt-in
dependency-aware path can execute a root query before constructing a dependent
query such as "memory available on that node"; see `HANDOFF.md` and `test.md`.

**If you're on the frontend team, or just want the full picture (input/output
contract, what's implemented vs. pending, how to test, how to update this
folder later): read [`HANDOFF.md`](./HANDOFF.md) first.** This README is the
short, developer-facing "how do I run this" version.

## Before trying real questions: verify the two things that couldn't be tested from a sandbox

Neither the Gemini API call nor a live Prometheus/OpenSearch connection could
be exercised while building or reviewing this (no network path to either from
that environment). Run these two scripts, in order, before trying real
questions — they isolate exactly which layer is broken if something doesn't
work, instead of debugging a full pipeline run blind:

```bash
python3 scripts/smoke_test_gemini.py       # is the SDK call itself working?
python3 scripts/smoke_test_prometheus.py   # is Prometheus reachable the way the code expects?
```

If you're also turning on `ALERT_RULE_CREATION_ENABLED` (SKILL.md §12), run
one more, first:

```bash
python3 scripts/smoke_test_grafana.py      # is Grafana reachable, and do the folder/datasource UIDs actually exist?
```

Once these pass, work through `skills/evals/regression-cases.md` — the skill
package's own hand-authored test questions, covering routing, ambiguity,
comparisons, panic mode, and label-fabrication prevention. That's the
concrete, ready-made test plan; no need to invent test questions from
scratch. `HANDOFF.md` has a fuller step-by-step testing checklist.

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt   # includes prod requirements + pytest/httpx

cp .env.example .env                  # fill in a real GEMINI_API_KEY
# Confirm PROMETHEUS_URL / OPENSEARCH_URL match your local setup
# (defaults: http://localhost:9090, http://localhost:9600)
# Keep DEPENDENT_QUERY_RESOLUTION_ENABLED=false for the legacy flat path.
# Set it to true, then restart, to enable staged dependent compound queries.

python run_server.py
```

Then:

**PowerShell (Windows):**
```powershell
# Using Invoke-RestMethod (recommended for PowerShell)
Invoke-RestMethod -Uri http://localhost:8000/api/v1/query -Method Post -ContentType "application/json" -Body '{"question": "compare CPU utilization on node-1 and node-2 over the last hour"}'

# Or using curl.exe (call curl.exe explicitly in PowerShell to bypass the Invoke-WebRequest alias)
curl.exe -s -X POST http://localhost:8000/api/v1/query -H "Content-Type: application/json" -d "{\"question\": \"compare CPU utilization on node-1 and node-2 over the last hour\"}"
```

**Bash / macOS / Linux:**
```bash
curl -X POST http://localhost:8000/api/v1/query \
  -H 'Content-Type: application/json' \
  -d '{"question": "compare CPU utilization on node-1 and node-2 over the last hour"}'
```

## Running tests

```bash
pytest
```

All tests are deterministic (every backend and LLM call is mocked) — no live
Prometheus/OpenSearch/Gemini connection required to run the suite. Run
`pytest -q` and check the final count yourself; don't trust a number typed
here, since it drifts every time a test is added.

## Project layout

```
run_server.py          FastAPI wrapper instantiating the ADK ApiServer and
                         overlaying compatibility routes.
app/
  agent.py              Defines the custom ADK Agent ObservabilityQueryBuilderAgent.
  skill_index.py        Parses SKILL.md's routing table + sections. The
                         single source of truth for "what does this skill
                         cover and where does each piece live" -- nothing
                         about specific exporters/metrics is hardcoded here.
                         This is what makes a new domain file "automatically
                         known" after adding a routing row + restart/reload.
  time_utils.py          Relative time expression resolution (now, now-1h,
                          now/d, ...), shared by both backends.
  prometheus_client.py   Prometheus HTTP client: instant + range queries,
                          automatic step-widening on "too many samples".
  opensearch_client.py   OpenSearch HTTP client: search/aggregation
                          execution, plus live index/mapping discovery.
  label_discovery.py     Live Prometheus label-key discovery (Principle 9).
  field_discovery.py     Live OpenSearch Attributes.* key discovery.
  normalizer.py          Converts raw backend responses into the three
                          frontend-facing shapes: series / buckets / hits.
                          Also where NaN/Infinity sanitization happens.
  validator.py            Deterministic (no-LLM) contract validation --
                          structural conformance, fabricated-metric and
                          fabricated-label detection, time-grammar checks.
  executor.py             Deterministic dispatch: runs a validated contract
                          entry against the right backend, per-entry
                          failure isolation.
  llm_client.py           The only module that calls the Gemini API.
  grafana_client.py       Thin wrapper around Grafana's Alerting
                          Provisioning HTTP API. The ONLY module that ever
                          writes to Grafana, and only ever called from
                          run_server.py's confirmation endpoint --
                          never from pipeline.py/executor.py. See SKILL.md
                          §12 and HANDOFF.md's "Alert Rule Creation" section.
  pipeline.py             Router -> Generator -> Validator orchestration,
                          partial-datasource-coverage handling, and the
                          opt-in staged dependent-query/synthesis path.
  config.py                Typed settings (env vars, see .env.example).
skills/                    The observability-query-builder skill package
                            (SKILL.md + references/). Replace this directory
                            wholesale to update the skill; nothing in app/
                            needs to change for routing-table-level updates.
tests/                     Unit + integration tests for every module above,
                            run with `pytest`.
```

## API

### `POST /api/v1/query`

```json
{"question": "compare CPU utilization on node-1 and node-2 over the last hour"}
```

Returns `{"result": {...}, "session_id": null}`. `result` is SKILL.md §9's
Output Contract with an `execution` block attached to every `ok`/
`panic_mode_best_effort` entry. If the result needs a clarifying answer
(`ambiguous_metric`, or `declined` with reason
`parameter_requires_clarification`), `session_id` is non-null and the query
is NOT executed yet — send it back as:

```json
{"question": "the available one, not free", "session_id": "<the returned id>"}
```

See `HANDOFF.md` for the full, worked-example breakdown of every shape
`result` can take.

### `POST /api/v1/alerts/confirm`

SKILL.md §12's confirmation step -- the only endpoint that ever creates
anything in Grafana. Only reachable when `ALERT_RULE_CREATION_ENABLED=true`
(see `.env.example`); disabled by default. Takes ONLY a `session_id` from a
prior `alert_rule_proposed` result, never a restated rule payload:

```json
{"session_id": "<the id returned alongside an alert_rule_proposed result>"}
```

```json
{"session_id": "<...>", "confirm": false}
```

`confirm` defaults to `true`. Setting it to `false` discards the proposal
without creating anything -- the session is consumed either way (single-use).
The alert feature flag is checked again immediately before every Grafana
write, and explicit alert-creation wording is deterministically refused when
the flag is off even if an LLM omits its alert-intent annotation. A proposal
retained from an earlier enabled session therefore cannot bypass a later
disabled setting.
Returns `{"status": "created", "rule_uid": "...", "deeplink": "..."}` on
success, or a 4xx/5xx with an explanatory `detail` otherwise (expired/
unknown session → 410; session isn't a pending alert proposal → 409; feature
disabled → 403; Grafana misconfigured → 500; Grafana unreachable/erroring →
502; a rule with the same title/folder already exists → 409). See
`HANDOFF.md`'s "Alert Rule Creation" section for the full flow and required
Grafana-side setup.

### `GET /health` / `GET /readyz`

Liveness / readiness. `/readyz` confirms the skill package loaded.

### `GET /api/v1/capabilities`

Introspection: the currently-loaded skill's routing table and the two
non-secret feature flags actually loaded by this server process, so a
frontend can know what's covered and whether optional capabilities are on
without learning it from a failed query. This is the authoritative way to
verify an `.env` flag after restarting the server.

### `POST /api/v1/admin/reload-skill`

Re-parses SKILL.md's routing table without a process restart — needed after
adding a brand-new routing row (a new exporter, a new previously-unrouted
domain file). Editing the *content* of an already-routed reference file
needs no action at all — it's read fresh from disk every request. See
`HANDOFF.md`'s "Adding new coverage" section for the full explanation of
which is which.

## What's implemented vs. pending

See `HANDOFF.md` for the full breakdown. Short version: the entire
Prometheus path is implemented end-to-end. The OpenSearch EXECUTION layer
(client, discovery, normalizer) is implemented and unit-tested against
realistic mocked responses — but no `opensearch-*` domain rows exist in
SKILL.md's routing table yet, so the pipeline can't currently route a real
question to OpenSearch data. A question that needs OpenSearch-backed data
gets an explicit `unmapped` result explaining that, rather than silently
failing or being dropped — including when it's only PART of a larger
question that also needs Prometheus data (see `HANDOFF.md`).

Alert-rule CREATION (SKILL.md §12) is implemented end-to-end but disabled by
default (`ALERT_RULE_CREATION_ENABLED=false`) — see `.env.example`'s
`GRAFANA_*` block and `HANDOFF.md`'s "Alert Rule Creation" section before
turning it on. This does not extend to silencing, deleting, or modifying an
existing alert, which remains fully out of scope with no exception,
unaffected by this flag either way.

Dependency-aware compound resolution is also implemented but disabled by
default (`DEPENDENT_QUERY_RESOLUTION_ENABLED=false`). When enabled, a query
whose later scope depends on an earlier result is resolved in stages and may
receive deterministic, data-grounded multi-result `synthesis`. See
[`test.md`](./test.md) for the verification procedure.
