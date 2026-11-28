# Master validation runner

`scripts/run_full_validation.py` runs the seven-stage (0-6) OpenSearch validation workflow in order, captures
every command's output, and writes one consolidated report. It orchestrates the existing scripts as
subprocesses; it does not reimplement any validation logic. What each existing script really does, and why
`run_opensearch_30_suite.py` is not a live Gemini evaluation, is in [VALIDATION_AUDIT.md](VALIDATION_AUDIT.md).
The manual procedure it automates is [SMOKE_TEST.md](SMOKE_TEST.md).

## The stages

| # | Stage | What runs | Live services |
|---|---|---|---|
| 0 | Preparation | In-process checks: Python, dependencies, scripts, settings; HTTP probes of OpenSearch, the agent server (`/readyz`), Prometheus, Grafana; `smoke_test_gemini.py` | OpenSearch always (stages 0, 2-4). Agent server and **one real Gemini call** only when a live runner is configured and stage 5 is selected. |
| 1 | Offline consistency | `smoke_test_opensearch.py --check-skills` | none |
| 2 | Live mapping verification | `smoke_test_opensearch.py --queries --rows` | OpenSearch, read-only. No Gemini. |
| 3 | Mapping snapshots | `smoke_test_opensearch.py --save-mappings <run dir>/mappings` | OpenSearch, read-only. No Gemini. |
| 4 | Examples and analyses | `smoke_test_opensearch.py --examples --analyses` | OpenSearch, read-only. No Gemini. |
| 5 | Live question evaluation | A live runner (`--live-eval-cmd`), e.g. `scripts/run_live_agent_30_suite.py`; results graded with `manual_test_report.grade` | Agent server, Gemini, OpenSearch |
| 6 | Findings and report | `manual_test_report.py <stage 5 results> --strict`, then `report.json` and `report.md` | none |

Only stages 0 (when stage 5 is on) and 5 call Gemini. Stages 1 to 4 never do, so a run without a live runner
makes **no** Gemini call and says so.

### Stage 5 is an integration point, not a built-in test

The master runner does not contain a live evaluator; it runs whatever `--live-eval-cmd` names. Without one,
stage 5 is reported **BLOCKED**, and the overall result is INCONCLUSIVE (exit 3). Nothing is substituted: not
`run_opensearch_30_suite.py` (in-process, canned payloads, fixed HTTP 200), not canned payloads.

The repository's runner for this slot is `scripts/run_live_agent_30_suite.py`. It sends the 30 mentor questions
(ids, text and expected behavior imported from `run_opensearch_30_suite.py`; its canned router/generator payloads
are never used) as real `POST /api/v1/query` requests with body `{"question": ...}` to a running agent.

```bash
# on its own (agent already running; stage 5 needs the real GEMINI_API_KEY and a reachable OpenSearch behind it)
python scripts/run_live_agent_30_suite.py --out /path/to/results.json --agent-url http://localhost:8000
python scripts/manual_test_report.py /path/to/results.json --strict     # grade it

# through the master runner
python scripts/run_full_validation.py \
    --live-eval-cmd "python scripts/run_live_agent_30_suite.py --out {results_file} --agent-url {agent_url}"
# or:  export VALIDATION_LIVE_EVAL_CMD="..."   (same placeholders)
```

| Option | Meaning |
|---|---|
| `--out FILE` | Where to write results (default `$VALIDATION_RESULTS_FILE`, which the master runner sets). |
| `--agent-url URL` | Agent base URL (default `$VALIDATION_AGENT_URL`, `$AGENT_URL`, else `http://localhost:8000`). |
| `--timeout SEC` | Per-request read timeout (default 300). |
| `--ids FH-01,CL-03` | Run only some questions. The file records that fewer than 30 were run; do not treat it as the full suite. |
| `--max-connect-failures N` | Stop sending after N consecutive connection failures (default 3; 0 = never). |

If the agent has `API_KEY` set, export the same `API_KEY` for the runner; it is sent as `X-API-Key` and never
written to the output.

