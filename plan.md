# OpenSearch `minimum_should_match` implementation plan

## Recommendation

Use `bool.minimum_should_match` first to answer questions that name several independent log signals. For “at least two of these three,” give each signal one `should` branch and set the minimum to `2`. Put that group under the existing time filter, along with any requested host, service, event code, or other exact constraint. This improves match precision for rows, counts, and aggregations because a document must meet the same threshold in each case.

Add integer `match.minimum_should_match` for unordered searches within one message field. Remove standalone English stop words such as `the` and `is` before applying the threshold. Clean up repeated words so they cannot make an impossible threshold appear valid. Keep phrase matching for quoted or named log phrases and exact filters for structured fields. Preserve the current behavior of ordinary `match` queries with no threshold.

## What OpenSearch supports

`bool.minimum_should_match` counts the immediate `should` branches of that Boolean group. Its default is `0` when that group contains `must` or `filter`, and `1` when it has `should` clauses and neither is present. This matters here because every agent query requires a top-level time range. Putting message alternatives beside that range at the top level without a threshold can return every document in the window. A nested `bool` under `filter` makes the intended threshold explicit and keeps it in filter context. [OpenSearch Boolean query documentation](https://docs.opensearch.org/latest/query-dsl/compound/bool/)

`match.minimum_should_match` applies to optional clauses produced by the message field’s search analyzer with `operator: "or"`. The analyzer can split identifiers, drop words, or combine alternatives into a single clause, so use explicit Boolean branches when the user names the exact signals to count. The linked page demonstrates both a full-text threshold and the effect of changing Boolean thresholds. [Minimum should match](https://docs.opensearch.org/latest/query-dsl/minimum-should-match/) · [Match query](https://docs.opensearch.org/latest/query-dsl/full-text/match/)

| Documented form | Example and meaning | Project rollout |
| --- | --- | --- |
| Non-negative integer | `2` requires two optional clauses. | Start with this form. |
| Negative integer | `-1` allows one optional clause to be missed. | Revisit if users ask for “all but one” over variable lists. |
| Non-negative percentage | `80%` of four rounds down to three required clauses. | Defer until there is a variable-size list use case. |
| Negative percentage | `-20%` of four allows zero misses after rounding down, so all four are required. | Defer for the same reason. |
| Conditional expression | `2<75%` requires all clauses for up to two, then applies `75%`. | Defer. |
| Multiple conditions | `3<-1 5<50%` requires all for up to three, allows one miss for four or five, and requires half for six or more. | Defer. |

The documentation describes bounds for percentage-derived minima. If expression support becomes useful, test its rounding and boundary behavior on the deployed OpenSearch version before accepting strings from the generator. [Minimum should match value formats](https://docs.opensearch.org/latest/query-dsl/minimum-should-match/#valid-values)

## Repository baseline before implementation

| Existing code | What it means for the feature |
| --- | --- |
| [Runtime prompt](app/opensearch_catalog/context.py) and [index registry](app/opensearch_catalog/index_registry.json) | The generator already sees the fields that the registry and runtime mapping both allow. The text fields are `fields.Value` for fabric health, `Body` for controller logs, and `Message` for iLO. The existing 24-hour default and 7-day cap frame these searches. |
| [Query guard](app/opensearch_catalog/guard.py) | `_bool` accepts integers from `0` to `10` and currently does not compare them with the number of `should` branches. `_match` accepts `query` and optional `operator` on `match`, and only `query` on `match_phrase`. It rejects the new `match` option today. |
| [Text variants](app/opensearch_catalog/text_forms.py) and [severity](app/opensearch_catalog/severity.py) | Tense and plural variants already use an inner one-of group. Severity uses one-of groups where the family's rules call for alternatives. An outer threshold should count a complete group as one signal. |
| [Canonicalization](app/opensearch_catalog/canonical.py), [validation](app/validator.py), and [execution](app/executor.py) | The guard normalizes the query, then validation and execution independently recheck the canonical form. Thresholds must survive those checks unchanged. The executor already sends accepted DSL to OpenSearch and exposes the count, buckets, or rows. |

## Phase 1 · Require a minimum number of distinct signals

1. Update the generator prompt and the shared OpenSearch log guidance to show `1` for “any of” and the requested integer for “at least N of.” Represent each named phrase as its own branch. Use an ordinary required filter for a condition that must always hold. Keep omitted and explicit-zero thresholds compatible with their current behavior.
2. In `_Checker._bool`, validate positive minima against the number of immediate `should` branches after canonicalization, within the existing `0`–`10` limit. For a minimum above `1`, reject duplicate canonical branches so the same condition cannot fill two slots. Keep the internal variant groups at `1`, including on a guard recheck. When the requested minimum exceeds the given signals or is ambiguous, have the generator ask for clarification. The guard should reject invalid generated DSL with the reason.
3. Document a worked example using the controller family. The phrases below illustrate user-supplied clues. Actual log wording should come from the question or verified data.

**Example question**

> Show controller logs from the last 24 hours containing at least two of these clues in the same log entry: “link down,” “port disabled,” and “transceiver fault.”

```json
{
  "query": {
    "bool": {
      "filter": [
        {"range": {"@timestamp": {"gte": "now-24h", "lte": "now"}}},
        {
          "bool": {
            "should": [
              {"match_phrase": {"Body": "link down"}},
              {"match_phrase": {"Body": "port disabled"}},
              {"match_phrase": {"Body": "transceiver fault"}}
            ],
            "minimum_should_match": 2
          }
        }
      ]
    }
  }
}
```

A single entry with both “link down” and “port disabled” passes. An entry with only “link down” fails. Multiple forms of one phrase still count as one signal after the guard expands the variants. This filter applies to one document at a time. A time-proximity question across separate events still uses the existing correlation path.

## Phase 2 · Match meaningful words within one message

1. Extend `_Checker._match` to accept an integer `minimum_should_match` from `1` to `10` for `match` on a confirmed text field. Allow explicit `operator: "or"` or its omitted equivalent. Reject `operator: "and"` with the threshold, as well as booleans, floats, strings, and the option on `match_phrase`. Keep the existing field, text-length, depth, and clause limits.
2. In the guard, remove standalone stop words case-insensitively and deduplicate repeated search words. Use a short allowlisted set of common English stop words and preserve negation such as `no` and `not`, as well as technical terms. Reject inputs with fewer remaining words than the requested minimum, including an all-stop-word input. Record the cleanup in `guard_notes`. Have the generator ask for more terms when it can identify this case before it builds a query.
3. Preserve the threshold inside each generated `match` variant and recognize the validated integer when rechecking the group. Keep the surrounding word-form group at one of its variants. Skip variant expansion if it would create repeated tokens that alter the threshold; keep the cleaned search terms and the original threshold.
4. For a controller search for two words from `the link is down at the port`, send `{"match":{"Body":{"query":"link down port","operator":"or","minimum_should_match":2}}}` before any applicable word-form expansion. Keep the configured field analyzer. Test analyzer behavior and real results with stop words, punctuation, and an input that would otherwise collapse to one meaningful term. Use explicit Boolean branches when the user requires distinct named signals. [Analyze API](https://docs.opensearch.org/latest/api-reference/analyze-apis/) · [OpenSearch match implementation](https://github.com/opensearch-project/OpenSearch/blob/main/server/src/main/java/org/opensearch/index/query/MatchQueryBuilder.java#L380-L402)
5. Update `execution.metadata.text_search_note` in [the executor](app/executor.py) to describe the actual search when no rows match. Phrase searches have ordering semantics. A thresholded `match` searches analyzed terms, and an outer Boolean threshold requires a number of signal branches. Keep the existing response shape.

For both phases, verify [result cache](app/opensearch_catalog/query_cache.py) and [query-plan cache](app/opensearch_catalog/plan_cache.py) behavior. The caches already store and revalidate canonical DSL. Use exact-question reuse for newly introduced threshold queries until the semantic matcher can prove that a reworded question asks for the same minimum and signals. Cover both directions so an existing broad cached query cannot answer an explicit threshold question either. Ordinary severity and word-form groups with a minimum of `1` keep their current cache behavior.

## How to measure improvement

- **Correctness.** Test `0`, `1`, `2`, and all branches as appropriate, plus missing clauses, too-large minima, duplicate branches, and invalid types. Include time and host scope, nested variant groups, severity, and exact event code filters. Add a pipeline test that verifies the canonical threshold in the actual client payload and a test that invalid DSL never reaches `_search`. Keep fixtures for rows, exact counts, and buckets. Mocked responses establish what the agent sends. A disposable OpenSearch index with known documents establishes which documents match.
- **Relevance.** On a fixed time window and a labeled set of representative log entries, compare an any-signal baseline, the requested threshold, and an all-signals or phrase query where applicable. Record expected versus returned IDs and matched counts. A two-of-three query should drop one-signal noise while preserving relevant two-signal entries that an all-three query misses. Recheck the existing 30 mentor questions for regressions, especially severity and exact text searches.
- **Cost.** Run repeated comparisons on the same indices, with identical time bounds, projections, size, sort, and `track_total_hits`. Bypass the agent caches when measuring execution cost. Compare median and p95 OpenSearch `took` and end-to-end latency. Fewer matching documents can reduce collection, aggregation, or response work. Query evaluation can also get more expensive, so a smaller result alone cannot establish a speedup. Use `profile: true` in separate direct diagnostics because profiling adds overhead. [OpenSearch Profile API](https://docs.opensearch.org/latest/api-reference/search-apis/profile/)

## Implementation status

Both phases are implemented. The automated tests cover query generation with a mocked model, validation, execution, and cache eligibility. A disposable OpenSearch 2.19.4 index verified that removing stop words and requiring two analyzed terms filters out irrelevant synthetic log entries while preserving consistent rows, counts, and aggregation results. Production relevance and latency measurements remain separate work. [Feature guide](mimuum_match.md)
