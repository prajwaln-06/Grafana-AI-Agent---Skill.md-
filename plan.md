# OpenSearch minimum match and latency implementation

## Guard and query shape

The generator writes one `match` for unordered text and includes an integer minimum when the user asks for one, including “any” and “all.” For named signals it writes a nested `bool.should` with one branch per phrase or exact condition. The guard requires at least one branch whenever `should` is present and the minimum is omitted. It validates explicit minima against the number of branches and compares canonical branches for duplicates when the minimum exceeds one.

For an unordered `match` on a confirmed text role, the guard removes standalone common words and negators, case-folds and deduplicates keywords, and handles clear adjacent exclusions with `must_not`. With the default ratio, 2, 3, 4, 5, 6, 7, and 8 keywords require 2, 2, 3, 3, 4, 5, and 6 matches respectively. A single keyword receives no default minimum. The computed minimum is bounded by 2, 10, and the keyword count. Empty positive input or an impossible explicit minimum produces a clarification.

For minima of at least two, the guard normally compiles word forms into a single OR match per keyword and technical identifiers into phrase branches. For example, this group requires two of three signals.

```json
{
  "bool": {
    "should": [
      {"match": {"Body": {"query": "fail failed failing fails", "operator": "or"}}},
      {"match_phrase": {"Body": "eth0/1"}},
      {"match_phrase": {"Body": "link-down"}}
    ],
    "minimum_should_match": 2
  }
}
```

The compiler reserves the rest of the query's clauses before expanding. The guard allows at most 30 clauses, five levels of Boolean nesting, and 100 characters per text value. It uses a cleaned native match with the same integer minimum and warns when the per-signal form exceeds a limit. The native form applies the threshold to analyzed terms. Explicit minimum one uses that native form. Ordered phrase searches can still match word-form variants. Named conditions for severity retain their registry-defined branches.

The runtime supplies the intersection of permitted registry fields and discovered operations. The guard canonicalizes the query and records applied changes and warnings in `guard_notes`. The validator and executor independently re-run the full guard and compare the query with the canonical form. Numeric strings, booleans, floats, negatives, percentages, and expressions cannot enter the DSL as minima.

For relevance ranking, the guard moves required positive text to scoring context for rows without a supplied sort and uses `[{"_score":"desc"},{"@timestamp":"desc"}]` for the shipped families. The generator cannot specify `_score`. Explicit permitted sorts take precedence. Count-only and aggregation queries keep required text in filter context. Execution reports the keywords, threshold provenance, rank status, and ordering when applied.

[Guard](app/opensearch_catalog/guard.py) · [Text forms](app/opensearch_catalog/text_forms.py) · [Executor](app/executor.py)

## Caches and routing

The answer cache defaults to a 60-second TTL, with shorter lifetimes for narrow relative windows. The OpenSearch plan cache defaults on and re-executes valid plans against the current window. The guard's provenance lets equivalent questions reuse default-threshold entries. An explicit threshold is eligible for exact wording only. Configuration fingerprints include the threshold, ranking, and OpenSearch routing settings.

The agent schedules cache writes before yielding the response. The reported timing covers work already completed. Backlogged answer writes cannot extend expiry, and an admin reload invalidates earlier pending writes. Field discovery serves a prior mapping while it is inside the stale TTL and refreshes once per pattern in the background. Cold and expired requests need a usable discovered view. Startup and admin reload schedule prewarm for all enabled patterns.

The optional Router bypass accepts a simple read verb plus one exact registered family alias or pattern ID, an optional recent time window, and an optional quoted text clause. The request must have no clarification context, the family must be enabled and unique, and a working Prometheus catalog must return zero candidates. All other requests use the Router. OpenSearch-only generation uses the compact prompt and does not enter the Prometheus candidate-expansion retry chain.

[Result and plan caches](app/opensearch_catalog/runtime.py) · [Registry](app/opensearch_catalog/index_registry.json) · [Pipeline](app/pipeline.py)

## Settings

| Setting | Default | Effect |
| --- | --- | --- |
| `OPENSEARCH_TEXT_DEFAULT_MSM_ENABLED` | `true` | Applies the default minimum to eligible unordered text |
| `OPENSEARCH_TEXT_DEFAULT_MSM_RATIO` | `0.75` | Fraction rounded down for the default minimum |
| `OPENSEARCH_RANK_TEXT_MATCHES` | `true` | Ranks eligible text rows before applying newest-first ties |
| `OPENSEARCH_QUERY_PLAN_CACHE_ENABLED` | `true` | Reuses validated OpenSearch plans |
| `OPENSEARCH_RESULT_CACHE_TTL_SECONDS` | `60` | Default completed-answer lifetime |
| `OPENSEARCH_ROUTER_FASTPATH` | `false` | Enables the conservative registry-only routing decision |
| `GEMINI_HTTP_TIMEOUT_SECONDS` | `60` | Default timeout for each Gemini HTTP request |
| `GEMINI_MAX_ATTEMPTS` | `3` | Maximum application attempts for transient and JSON failures |
| `GEMINI_ROUTER_THINKING_BUDGET` | Unset | Optional setting for the shared Router |
| `GEMINI_GENERATOR_THINKING_BUDGET` | `0` | Thinking budget for OpenSearch-only read generation |
| `GEMINI_GENERATOR_MAX_OUTPUT_TOKENS` | `8192` | Output budget for OpenSearch-only read generation |
| `GEMINI_GENERATOR_TIMEOUT_SECONDS` | Unset | Optional HTTP timeout override for OpenSearch-only read generation |

