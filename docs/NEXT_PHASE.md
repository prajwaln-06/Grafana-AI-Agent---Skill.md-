# Next phase: alerting and correlations (OpenSearch)

**Status: not implemented on purpose.** The current phase finished the read
path (question -> safe query or analysis -> result). This file keeps the next
phase easy: it lists every open question (all 63, nothing dropped, each with an
empty answer slot), says where in the merged code each answer lands, and
describes the hooks that already exist.

It was carried over from the second implementation (`reference-mine`) and
rewritten for the merged code on this branch: the contract is the friend's
(`pattern_id`, `raw_metric`, "reject and explain, never change a value the
model wrote"), with the optional `intent` / `analysis` keys added in this
merge.

## How to start the next phase

Give an agent: (1) this repository, (2) this file with the **Answer:** lines
filled in, (3) `currentstatus.md`, (4) `docs/SKILL_AUTHOR_CONTRACT.md`,
(5) `progress.md`. Then say:

> Implement the alerting and correlation features described in
> `docs/NEXT_PHASE.md` using the answers filled in below. Reuse the registry,
> field catalog, guard, analysis runner and results normaliser in
> `app/opensearch_catalog/`. Keep the guard's rule: reject and explain, never
> change a value the model wrote. Do not weaken any guard rule. Add tests first
> for each new analysis or intent, in the style of
> `tests/test_opensearch_analysis.py` and
> `tests/test_opensearch_mentor_questions.py`. Record every decision in
> `progress.md` as D-xx with FLAG where the owner must confirm.

## Hooks that already exist (what the next agent can reuse)

| Need | Already in the code |
|---|---|
| Describe a new kind of question | `ANALYSIS_TYPES` and `_ANALYSIS_SPECS` in `app/opensearch_catalog/registry.py` (the per-family `analyses` block, validated at load); `_ANALYSIS_KEYS` and `_check_analysis` in `guard.py` (parameter rules, code `analysis_invalid`); one `run_*` function per analysis in `analysis.py`. No field names in Python: parameters live in `index_registry.json`. |
| Run several queries for one question | `analysis.py`: each `run_*` receives `runner(dsl, purpose)`. The executor builds that runner; it re-checks every query with `guard.check_entry(..., followup_of=<type>)` and sends it through `opensearch_client`, so the executor stays the only caller of OpenSearch. Build follow-up queries from the already validated query; copy only plain scalar values out of documents. |
| Safe window / time handling | The guard requires exactly one bounded range on the family's time field and rejects (never shortens) a window over `max_window`. `context_window` shows how a follow-up window is built around an event; an alert condition is the same idea (a query over a rolling window). |
| Join keys between sources | Registry **roles** (`entity_switch`, `entity_host`, ...) and the normalisation rule in the `absence` parameters (`normalise_lowercase`, `normalise_strip_suffix_regex`). Extend the same mechanism for xname matching. |
| Ingest-lag awareness | the `ingest_lag` analysis and the `ingest_lag_variable` caveat; an alert window can subtract the measured lag. |
| Raw documents vs distinct faults | the `recurrence` analysis (`metrics.active_intervals`) and the `duplicate_reemission` caveat. |
| Show what ran | every OpenSearch result carries `metadata` (count exactness, partial, aggregation approximation, query window, observed range, severity vocabulary, redaction) and the entry carries `guard_notes` (defaults applied, warnings); analyses add `execution.analysis` (parameters, counts, `queries_executed`, `query_purposes`, warnings). |
| Alert propose -> confirm flow | Prometheus alerting exists (`app/grafana_client.py`, `POST /api/v1/alerts/confirm`, flag `ALERT_RULE_CREATION_ENABLED`). The confirm path re-checks the flag on every confirm and keeps the proposal on a transient Grafana failure. It has **no** OpenSearch datasource yet. |
| Operational visibility | `GET /api/v1/opensearch/status` (registry, analyses per family, limits, field-catalog cache state) and `scripts/smoke_test_opensearch.py --check-skills / --examples / --analyses`. |

## Suggested design (change it if the answers say otherwise)

**Alerts.** Add an alert proposal for OpenSearch that reuses the existing
propose -> confirm flow: the Generator emits a *condition query* that the same
guard validates, the proposal shows the query, window, threshold, evaluation
interval and delay, and nothing is created until the confirm call. Per-family
defaults (window, delay for ingest lag, distinct faults versus raw documents)
go into a new `alerting` object in each registry pattern, validated at load
time like `analyses`. A read-only "what is firing / what fired last night"
question is an ordinary rows or top-N query against the alert-state index once
the mentor says where it lives.

**Correlations.** Add an analysis type `correlation` with parameters such as
`{"left": ..., "right": ..., "window": "5m", "join": "entity|time|both"}`.
Python runs the left query, builds each right query from the left result
(exactly as `context_window` does), joins by registry roles plus the
normalisation rule, ranks by the answer to correlation question 15, and returns
a templated, deterministic narrative that says "coincided with", never
"caused". Metric <-> log joins need the Prometheus label that carries the xname
(question C5) and a second runner for Prometheus. The friend's overview keeps
cross-family questions `unmapped` until this exists.

## Where each answer lands in the code

| Question group | Lands in |
|---|---|
| Alert system / datasource / permissions (B1-B8) | `app/config.py` (new settings, then `python scripts/generate_env_example.py`), `app/grafana_client.py`, registry `alerting` |
| Rule semantics (B9-B14) | registry `alerting` defaults, analysis parameters |
| Delivery and reading (B15-B18) | result shape (`execution.analysis` block), skill file wording |
| Correlation definition / join keys (C1-C8) | registry roles + normalisation rules, a new `correlation` analysis |
| Time and ranking (C9-C16) | `Limits` in `context.py` (windows, env-driven), correlation parameters |
| Output and tests (C17-C20) | `results.py` / `execution.analysis`, `tests/` fixtures (ground-truth incidents) |
| Open confusions (A1-A15) | A3/A10 -> `recurrence` parameters and a new baseline analysis; A5/A9 -> skill files and registry `severity_semantics`; A6 -> replace the synthetic fixtures with `smoke_test_opensearch.py --save-mappings` output; A7 -> `absence` parameters (`heartbeat_interval_seconds`, `dead_after_intervals`, then `"verified": true`); A9 -> `text_ratio_shortfall.regex`; A14 -> `ingest_lag`; others -> config |
| PDF questions (D1-D10) | D1/D2 -> `config.py`, `opensearch_client.py` (TLS options are not ported yet, progress.md F-3); D3/D4 -> registry; D5 -> skill files + new tests; D6 -> registry `time_field`; D7-D9 -> above; D8 -> registry `sensitivity` and `output_projection` |

Answers already partly given by this phase (still confirm them): A7 heartbeat
(registry placeholder 60 s x 3), A9 "n/m switches available" wording (registry
placeholder regex), A3 "recurring" (registry placeholder: separate one-hour
intervals), A14 the ingest-lag question (now answered by `ingest_lag`). All of
them are `"verified": false` until confirmed; see `docs/SMOKE_TEST.md`.

## The questions

Copy each question and write the answer under it. Nothing below has been answered yet.

### A. Still-open confusions from the decisions guide (section 12)

1. **A3 reading** — one template + file per index, or one file with sections? (recommended: template + per-index files)
   - **Answer:** _(to be filled in)_
2. **Simulator fidelity** — does `prom-opensearch` produce these exact 3 indices/mappings, the hourly re-emit, the duplicates, the stale event times? If not, correct code can still look broken (or the reverse).
   - **Answer:** _(to be filled in)_
3. **"Recurring" vs re-emitted** — fabric faults repeat hourly with identical content. Does "recurring" mean *persistent state* or *new occurrences*? Needs a definition (e.g., distinct hours with a doc, or state-change detection).
   - **Answer:** _(to be filled in)_
4. **Other indices** — v1 saw names like `slingshot_switchstate`. Are more `slingshot_*` indices in scope?
   - **Answer:** _(to be filled in)_
5. **Severity value domains** — only `CRITICAL/HEALTHY`, `info/error`, `OK/Informational` observed. Full lists unknown.
   - **Answer:** _(to be filled in)_
6. **Mapping never seen** — we infer `.keyword` from one query. Real mapping may differ between daily indices.
   - **Answer:** _(to be filled in)_
7. **Heartbeat interval and "dead" threshold** — not stated.
   - **Answer:** _(to be filled in)_
8. **Port token mismatch** — `j104p1` (fabric) vs `j105p0 (p1)` (controller Body). Same naming?
   - **Answer:** _(to be filled in)_
9. **Failed-login wording & `fabric-manager` failure wording** — no real examples in the sample.
   - **Answer:** _(to be filled in)_
10. **"Unusual" frequency (iml)** — needs a baseline definition.
   - **Answer:** _(to be filled in)_
11. **What "alerting" is** (OpenSearch Alerting plugin? Grafana alert rules? Alertmanager?) → §13.
   - **Answer:** _(to be filled in)_
12. **Auth/TLS** — deferred; keep configurable.
   - **Answer:** _(to be filled in)_
13. **Prometheus-side labels** for xname — needed for any metric↔log join.
   - **Answer:** _(to be filled in)_
14. **Truncated last question** in the doc — "How long after creation were events ingested…" — ask for the rest.
   - **Answer:** _(to be filled in)_
15. **`_tagsparsefailure` on every doc** — expected permanently, or a Logstash bug worth fixing upstream? (we ignore it either way)
   - **Answer:** _(to be filled in)_

### B. Alert questions (decisions guide section 13)

Group A — *What kind of alerting exists*
1. Which system evaluates alerts today: **OpenSearch Alerting monitors**, **Grafana alert rules**, **Prometheus/Alertmanager**, or a mix? (A4 only said "there is alerting")
   - **Answer:** _(to be filled in)_
2. Are alerts defined on **metrics (PromQL)**, **logs (OpenSearch)**, or both? Which of the three indices (if any) have alerts?
   - **Answer:** _(to be filled in)_
3. Is there an alert **history/state index** I can query (e.g. firing/resolved events), or only the alerting UI/API?
   - **Answer:** _(to be filled in)_

Group B — *What the agent may do*
4. Should the agent only **read/list/explain** alerts, or also **create / edit / silence / delete**? (this decides how far the "read-only" guardrail is narrowed)
   - **Answer:** _(to be filled in)_
5. For create/edit: must every change pass the existing **confirm step** (`/alerts/confirm`), and who is allowed to confirm?
   - **Answer:** _(to be filled in)_
6. Should it be able to create **OpenSearch-based** alert rules? Grafana needs the OpenSearch **datasource** (B4: not configured yet) — who sets it up and what are its name/UID?
   - **Answer:** _(to be filled in)_
7. Which **role/permissions** will the app use for alerting (separate from read access to indices)? Least-privilege expectations?
   - **Answer:** _(to be filled in)_
8. Is there a **safe sandbox** (simulator) where test rules may be created and cleaned up automatically?
   - **Answer:** _(to be filled in)_

Group C — *Rule semantics*
9. What alert conditions are typical for these indices (e.g. "> N CRITICAL fabric events in 5 min", "no heartbeat for 3 intervals", "fewer switches available than expected")? Please give 5 real examples.
   - **Answer:** _(to be filled in)_
10. Which **severity levels** should alerts carry, and how do they map to `CRITICAL / error / OK…` in the logs?
   - **Answer:** _(to be filled in)_
11. Defaults for **evaluation interval**, **"for" duration**, **lookback window**, and **no-data / error state** handling?
   - **Answer:** _(to be filled in)_
12. Because fabric faults are **re-emitted hourly with duplicates**, should alert conditions count raw docs or **distinct faults**?
   - **Answer:** _(to be filled in)_
13. Because ingestion lag is up to ~45 min on iml, should alert windows add a **delay** (evaluate "now − lag")?
   - **Answer:** _(to be filled in)_
14. **Naming/labels/folder** conventions for rules, and required labels (`xname`, `rack`, `severity`, `team`)?
   - **Answer:** _(to be filled in)_

Group D — *Delivery & reading*
15. Notification **channels** and routing — in scope for the agent, or out?
   - **Answer:** _(to be filled in)_
16. Which alert questions should the agent answer: *"what is firing now?"*, *"what fired last night?"*, *"why did X fire?"*, *"how often does X fire?"*
   - **Answer:** _(to be filled in)_
17. How should alert output be shown — table, timeline, link to the alert?
   - **Answer:** _(to be filled in)_
18. Any **audit/log** requirement for agent-made changes?
   - **Answer:** _(to be filled in)_

### C. Correlation questions (decisions guide section 14)

Group A — *Definition*
1. In "congestion ↔ alerts" what exactly is **congestion**: which Prometheus metric(s) / which log events? What is the **alert** — the rule firing, or an OpenSearch/Prom event?
   - **Answer:** _(to be filled in)_
2. Which direction is expected: **alert → find causes in logs**, **metric spike → logs**, or **log event → metrics**?
   - **Answer:** _(to be filled in)_
3. Which kinds of correlation count: **temporal co-occurrence** (same window), **entity match** (same switch/port), or **statistical** (e.g. Pearson)? I propose *temporal + entity*, deterministic.
   - **Answer:** _(to be filled in)_
4. Is it acceptable that "correlation" ≠ "causation", and the answer says "coincided with"?
   - **Answer:** _(to be filled in)_

Group B — *Join keys*
5. What Prometheus label carries the **xname** (`instance`, `node_id`, other)? Same string form as `Resource.host.name` / `fields.Switch`?
   - **Answer:** _(to be filled in)_
6. How do **ports** map across sources (`j104p1` vs `j105p0 (p1)` vs a metric label)?
   - **Answer:** _(to be filled in)_
7. `log_iml` uses BMC hostnames (not xnames): is there a **mapping table** to xnames, or is iml independent?
   - **Answer:** _(to be filled in)_
8. Fabric `Switch` has two spellings (`x1000c3r1` vs `x1000c3r1b0`): confirm that **prefix match** is correct.
   - **Answer:** _(to be filled in)_

Group C — *Time*
9. Standard **correlation window** around an event (±1 / ±5 / ±15 min)? Different for iml because of lag?
   - **Answer:** _(to be filled in)_
10. Which timestamp is authoritative when correlating: **`@timestamp`** (ingest) only? (event-time fields are stale in the simulator.)
   - **Answer:** _(to be filled in)_
11. How should **ingestion lag** be handled (padding the window vs. adjusting with `Created`)?
   - **Answer:** _(to be filled in)_

Group D — *Which pairs matter*
12. Please list the **top correlations** you expect, e.g.: congestion metric ↔ `linkerrors.*` faults · link-down (controllerlog) ↔ CRITICAL (fabric) · "switches available < expected" ↔ heartbeat gaps · alert firing ↔ log burst.
   - **Answer:** _(to be filled in)_
13. Are **log ↔ log** correlations (all inside OpenSearch, e.g. controllerlog ↔ fabric health by xname) in scope, or only **metric/alert ↔ log**?
   - **Answer:** _(to be filled in)_
14. For "before and after an event" questions: how many events/minutes, and sorted ascending?
   - **Answer:** _(to be filled in)_

Group E — *Behaviour & output*
15. If several candidate events match, **rank** by what — closeness in time, severity, count?
   - **Answer:** _(to be filled in)_
16. If one side is **empty** (metrics but no logs, or the reverse), return partial results with a note?
   - **Answer:** _(to be filled in)_
17. Must the narrative be **deterministic** (templated from results) or may an LLM write it?
   - **Answer:** _(to be filled in)_
18. Expected output: side-by-side table, merged timeline, or chart with markers?
   - **Answer:** _(to be filled in)_
19. Do you have **ground-truth incidents** (known fault ↔ known symptom) so I can build regression tests? Will the **simulator inject** such correlated events?
   - **Answer:** _(to be filled in)_
20. Performance: is it acceptable to run **staged** queries (Prometheus first, then OpenSearch around the peak), accepting extra latency?
   - **Answer:** _(to be filled in)_

### D. Mentor questions from the project-status PDF (section 7)

1. **Access:** the correct REST endpoint, auth, TLS, and whether we query OpenSearch directly or only through Grafana's datasource.
   - **Answer:** _(to be filled in)_
2. **MCP limits:** the installed Grafana MCP only supports Lucene for OpenSearch, with no DSL aggregations. Can we upgrade it or bypass it for aggregations?
   - **Answer:** _(to be filled in)_
3. **Index inventory:** the real index names and rotation (daily or ILM), and whether `syslog-*`, `consolelog-*`, `heartbeat` are accurate.
   - **Answer:** _(to be filled in)_
4. **Mappings:** the real schema, for example whether Severity and Resource.* are keyword or text, plus sample documents.
   - **Answer:** _(to be filled in)_
5. **Questions to support:** the top 10 log or event questions users actually ask, and which are counts, trends, top-N, or raw search.
   - **Answer:** _(to be filled in)_
6. **Time semantics:** timezone and which timestamp field is authoritative.
   - **Answer:** _(to be filled in)_
7. **Alert scope:** whether OpenSearch-based alerting is in scope or only Prometheus.
   - **Answer:** _(to be filled in)_
8. **Limits and retention:** data volume and query-cost limits, and any restricted fields or PII that must not be shown.
   - **Answer:** _(to be filled in)_
9. **Correlation:** whether metric-to-log correlation (a compound question over both datasources) is a goal.
   - **Answer:** _(to be filled in)_
10. **Content ownership:** who authors and verifies the OpenSearch skill content.
   - **Answer:** _(to be filled in)_

