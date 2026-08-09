# Backend Handoff — Frontend Integration Notes

This backend takes one natural-language question and returns one JSON
result. Everything else (routing, metric selection, query construction,
validation, live execution) happens internally and is never exposed.

## The one function you call

```python
from agent import load_session_state, get_final_result

await load_session_state()          # once, at process/server startup
result = await get_final_result("What is the GPU temperature?")  # per question
```

- `load_session_state()` scans the `skills/` folder and fetches live label
  data from Prometheus. Run it **once** when your server starts, not per
  request — it's what makes the routing registry and label data available
  to every later call.
- `get_final_result(question: str) -> dict` is async and is the **only**
  entry point you should call per user question. It internally runs all
  four phases (Router → Domain Resolver → Query Generator → Validator →
  Executor) and returns just the final JSON — never the intermediate
  routing/validation output.
- Both are in `agent.py`.

Do not use `interactive_test_harness()` / `python agent.py` directly in the
product — that's the local debug CLI. It prints every phase's intermediate
output to the terminal, which is a debugging aid only.

## Input format

Plain string. No schema, no wrapping object:

```python
await get_final_result("Has the GPU been power throttling?")
```

## Output format

Always a JSON-serializable `dict`. Top level is always one of two shapes:

```json
{ "mode": "single", "status": "...", ...fields for that status... }
```
```json
{ "mode": "multi", "results": [ {"status": "...", ...}, {"status": "...", ...} ], "synthesis": null }
```

`"multi"` happens when a question needs more than one metric (either across
exporters, or two measurements within the same exporter, e.g. "show used
and free memory"). Render each entry in `results` the same way you'd render
a `"single"` response — they use identical per-status shapes.

### Every possible `status` value and what to render

| `status` | Meaning | Key fields to show | Never has |
|---|---|---|---|
| `ok` | Success — query built and run | `query`, `explanation`, `execution.series` (chartable), `execution.execution_status` | — |
| `panic_mode_best_effort` | Same as `ok`, but the question was vague/urgent ("everything is down!") | Same as `ok`, plus `caveat` — show this caveat text near the data | — |
| `ambiguous_metric` | More than one metric could match; needs user input | `clarification` (show this as the follow-up prompt), `candidates` (list of `{name, purpose}` — could render as quick-reply buttons) | `query` |
| `unsupported_metric` | The metric was identified but isn't/can't be queryable right now (either not documented, or explicitly blocked pending verification — see note below) | `requested_measurement`, `explanation` | `query` |
| `unmapped` | No exporter covers this at all | `explanation` | `query` |
| `declined` | Nonsensical input, prompt-injection attempt, or a parameter that has no safe default | `reason`, `explanation`, `clarification` (only present when `reason` is `parameter_requires_clarification` — treat this one like `ambiguous_metric`'s follow-up) | `query` |
| `out_of_scope_action` | User asked to *do* something (restart, silence an alert) rather than retrieve data | `requested_action`, `explanation` | `query` |

Only `ok` and `panic_mode_best_effort` ever carry an `execution` block —
that's your chart/table data. Every other status is a "nothing to show,
here's why" response; render its `explanation` (and `clarification` where
present) as a message, not as a failed chart.

### The `execution` block (only on `ok` / `panic_mode_best_effort`)

```json
{
  "execution_status": "success",
  "resolved_time_range": {"start": "2026-08-09T10:15:00Z", "end": "2026-08-09T10:30:00Z", "step_seconds": 60},
  "series": [
    { "labels": {"instance": "node-1:9100", "gpu": "0"}, "points": [ {"timestamp": "2026-08-09T10:15:00Z", "value": 34.2} ] }
  ],
  "series_count": 1,
  "endpoint": "http://localhost:9090",
  "fetched_at": "2026-08-09T10:30:02Z"
}
```

`execution_status` is a closed enum — `success`, `empty_result`,
`endpoint_unreachable`, `endpoint_error`, `timeout`, `not_executed`. Only
`success` has real data; treat every other value as "the query was valid
but we couldn't get you data right now" and show `error` (present on all
non-`success` values). `series` is always backend-agnostic —
`{labels, points}` — so your charting code never needs to know whether the
data came from Prometheus or (later) OpenSearch.

## Timing / UX notes

- A single question can involve up to 3 sequential Gemini calls (Router →
  Query Generator → Validator) plus one live Prometheus call. Expect
  **several seconds**, not milliseconds — show a loading state, don't
  assume a fast round-trip.
- It's slow-but-never-wrong by design: the system prompt explicitly
  prioritizes correctness over speed, and would rather take longer or
  return `unsupported_metric`/`ambiguous_metric` than fabricate a query or
  a number.
