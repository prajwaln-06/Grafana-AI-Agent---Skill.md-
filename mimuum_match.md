# More precise OpenSearch log search

Ask in your own words. For an unordered search such as “Show controller logs from the last 24 hours about fan off rpm,” the guard requires at least two of `fan`, `off`, and `rpm` to match the same document. Time, host, service, and severity constraints still apply.

## Choose how many clues should match

- Leave the minimum unspecified to use the default. The guard removes common standalone words, case-folds the remaining keywords, and counts repetitions once.
- Say “any of these words” or “either” to request an explicit minimum of `1`. Say “at least 3” or “all of these words” to require more. A query with an explicit `and` operator retains that operator.
- Quote a phrase when word order matters. To require two of several named phrases, name each phrase separately. One phrase and its allowed word forms count as one signal.
- Use a clear exclusion such as “exclude messages containing reboot.” For an unordered threshold, the guard can also turn an adjacent negator such as `without reboot` into a `must_not` clause. Negators do not count toward the minimum. If the scope cannot be determined safely, the guard removes the negator and reports a warning.

The default applies when `OPENSEARCH_TEXT_DEFAULT_MSM_ENABLED` is true and the generator supplies neither an explicit minimum nor `operator: "and"`. With the default ratio of `0.75`, the minimum for two or more meaningful keywords is the count times the ratio, rounded down and bounded by 2, 10, and the keyword count.

| Distinct meaningful keywords | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Default minimum | Clarify | Omitted | 2 | 2 | 3 | 3 | 4 | 5 | 6 | 6 | 7 |

If too few distinct positive words remain for the requested minimum, the agent asks for more terms or a minimum of one. A named-signal group also rejects duplicate canonical branches when its minimum exceeds one.

## How technical terms count

A minimum of two or more normally becomes a Boolean group with one branch per keyword. Word forms such as `fail`, `failed`, and `failing` share one branch. Technical identifiers such as `eth0/1`, `link-down`, and `x1000c0s0b0` each use a phrase branch, even if the field analyzer splits the identifier into several tokens.

The guard uses a simple analyzed match with the same integer minimum when the per-keyword form would exceed the clause, depth, text, or branch limits. It reports that fallback. An explicit minimum of one also uses a native match.

## Reading the result

With `OPENSEARCH_RANK_TEXT_MATCHES=true`, rows with positive text conditions and no requested sort appear best matches first, ties newest first. A requested permitted sort takes precedence. Counts and aggregations use filter context for required text.

The response reports the search keywords, minimum, whether it came from the default or the request, and whether ranking applied. It may say “at least 2 of: fan, off, rpm” and suggest asking for any of those words. An empty search means no returned document met the query conditions in that window.

[Implementation and verification notes](plan.md)
