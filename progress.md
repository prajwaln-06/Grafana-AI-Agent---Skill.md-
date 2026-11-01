# progress.md — merge log and decision record

This file records the work of porting the valuable parts of a second,
independent implementation (`reference-mine`) onto this branch (the friend's
`feat/opensearch-integration`). Every decision has a number (`D-xx`) so it
can be found later. Anything marked **FLAG** is something the owner should
confirm or overrule.

The rule that governed every choice: **the friend's branch wins every design
conflict.** Her contract (`pattern_id`, `raw_metric`), her guard strictness
("reject and explain, never change a value the model wrote"), her severity,
redaction and results behaviour, and her skill wording were kept. Nothing was
merged with `git merge`; every change was made by hand, in stages, with tests.

---

## 1. Starting point (verified, not assumed)

- Code: `work/Grafana-AI-Agent---Skill.md--feat-opensearch-integration/` (the
  friend's branch). Reference (read-only): `reference-mine/repo/`.
- Baseline test run before any change, on this Windows laptop with Python
  3.12.10: **1230 passed, 1 failed**. The one failure is
  `tests/test_reviews.py::test_round_trip_is_deterministic_and_sorted`. It
  checks that a written file has the Linux permission bits `0o644`; Windows
  always reports `0o666`. The reference repository fails the same test on this
  machine, so it is a property of the operating system, not of the code. On
  Linux the baseline is the expected 1231. The test was left unchanged.

---

## 2. Decisions

### Process

- **D-01 A separate git repository was created inside the work folder.**
  Neither folder had its own git history; both were inside an empty git
  repository that covers the whole user profile (`C:\Users\csepr`). Running
  `git add -A` there would have tried to commit the entire profile. A new
  repository was therefore initialised in the work folder (branch
  `feat/opensearch-integration`), and the friend's code was committed
  unchanged as `baseline` before any edit. Each stage is one further commit,
  so any stage can be reviewed (`git show`) or undone (`git revert`).
- **D-02 Dependencies live in a virtual environment outside both repositories**
  (`Promethues-Opensearch/.venv`), so nothing generated is committed.
- **D-03 The Windows-only permission test is not weakened.** It is excluded
  only when counting on this laptop and is reported separately. Stage 5 states
  the count with and without it.

### Stage 1 — platform and Prometheus fixes

- **D-04 Label discovery is cached and parallel** (`app/label_discovery.py`,
  taken almost unchanged from the reference because the friend's file was
  identical to `main`). A successful label set per metric is reused for
  `LABEL_DISCOVERY_CACHE_TTL_SECONDS` (default 300 s); a failure is **never**
  cached, so a Prometheus hiccup heals on the next request; cache misses are
  fetched in parallel by up to `LABEL_DISCOVERY_MAX_WORKERS` (default 8)
  threads. Example: a question touching 12 metrics used to make 12 calls one
  after the other on every request; now it makes them at most once every five
  minutes, 8 at a time. Discovery is still done for every known metric, not
  narrowed (the validator's "only confirmed labels" rule needs them all).
  **FLAG F-1:** confirm 5 minutes is an acceptable delay for a brand-new label
  to become visible.
- **D-05 Reference files are cached until they change** (`app/skill_index.py`,
  `_read_text_cached`, keyed by modification time and size). An edited file is
  read again on the next request.
- **D-06 Blocking work no longer runs on the event loop.** In `app/pipeline.py`
  (re-applied by hand, the friend's pipeline was not overwritten): both LLM
  calls (Router and Generator), every `_build_generator_context` call (label
  discovery and OpenSearch mapping reads), and both `executor.execute_contract`
  calls of the dependent-query path now run through `asyncio.to_thread`. In
  `app/agent.py` the main `execute_contract` call and the Grafana call of the
  chat "yes" path do too, and so does the Grafana call of the HTTP confirm
  endpoint. A test proves a ticking task keeps running while the pipeline
  waits on slow fakes. Why it matters: while one person waited for the AI, the
  whole server used to be frozen for everybody else.
- **D-07 The chat "yes" path re-checks the alert feature switch, and keeps the
  proposal when creation fails for a reason that can go away** (switch off,
  Grafana unreachable or misconfigured, unexpected error). The proposal is
  cleared only on success or when the rule already exists (`conflict`). The
  same rule now applies to `POST /api/v1/alerts/confirm`. Example: Grafana is
  down, the person says "yes", gets the error, and can simply say "yes" again
  later instead of re-asking the original question.
- **D-08 The misspelled `_get_get_skill_index_or_reload` was renamed**
  `_get_skill_index_or_reload`. Nothing else referred to it.
- **D-09 `API_KEY` is enforced** (`run_server.py`): when it is set, every route
  except `/readyz` and `/health` needs the header `X-API-Key` (compared in
  constant time, so the key cannot be guessed by timing). **FLAG F-2:** when
  `API_KEY` is empty the service stays open (convenient for local work) and a
  warning is logged at startup. Set it before exposing the service.
- **D-10 `CORS_ALLOW_ORIGINS` is applied for real.** The setting existed but
  nothing used it. The default `["*"]` is only sensible on your own machine.
  Browser pre-flight (`OPTIONS`) requests need no key.
- **D-11 Startup readiness log and `GET /api/v1/opensearch/status`.** At
  startup the log says whether the OpenSearch registry loaded (loading it also
  proves each family's skill file exists) or why OpenSearch is disabled. The
  status endpoint shows the registry state, the enabled families and their
  analyses, the limits in force, and what the field-catalog cache currently
  holds. It never queries OpenSearch: a small read-only `snapshot()` was added
  to the friend's `FieldCatalogCache` for this (additive, no behaviour change).
  The reference's in-process counters were not ported (the friend's code has
  no counter mechanism and nothing needs it yet).
- **D-12 `.env.example` is generated from `Settings.model_fields`**
  (`scripts/generate_env_example.py`), and `tests/test_env_example.py` fails if
  the committed file differs, so it cannot drift. Required values are empty;
  secrets are never written.
- **D-13 Lint hygiene.** `ruff check app run_server.py --select F,E9` reported
  unused imports in the friend's OpenSearch modules and `run_server.py`. They
  were removed. In `guard.py` the unused variable `info` was dropped but the
  validating call `self.info(...)` it held was kept, so no check was lost.
- **D-14 Not ported from the reference in Stage 1:** TLS options
  (`OPENSEARCH_VERIFY_TLS`, `OPENSEARCH_CA_BUNDLE`) and the removal of the
  legacy `app/field_discovery.py`. Neither was in the Stage 1 list, the
  friend's client has no TLS parameters yet, and `field_discovery.py` is still
  referenced by the friend's tests. **FLAG F-3:** if the real cluster uses
  HTTPS with a private certificate, TLS options will be needed.

### Stage 2 — the five analyses

- **D-15 Two new optional contract keys, nothing else changed.** `intent` (one
  of `rows`, `count`, `top_n`, `trend`, `distinct`, or an analysis name) and
  `analysis` (`{"type": "<analysis>", ...parameters}`). `pattern_id`,
  `raw_metric`, `query` and "never write an index" keep their meaning. A simple
  question carries neither key and is checked exactly as before (a test proves
  it). A simple `intent` is descriptive only; it changes no check.
- **D-16 Two new rejection codes in the friend's style:** `intent_invalid` (an
  unknown intent value) and `analysis_invalid` (everything about the analysis
  object). They are produced inside her guard, so the validator, the
  canonicalisation hook and the retry path treat them like her other codes.
- **D-17 Reject, never repair, for analysis parameters too.** Examples: a
  context window of `before: "9h"` is rejected with "longer than the maximum of
  60m; it has not been shortened" (the reference clamped it to 60m); a
  `recent_window` of `30s` for heartbeats is rejected because it is shorter
  than one heartbeat interval; a missing `_source` field needed to measure
  ingest lag is rejected, never added. Only documented defaults are filled, and
  only when the key is absent, and each is recorded in `applied`:
  `recent_window` (= heartbeat interval × missed beats from the registry, today
  3m), `before`/`after` (registry default 5m), the anchor query size (3), and
  the absence terms size (500).
- **D-18 Registry `analyses` block per family, validated at load.** Unknown
  analysis types or parameters, roles that do not exist, a role that is not
  groupable (for set differences and recurrence) or not returned in rows, a
  regex that does not compile or does not have exactly two capture groups, a
  bad duration, and a missing `verified` flag all stop the registry from
  loading (12 tests, one per mistake). Every analysis is `"verified": false`
  and carries a `source` note saying what is a placeholder.
  Fabric: `absence` (switch, ASIC suffix `b<digits>` ignored, lower-cased,
  heartbeat 60 s × 3) and `recurrence` (location, 1-hour intervals).
  Controller: `context_window` (same host, ±5 minutes) and
  `text_ratio_shortfall` (case-insensitive `n / m switches available`, keep
  n < m). iLO: `ingest_lag` (`@timestamp` minus `Created`).
  **FLAG F-4:** heartbeat 60 s × 3, the `n/m` wording, the 1-hour meaning of
  "recurring" and whether `Created` is trustworthy are placeholders. Only the
  real cluster can confirm them.
- **D-19 The model writes the ordinary query; Python builds every follow-up
  from it, and the friend's guard checks each follow-up again before it is
  sent.** The executor builds one `runner` that (1) re-validates the query
  with `guard.check_entry` against the live field catalog and (2) sends it
  through the friend's `opensearch_client`. So the executor is still the only
  code that talks to OpenSearch, and a follow-up can never use a field, size or
  window the guard would refuse. Before any query, the executor also
  re-validates the whole analysis entry and refuses it if it is not exactly the
  canonical form the validator accepted. Only plain scalar values (string,
  number, true/false) are copied from a returned document into a follow-up; a
  nested object is never copied (tested).
- **D-20 The larger terms size (500) applies only to `absence`.** A truncated
  entity list would make "who is missing" wrong. Every other query keeps the
  friend's 50. If even 500 is not enough, OpenSearch reports the cut-off
  (`sum_other_doc_count`) and the answer says `complete: false` with a warning.
- **D-21 New limits next to the friend's `Limits`, all env-driven:**
  `OPENSEARCH_ANALYSIS_MAX_TERMS` (500), `OPENSEARCH_ANALYSIS_ANCHOR_LIMIT` (3
  events expanded per before/after question),
  `OPENSEARCH_ANALYSIS_CONTEXT_MAX_MINUTES` (60).
- **D-22 Analysis output goes through the friend's `results.py`.** Each
  analysis produces a `_search`-shaped body, so redaction (a user name in a
  context row is masked), severity assessment, count exactness (`eq`/`gte`),
  partial-shard and timeout flags and the observed range apply to analysis
  rows exactly as to ordinary rows. Added on top (additive only): an
  `execution.analysis` summary block (type, counts, parameters, whether the
  parameters are verified, warnings, how many queries ran and why), and a
  per-row `analysis` annotation on hits (`{"position": "before"}`,
  `{"lag_seconds": 300}`, `{"available": 30, "expected": 32}`). A row without an
  annotation has exactly the old shape.
- **D-23 "Nothing to report" is a success for an analysis that had data.**
  Example: every switch that reported in the last day also reported in the last
  3 minutes, so the answer is "no switch is missing". That is
  `execution_status: "success"` with an empty bucket list and
  `analysis.missing_count: 0`, not `empty_result` (which would read "no data
  for this range"). When the analysis had no input at all (no event found, no
  rows), it stays `empty_result`.
- **D-24 `recurrence` uses exactly the registry interval.** A
  `date_histogram` with another interval is rejected, so "recurring" has one
  definition the owner can change in one place (see F-4).
- **D-25 The Generator is told about analyses only for the selected family**,
  with the live exact-match field paths, the defaults and the limits, and it is
  told to answer `unmapped` when the family does not list the analysis (no
  approximation). Unverified parameters are labelled "UNVERIFIED placeholder --
  say so in the explanation".
- **D-26 Tests.** `tests/os_fixtures.py` adds a fake cluster that replaces only
  the HTTP session, so the friend's real client code runs, serving her
  synthetic fixture mappings. `tests/test_opensearch_analysis.py` has 57 tests
  (registry validation, guard rules, idempotency, all five analyses end to end,
  follow-up re-checks, failures, partial results). `tests/test_opensearch_mentor_questions.py`
  runs the mentor's 35 questions through the whole pipeline with the friend's
  real skill files, plus checks that a `must_not` on severity, a wrong severity
  case and an over-limit analysis parameter are rejected, not fixed. Two
  questions stay `unmapped` because the friend's skill files say so: "unusually
  frequent logins" (no baseline is defined) and "failed logins" (the wording is
  not confirmed).

### Stage 3 — skill files (the friend's content, minimal edits)

- **D-27 Only what the new capability made false or incomplete was edited.**
  Her structure, headings, caveats and wording were kept. Per file:
  - `overview.md`: the sentence "Not supported in this domain yet: absence …,
    before and after …, and cross-family queries. Treat those as `unmapped`"
    became a short table of the five analyses (which family supports each)
    plus the rule "parameters outside their limit are rejected, never
    shortened; the parameters are unverified". Cross-family queries **stay**
    `unmapped`, in her words. Three trigger examples added; one paragraph about
    the `execution.analysis` block added to "Result metadata and limits".
  - `fabric-health.md`: routing triggers gained "switches that stopped sending
    heartbeats" and "recurring faults"; two rows in Query Intent Patterns; her
    "Recurring is undefined" caveat kept and extended with how the analysis
    reads it; her heartbeat caveat's last clause ("no heartbeat-missing logic
    is supported yet") replaced with how `absence` works and that its
    threshold is a placeholder; two JSON examples added under her existing
    "not verified" disclaimer.
  - `controller-log.md`: triggers gained "before and after an event" and "fewer
    switches available than expected"; two Query Intent rows; her "Not
    supported by a plain query" caveat kept as the explanation of why, with
    instructions for `text_ratio_shortfall` and `context_window` in place of
    "treat as `unmapped`"; two JSON examples; "the example is" became "the
    examples are".
  - `ilo-events.md`: the ingestion-lag trigger now names the question form;
    one Query Intent row; the "not supported" caveat split in two: ingest lag
    is now an analysis (sample-only and `Created`-unverified caveats stated),
    while "unusually frequent" access and failed-login detection **stay
    `unmapped`** exactly as she wrote; one JSON example.
  - `SKILL.md`: the three routing rows gained the new phrasing (each phrase in
    one row only); §9 documents the optional `intent` and `analysis` keys,
    restates that `pattern_id`, `raw_metric` and "never write an `index`" are
    unchanged, and shows one complete analysis result.
  - `evals/regression-cases.md`: cases 11e–11k (one per analysis, plus "absence
    is not applied to the controller family" and "unusually frequent / failed
    logins stay unmapped").
- **D-28 The three families do not overlap more than before.** A routing test
  proves every new phrase appears in exactly one family row, next to her
  existing test that the signature words (hmscd, heartbeat, IPMI) stay apart.
  Another test proves each family file documents exactly the analyses the
  registry configures for it, so docs and registry cannot drift.
- **D-29 Every JSON example in the skill files is proven valid.**
  `tests/test_skill_examples.py` extracts each ```json block from the three
  family files, the overview and the concrete OpenSearch example in `SKILL.md`,
  and runs it through the friend's guard against the fixture mappings (10
  examples today). Her own example test (`test_reference_examples_are_accepted_by_the_guard`)
  was extended so it also understands an example that carries `pattern_id` +
  `analysis` (it previously assumed every block was a bare query body). Nothing
  in it was relaxed.
- **D-30 Not verified:** whether the real Gemini model routes the new phrasings
  to the intended rows and writes these shapes. Tests fake the Router and
  Generator. See the smoke test in Stage 4.

### Stage 4 — docs and tools

- **D-31 One live tool, not two.** The friend's
  `scripts/smoke_test_opensearch.py` stays the live check. The reference's
  separate `check_opensearch_registry.py` was folded into it as
  `--check-skills` (offline, no network: registry loads, every family has a
  routing row with data source `opensearch`, its domain file states its
  `pattern_id`, documents exactly the analyses the registry configures, and
  has valid JSON examples that name no index; then it lists every registry fact
  that is still unverified). Two live additions: `--examples` checks every
  skill-file example against the **real** mapping (catches a `.keyword` that
  only exists in the synthetic fixtures), and `--analyses` runs each analysis
  example once and prints only counts and booleans (never a name or value; a
  test plants a secret name and proves it is not printed). Those counts are
  how the placeholder parameters get confirmed (F-4).
- **D-32 `docs/SMOKE_TEST.md` and `docs/NEXT_PHASE.md` were taken from the
  reference and rewritten for the merged code.** The smoke test uses the
  friend's tool and her result fields (`metadata.count.exact`,
  `guard_notes.applied`, `severity_vocabulary_provisional`, ...). NEXT_PHASE
  keeps all 63 open questions word for word, each with an empty answer slot;
  its "hooks" and "where answers land" sections now point at the merged code.
- **D-33 `docs/SKILL_AUTHOR_CONTRACT.md` describes the merged contract**
  (`pattern_id`, `raw_metric`, reject-never-repair, the two optional keys and a
  table of the five analyses with their limits). Its three JSON examples are
  run through the guard by `tests/test_skill_examples.py`.
- **D-34 `currentstatus.md` was written from scratch** as a plain-language
  tour of the whole OpenSearch part, including the friend's severity,
  redaction and results modules. Every JSON example in it is real output of
  the code (fixture mappings, fake network), not invented.
- **D-35 `HANDOFF.md` and `README.md` were corrected, not rewritten.** §7 of
  HANDOFF ("OpenSearch questions currently come back as `unmapped`") was
  replaced with what the frontend now receives (OpenSearch entry fields,
  `metadata`, `severity_assessment`, and the new optional `execution.analysis`
  and per-row `analysis`). Also updated: the API-key/CORS paragraph, the
  `unmapped` row of the status table, "not live yet" notes on buckets/hits,
  the test command and environment note, the compound-question live test, and
  the file manifest. **FLAG F-5:** tell the frontend about the new optional
  fields; no existing field changed meaning.

### Stage 5 — final verification

- **D-36 Verified results.** `ruff check app run_server.py --select F,E9`:
  clean. The result was packaged with `git archive` (exactly the committed
  files, so no `.git`, `__pycache__`, caches or `.env`; 165 entries checked),
  unzipped into a fresh folder outside the repository, and the full suite was
  run there twice:
  - Windows 11, Python 3.12.10: **1384 passed, 1 failed** (only the
    Windows-only permission test, D-03).
  - Linux container (`python:3.12-slim`, Python 3.12.16, dependencies freshly
    installed from `requirements-dev.txt`): **1385 passed, 0 failed**, ruff
    clean.
  Starting point: 1231 tests. Added: 154.
- **D-37 Deliverable:** `work/Grafana-AI-Agent---Skill.md--feat-opensearch-integration-merged.zip`.
  The git history of the five stages stays in the work folder
  (`git log --oneline`) for review or revert.

---

## 3. What could NOT be verified (stated plainly)

- Nothing touched the real OpenSearch, Grafana or Gemini. Every test fakes the
  two AI calls and the network (the real client code still runs).
- The field mappings used by every OpenSearch test are **synthetic**
  (`tests/fixtures/opensearch/README.md`); real `.keyword` sub-fields, types
  and daily drift are unknown until `--save-mappings` is run on the cluster.
- The analysis parameters (F-4), severity vocabularies other than fabric's
  observed `HEALTHY`/`CRITICAL`, and the daily index rotation are unconfirmed.
- Whether the real Router sends the new phrasings to the right family, and
  whether the real Generator writes the `analysis` shapes, is unproven; the
  regression cases 11e–11k in `skills/evals/regression-cases.md` are the
  manual check.
- (Resolved in Stage 5: on Linux all 1385 tests pass.) On this Windows laptop one test
  (`test_reviews.py::test_round_trip_is_deterministic_and_sorted`) fails
  because it checks a Linux file permission.

---

## 4. Open flags (summary)

| Flag | Decision to confirm |
|---|---|
| F-1 | Label sets are cached for 5 minutes (D-04). |
| F-2 | With `API_KEY` unset the service is open (D-09). |
| F-3 | No TLS options for OpenSearch yet (D-14). |
| F-4 | Analysis parameters are placeholders: heartbeat 60 s × 3, the `n/m switches available` wording, "recurring" = 1-hour intervals, `Created` as the iLO event time (D-18, D-24). |
| F-5 | The frontend should learn the new optional fields `intent`, `analysis`, `execution.analysis` and a row's `analysis` note (D-22, D-35). |
| F-6 | One test fails on Windows only (Linux file permissions); confirm the full suite on Linux (D-03). |
