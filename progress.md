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

---

## 3. Open flags (summary)

| Flag | Decision to confirm |
|---|---|
| F-1 | Label sets are cached for 5 minutes (D-04). |
| F-2 | With `API_KEY` unset the service is open (D-09). |
| F-3 | No TLS options for OpenSearch yet (D-14). |
