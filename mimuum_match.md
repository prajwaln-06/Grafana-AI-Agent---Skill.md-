# Finding more relevant OpenSearch logs with minimum match

`minimum_should_match` lets you tell the agent how many clues must appear in the same log entry. For example, you can look for controller messages that contain at least two of `fan`, `off`, and `RPM`, in any order. Requiring two clues filters out entries that mention only `fan`, while still finding relevant entries that omit `RPM`.

## How to use it

Ask the agent, “Show controller logs from the last 24 hours that match at least two of the words fan, off, and RPM in any order.” The agent searches the controller message field and applies the time range and any host filter you specify. Fabric health and iLO searches use their own message fields.

An unordered message search can include filler words. If the search text is `the fan is off at the RPM`, the query guard sends `fan off RPM` to OpenSearch with a minimum of `2`. The guard removes common standalone words such as `the`, `is`, and `at`, and counts repeated words once when checking that there are enough search terms. It keeps words like `no` and `not`. If only `fan` remains and the requested minimum is `2`, the guard rejects the query. Give it another meaningful term or ask for a minimum of one.

Use a different form when each clue is a whole phrase. For “show controller logs with at least two of ‘link down,’ ‘port disabled,’ and ‘transceiver fault,’” the agent creates one Boolean search condition per phrase and requires two conditions to match in the same entry. Word-form alternatives for one phrase stay within that phrase’s condition. Quoted phrases retain their original wording, and structured filters still handle exact hosts, event codes, and severity.

## What we verified

We ran the agent’s query guard and executor against a disposable OpenSearch 2.19.4 index with synthetic controller logs. With the same host and time filters, setting a minimum of two on the original text returned five entries. Cleaning the text returned four entries at a minimum of one and three at a minimum of two. The final search excluded a message with only stop words and a message that mentioned only `fan`. It kept `off after reboot fan`, demonstrating that the words can appear in a different order. The same query produced a count of three and an aggregation bucket of three. A separate phrase search still matched `the fan is off` in order.

The improvement in this check was relevance. Search latency depends on the dataset and should be measured separately. OpenSearch still uses the configured analyzer for each field, so use separate Boolean conditions when you need precise control over which named clues count. The agent keeps the current behavior for searches without a minimum.

[Implementation plan](plan.md) · [OpenSearch minimum-match documentation](https://docs.opensearch.org/latest/query-dsl/minimum-should-match/)