What it records, per question, from the real exchange: `http_status` (the status the server returned, or `null`
when no response arrived; never defaulted to 200), `application_status` and `execution_status` (from
`response.result`, `multi:` prefixed for multi-entry answers), `elapsed_seconds`, `generated_opensearch_dsl`,
`opensearch_execution_results`, `raw_response` and `raw_response_text`, plus `request_sent`, `request_error` and
`response_parse_error`. Failure handling: a connection error or connect timeout is `request_sent: false`; a read
timeout is `request_sent: true` with no status; a non-JSON body, or a 200 body without `result`, is a parse error;
non-200 responses are kept as returned. There are no retries. The file is rewritten after every question, and if
the agent disappears the unsent questions are still listed (`request_sent: false`) with `aborted_reason` set.

Exit code: 0 when every question got an HTTP response with a parseable body (a 500 is a response; grading fails it),
1 when any question had no usable response or the run aborted, 2 on usage errors, 130 on Ctrl-C.

Supply a runner with `--live-eval-cmd` or `VALIDATION_LIVE_EVAL_CMD`. Contract:

* It is a command line. These placeholders are replaced before it runs: `{results_file}` (where it must write),
  `{agent_url}`, `{output_dir}`, `{python}`. The same values are in the environment as
  `VALIDATION_RESULTS_FILE`, `VALIDATION_AGENT_URL`, `VALIDATION_OUTPUT_DIR`. No secret is passed.
* It must write JSON shaped like `test_output_all.json`: a `results` list whose records carry `id`, `question`,
  `http_status`, `application_status`, `execution_status`, and, where known, `elapsed_seconds`,
  `generated_opensearch_dsl`, `opensearch_execution_results`, `raw_response` (the shape
  `scripts/manual_test_report.py` already grades).
* Exit code 0 means it finished. A non-zero exit or a timeout fails stage 5 even if the partial results grade well.

Grading reuses `manual_test_report.grade`: **HTTP 200 alone is never a pass**. A question passes only if its
application status and its semantics match the expectation (`EXPECTATIONS` in that script; unlisted questions
must be `ok` and executed). A genuinely empty result is **INCONCLUSIVE**, not a pass. Each question's report row
has its id, text, HTTP status, application status, execution time, generated DSL, expected versus actual and
any error. Full evidence stays in `live_eval/results.json`.

The master runner cannot verify that a runner really sent HTTP requests to a live agent; that is the contract.
The runner's results file is rewritten in place with secrets masked.

## Usage

```bash
python scripts/run_full_validation.py                          # all stages
python scripts/run_full_validation.py --dry-run                # show the plan; run and write nothing
python scripts/run_full_validation.py --stages 1               # stage 1 (stage 0 is added when needed)
python scripts/run_full_validation.py --stages 1-4             # a range
python scripts/run_full_validation.py --stages 1,3,5-6         # a list
python scripts/run_full_validation.py --output-dir D:/runs     # base directory for run folders
python scripts/run_full_validation.py --agent-url http://localhost:8000 \
    --live-eval-cmd "python my_live_runner.py --out {results_file} --agent-url {agent_url}"
python scripts/run_full_validation.py --start-server --live-eval-cmd "..."   # start run_server.py if nothing answers
```

| Option | Meaning |
|---|---|
| `--stages SPEC` | `all` (default), `N`, `N-M`, or a comma list. Stage 0 is added automatically when a selected stage needs it. |
| `--dry-run` | Prints every command (secrets masked) in stage order. Executes nothing, probes nothing, creates no directory. |
| `--output-dir DIR` | Base directory (default `artifacts/validation_runs`). Each run gets its own `DIR/<UTC timestamp>/`; an existing folder is never reused (`-1`, `-2` suffix). |
| `--agent-url URL` | Agent server. Default `$AGENT_URL`, else `http://localhost:<API_PORT>` from the app settings. |
| `--start-server` | If nothing answers at the agent URL, start `python run_server.py`, and stop it at the end. A server that was already running is **always reused and never stopped**. Without this flag no server is ever started. |
| `--live-eval-cmd CMD` | Stage 5 integration point (default `$VALIDATION_LIVE_EVAL_CMD`). |
| `--live-eval-timeout SEC` | Stage 5 timeout (default 3600). |
| `--timeout SEC` | Per-command timeout, stages 1-4 (default 600). A timeout fails the stage. |
| `--probe-timeout SEC` | HTTP probe timeout (default 5). |
| `--require-prometheus` | An unreachable Prometheus then fails stage 0 (otherwise informational). |