Gemini clients are reused by credential and mode. Transient requests use jittered backoff and honor `Retry-After` on HTTP 429. Invalid JSON gets one immediate retry within the attempt budget. AI Studio model discovery remembers which fallback succeeded and skips previously confirmed 404s. OpenSearch uses a keep-alive pool and trims response fields for counts and projected rows while preserving aggregation, total, shard, timeout, error, and timing data. [Google Gen AI SDK](https://googleapis.github.io/python-genai/) · [OpenSearch REST parameters](https://docs.opensearch.org/latest/api-reference/common-parameters/)

## Verification

The fixture suite checks guard idempotency, closed grammar, minimum values, scoring context, cache provenance, concurrent mapping refresh, and response filtering. The operator timing log and `execution.metadata.timings` report milliseconds for the phases actually run. The field `opensearch_took_ms` is reported by the cluster. Guard time can overlap validation time, and a background cache write has no response-time duration.

### Prompt size

Gemini's token counter measured the OpenSearch-only controller fixture. The total counts the complete input text. The individual counts measure sections separately.

| Section | C0 | Release |
| --- | ---: | ---: |
| System instruction | 976 | 503 |
| SKILL 5 operating principles | 1,273 | — |
| SKILL 6 construction | 1,741 | — |
| SKILL 7 refusal conditions | 644 | — |
| SKILL 8 parameter interpretation | 972 | — |
| SKILL 9 output contract | 2,451 | — |
| Matched reference | 4,339 | 4,339 |
| Parent overview | 3,476 | — |
| Datasource fundamentals | 1,389 | — |
| Live OpenSearch context | 2,311 | 2,360 |
| Panic and action flags | 23 | 23 |
| Question | 11 | 11 |
| **Complete input** | **19,613** | **7,235** |

### Stage checkpoints

The fixture uses 30 warm-mapping cache misses per checkpoint and a fake model and cluster. The live checkpoint uses the same three questions from the full comparison with the query caches disabled. The live median includes every attempt and the last column gives OpenSearch executions. Treat these short checkpoint runs as diagnostics because provider response time and host load varied.

| Checkpoint | Fixture median total, ms | Live probe median total, s | Live searches |
| --- | ---: | ---: | ---: |
| C0 instrumentation | 5.174 | 11.235 | 3/3 |
| C1.1 transport | 71.233 | 8.012 | 3/3 |
| C1.2 call policy | 46.728 | 3.675 | 3/3 |
| C2 prompt reduction | 7.122 | 3.676 | 2/3 |
| C3 background writes | 2.471 | 4.075 | 3/3 |
| C4 field refresh | 3.037 | 6.481 | 2/3 |
| C5 OpenSearch transport | 2.944 | 7.015 | 2/3 |
| C6 Router bypass | 6.476 | 6.024 | 2/3 |
| C7 retry scope | 4.992 | 2.930 | 2/3 |
| C8 dead code cleanup | 3.058 | 15.227 | 3/3 |

The C2 through C7 probes caught a malformed single-result envelope on the fabric-count question in some runs. The final prompt includes a complete result example and explicit JSON syntax instructions. With an empty Prometheus catalog fixture, C6 skipped the Router on 30 of 30 eligible reads. The configured live catalog returned competing candidates for the tested aliases and kept their ordinary route.

### Before and after

The same 12 questions ran against live Gemini and the configured OpenSearch cluster before C1 and after all changes. Both query caches and the optional Router bypass were disabled. All 12 requests reached OpenSearch in both runs. The fixture columns use 30 warm-mapping misses per run. Values are median milliseconds.

| Stage | Fixture C0 | Fixture release | Live C0 | Live release |
| --- | ---: | ---: | ---: | ---: |
| Router | 0.587 | 0.590 | 5,139.430 | 1,105.496 |
| Context | 0.270 | 0.351 | 0.885 | 0.883 |
| Generator | 0.858 | 0.207 | 5,950.524 | 1,559.030 |
| Validate | 0.189 | 0.205 | 0.613 | 0.409 |
| Guard | 0.532 | 0.643 | 1.602 | 1.193 |
| Search client | 0.042 | 0.057 | 44.552 | 19.974 |
| OpenSearch `took` | 3.000 | 3.000 | 32.500 | 9.000 |
| Normalize | 0.051 | 0.060 | 0.086 | 0.120 |
| Cache store | 1.885 | — | 0.847 | — |
| **Total** | **5.174** | **3.724** | **11,361.507** | **2,832.959** |

OpenSearch `took` is synthetic in the fixture. The other fixture paths had median totals of 6.212 to 4.062 ms on a cold field catalog, 4.322 to 3.923 ms on a plan hit, and 1.296 to 2.665 ms on a result hit. The live suite's sample p95 by nearest rank was 20.299 s before and 8.061 s after. These sequential runs measure the configured endpoints under the test workload. Production concurrency and sustained tail latency are unmeasured.

[Feature guide](mimuum_match.md) · [Environment settings](.env.example)
