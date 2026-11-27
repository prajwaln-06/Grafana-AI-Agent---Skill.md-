# Validation workflow audit (input to `scripts/run_full_validation.py`)

Audited on branch `feat/seven-stage-validation-automation`, based on
`fix/gemini-generator-thinking-budget-2811` (`1ebac73`). Everything below was
confirmed by reading the code; nothing was run against a live OpenSearch,
Gemini, Prometheus or Grafana, because none is reachable from the audit
environment.

## 1. What each existing script really does

| Script | Network | LLM | Exit codes | Notes |
|---|---|---|---|---|
| `smoke_test_opensearch.py --check-skills` | none | none | 0 consistent, 1 problems | Loads settings (so `GEMINI_API_KEY` must be non-empty, any value). Also lists "still unverified" registry facts as informational lines. |
| `smoke_test_opensearch.py` (no flag) | OpenSearch `GET _mapping` | none | 0 all families usable, 1 otherwise | Base check; also runs under every flag below. |
| `... --queries --rows` | OpenSearch (`size:0` count, then up to 3 rows, 24 h window) | none | same | Guard -> executor path. Prints field names and counts only, never values. A projected field that never appears is `NOT SEEN` (informational, exit stays 0). Zero rows is a failure. |
| `... --save-mappings DIR` | OpenSearch `GET _mapping` | none | same | Writes `mapping_<pattern_id>.json` (names and types only). **Exit code does not reflect a failed snapshot on its own**: an unreachable cluster makes the family unusable (exit 1), but the file list is the only proof that snapshots exist. |
| `... --examples --analyses` | OpenSearch | none | same | Examples go through the guard against the live mapping; analysis examples execute once and print counts only. |
| `smoke_test_gemini.py` | Gemini | **yes, one trivial call** | 0 / non-zero | The only real Gemini call in the repo's scripts. Reads `GEMINI_API_KEY` / `GEMINI_MODEL` from the environment. |
| `smoke_test_prometheus.py` | Prometheus | none | 0 / non-zero | Direct client calls. Not needed by any OpenSearch stage. |
| `smoke_test_grafana.py` | Grafana (GET only) | none | 0 / non-zero | Only relevant when `ALERT_RULE_CREATION_ENABLED=true`. |
| `manual_test_report.py FILE [--strict]` | none | none | 0; 1 only with `--strict` and any semantic failure | Grades a **recorded** run (`results[]` with `http_status`, `application_status`, ...) on three axes: `http_ok`, `status_ok`, `semantic_ok`. Never contacts the agent. Empty results are flagged `needs_review`, not passed. |
| `run_opensearch_30_suite.py` | OpenSearch (direct) | **hybrid, see below** | 0 only if all match and `semantic_ok == total` | See section 2. |
| `qa_helpers.ps1` | agent, OpenSearch, Prometheus, simulator | via agent | n/a | Interactive PowerShell helpers (`Ask-Agent`). Not a batch runner. |

`run_server.py` exposes `GET /readyz` (public), `POST /api/v1/query`
(`{"question": ...}` -> `{"result": ..., "session_id": ...}`) and
`GET /api/v1/opensearch/status`. When `API_KEY` is set every route except
`/readyz` and `/health` needs `X-API-Key`.

## 2. `run_opensearch_30_suite.py` is not a live Gemini evaluation

Reading `_generate_agent_contract` and `main`:

1. **No HTTP request is ever sent to the agent.** It calls
   `pipeline.run_pipeline(...)` and `executor.execute_contract(...)` in-process.
   Yet every result record hard-codes `"http_status": 200` and
   `"request": {..., "sent": true}` for `http://localhost:8000/api/v1/query`.
   An HTTP-200 grade from this suite therefore carries no information.
2. **Live Gemini is used only when a non-placeholder `GEMINI_API_KEY` is set**
   (`use_live_llm`). If the live pipeline returns `status == "error"`, the code
   **silently falls back** to the canned `router` / `generator` payloads stored
   in `SUITE_CASES` and runs the pipeline against a patched `call_llm_json`.
   A "PASS" can therefore come from the hand-written query for that case, with
   nothing in the output saying so.
3. With no key (or a key starting with `test`) every case runs on the canned
   payloads. That is a deterministic regression check of guard, canonicaliser,
   executor and result shaping against live OpenSearch data, not an LLM test.
4. It rewrites any `localhost:9200` URL to `http://127.0.0.1:9250` (an
   environment-specific simulator port) and writes three JSON files **into the
   repository root**, overwriting the tracked copies
   (`opensearch_question_suite_fixed.json`, `test_output_all_fixed.json`,
   `opensearch_direct_vs_agent_comparison.json`).
5. `expected_behavior` is prose. Pass/fail is "agent query results equal a
   direct OpenSearch query results" (`_compare_agent_and_direct`), which checks
   agreement between two executions of nearly the same DSL, not whether the
   question was answered correctly.

**Conclusion.** No live Gemini question evaluator that talks to
`POST /api/v1/query` exists in this repository. The recorded
`test_output_all*.json` files and `MENTOR_DEMO_30_QUESTIONS_MANUAL.md` come from
the 30-suite or from manual PowerShell sessions. The only repo-side grader is
`manual_test_report.py`, which accepts any file in the `test_output_all` shape.

## 3. Decisions that follow

* Stage 5 (live question evaluation) is **BLOCKED** unless the operator
  supplies a live runner through the integration point
  (`--live-eval-cmd` / `VALIDATION_LIVE_EVAL_CMD`). The runner must write a
  `test_output_all`-shaped JSON file; grading reuses
  `manual_test_report.grade_run`. Nothing is substituted when it is missing.
* The 30-suite is **not wired in**. It is neither a live evaluation nor side-effect free (it overwrites
  tracked repo-root files), so the runner leaves it to be run by hand
  (`python scripts/run_opensearch_30_suite.py`) and says so when stage 5 is blocked.
* Stage 3 verifies the snapshot files exist rather than trusting the exit code.
* Gemini is called only by Stage 0 (via `smoke_test_gemini.py`), and only when
  Stage 5 is selected and a live runner is configured.
* Prometheus and Grafana are probed for information but only gate anything when
  the operator asks (`--require-prometheus`) or alert creation is enabled
  (Grafana). No OpenSearch stage uses them.