## Configuration

Nothing is hardcoded. URLs, ports and credentials come from `app/config.py` and therefore from the environment
or `.env` (see `.env.example`).

* `GEMINI_API_KEY`: required for settings to load. Stages 1 to 4 accept any non-empty value; stage 5 needs the
  real key.
* `OPENSEARCH_URL` (REST port 9200, not 9600) and, if the cluster needs them, `OPENSEARCH_AUTH_USERNAME` /
  `OPENSEARCH_AUTH_PASSWORD`.
* `PROMETHEUS_URL`: probed for information only. `GRAFANA_URL`: probed only when `ALERT_RULE_CREATION_ENABLED=true`.
* `API_PORT` / `AGENT_URL`: where the agent server is expected.
* `VALIDATION_LIVE_EVAL_CMD`: the stage 5 runner.

Prerequisites: Python 3.10+ (the Dockerfile uses 3.12) and `pip install -r requirements.txt`.

## Statuses and exit codes

| Status | Meaning |
|---|---|
| PASS | The stage ran and succeeded. |
| FAIL | It ran and failed, timed out, produced no required output, or hit a runner error. |
| BLOCKED | Not run, because a prerequisite from stage 0 is unmet (the reason is recorded). |
| SKIPPED | Not selected, or not applicable. Neutral. |
| INCONCLUSIVE | It ran but cannot be called pass or fail (for example an empty live result needing human review). |

Dependencies: stage 1 needs only valid settings. Stages 2-4 need settings and a reachable OpenSearch. Stage 5
needs those plus the agent server, a working Gemini call and a configured runner. Stages 2, 3 and 4 do not depend
on each other's verdict, so a failure in one never stops the others. Stage 6 always runs.

| Exit | Meaning |
|---|---|
| 0 | Every selected stage PASSED. |
| 1 | At least one FAIL. |
| 2 | Usage error (for example `--stages 9`). |
| 3 | No FAIL, but something was BLOCKED or INCONCLUSIVE: the run proved less than it was asked to. |
| 130 | Interrupted; the report is still written. |

A partial run (`--stages ...`) can exit 0 and is labelled PARTIAL in the report. Only a full run with all seven
stages PASS means the whole workflow passed.

## Output

```
artifacts/validation_runs/20261128T010203Z/
  report.md            human-readable consolidated report
  report.json          machine-readable (run id, git branch/commit, configuration without secrets,
                       per-stage status/duration/commands/counts/reasons, per-question rows, overall status)
  stages/              stdout and stderr of every command, plus agent_server.log if the runner started one
  mappings/            stage 3 snapshots (field names and types only; review before committing anywhere)
  live_eval/results.json   stage 5 raw results from your runner (secrets masked)
```

`artifacts/` is git-ignored. Do not commit run output. Every string the runner prints or writes passes through a
redactor (known secret values from the environment and settings, `AIza...` keys, bearer tokens, `password=`
style pairs, URL credentials). Reports hold configuration as booleans (`gemini_api_key_set`), never values.

## Known limits

* Counts for stages 1-4 are parsed from the smoke script's text output; they are `n/a` if it printed none.
* A timeout kills only the direct child process.
* A live runner must be configured (`--live-eval-cmd`); until then a full run ends INCONCLUSIVE (exit 3) by design.
* `run_live_agent_30_suite.py` has been unit-tested with mocked HTTP and exercised against a local stub server only. A Stage 5 verdict needs a run against the real agent with Gemini and OpenSearch.
* The runner does not run the unit tests; run `python -m pytest tests -q` separately.