- There is currently no streaming/partial output — you get one complete
  JSON object back per question.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # then fill in your own GEMINI_API_KEY
```

`.env` needs `GEMINI_API_KEY` (required) and optionally `GEMINI_MODEL`,
`PROMETHEUS_URL`, `OPENSEARCH_URL` — see `.env.example` for defaults. The
process will refuse to start without `GEMINI_API_KEY`.

The `skills/` folder must stay alongside the `.py` files — it's read at
runtime, not compiled in. Adding a new exporter later means adding a new
`skills/<name>/` subfolder with its own `_index.md`; no Python code changes
are needed for that (see `main_skill.md` Section 11).

## Known, intentional gaps (not bugs)

- **OpenSearch execution is stubbed.** Any `ok`/`panic_mode_best_effort`
  result with `data_source: "opensearch"` will get
  `execution_status: "not_executed"` — there's no live OpenSearch sub-skill
  or instance registered yet. Currently only `dcgm_exporter` and
  `node_exporter` (both Prometheus) are registered.
- **`synthesis` in `mode: "multi"` responses is always `null`.** It's a
  reserved seam for a future phase that cross-correlates multiple results
  in one answer; nothing sets it yet.

## What changed in this session (for context)

Two real bugs were found and fixed at the source, plus one improvement to
how strictly the safety net catches things:

1. **Routing false-negative** ("What is the FP32 pipeline utilization?" →
   incorrectly `unmapped`). Root cause: Phase 1 was only ever given each
   exporter's one-line `purpose` string and its short exact-phrase
   `trigger_keywords` — the richer `### Trigger Examples` list already
   written in every `_index.md` was never actually read or passed to the
   router, despite `main_skill.md` describing routing as using "purpose
   *and trigger_examples*". Fixed in `registry.py` (now extracts and
   exposes `trigger_examples`) and `agent.py`'s router prompt (now
   instructed to use them). Also added a few precision-pipeline keywords
   (`pipeline utilization`, `FP32`, `FP64`, `FP16`) to `dcgm_exporter`'s
   `_index.md` for a direct exact-phrase match.

2. **A specific Counter's query semantics were genuinely unverified**
   (`DCGM_FI_DEV_POWER_VIOLATION` — its exposed unit isn't confirmed
   against live Prometheus yet), but the Query Generator was constructing
   a query for it anyway, and separately mislabeling it as a Gauge instead
   of a Counter. One quick clarification on this: the "Section 2.8 /
   Step 6" wording describing an unverified-semantics stop-rule wasn't
   actually present anywhere in `main_skill.md` when checked — so rather
   than guess at text that didn't exist, this metric now carries an
   explicit, scoped override in `thermal.md` itself (the mechanism the
   Query Generator's prompt already supports): for this one metric only,
   the correct result is `unsupported_metric`, not a constructed query.
   Every other Counter/Gauge in the project is unaffected — this is not a
   blanket "refuse anything without a canned example" rule, only metrics
   whose semantics are genuinely unverified get blocked. The Validator's
   checklist also gained an explicit rule for this so it's an automatic
   FAIL rather than something the validator has to reason its way to.

`extract_generation_context()` already correctly included Section 3 by the
time this session's code was reviewed — it was already fixed, no change
needed.

## How to validate

**Without any live credentials** (registry/prompt-construction logic only):

```bash
python3 test_offline_fixes.py
```

This checks the actual mechanics of both fixes above — that
`trigger_examples` really do get read out of `_index.md` and appear in the
router's prompt text, that the new keywords are present, that
`extract_generation_context()` carries Section 3, and that `thermal.md`'s
override text and Counter typing are both correct. It does not call Gemini
or Prometheus.

**With live Gemini + Prometheus** (`python agent.py`, using the debug
harness so you can see every phase), two questions specifically worth
re-running:

- `What is the FP32 pipeline utilization?` — should now route
  `SINGLE-DOMAIN -> ['dcgm_exporter']` (previously `UNMAPPED`), resolve to
  the `compute` domain, and return `status: "ok"` referencing
  `DCGM_FI_PROF_PIPE_FP32_ACTIVE` (a Gauge, so no `rate()`/`increase()`).
- `Has the GPU been power throttling?` — should resolve to
  `DCGM_FI_DEV_POWER_VIOLATION`, correctly stated as a `Counter` in the
  explanation, but now return `status: "unsupported_metric"` with **no**
  `query` field (previously `status: "ok"` with a raw, unwrapped
  `sum()` over the counter and a mislabeled Gauge explanation).

General regression check: re-run a couple of the CPU/memory/temperature
questions that were already passing (see `session_log.txt` for examples)
to confirm they're unaffected.
