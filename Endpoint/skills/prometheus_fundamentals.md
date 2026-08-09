---
name: prometheus-fundamentals
description: Core PromQL data models, metric types, functions, and time modifiers.
version: 1.0
---

# Prometheus Fundamentals

## 1. Data Model
A time series is a metric name plus a set of key/value labels; each series holds a sequence of timestamped samples.

## 2. Metric Types

| Type | Behavior | Valid direct use |
|---|---|---|
| Counter | Only increases; resets to 0 only on restart | Never used raw across time — always wrap in `rate()`, `irate()`, or `increase()` first |
| Gauge | Rises or falls freely; a snapshot of "now" | Usable directly, or with `avg_over_time()`, `deriv()`, `predict_linear()`, etc. |
| Histogram | Cumulative bucket counters (`le` label) plus `_sum`/`_count` | Never read a bucket value as a percentile directly — use `histogram_quantile()` |
| Summary | Pre-computed quantiles exposed directly by the client | Use its exposed quantile labels directly; `histogram_quantile()` does not apply |

## 3. Selectors

| Syntax | Meaning |
|---|---|
| `metric_name{label="value"}` | Exact match |
| `label!="value"` | Negative match |
| `label=~"regex"` | Regex match |
| `label!~"regex"` | Regex negative match |
| `metric_name{...}[5m]` | Range vector — must be reduced by a function before use, never displayed directly |

## 4. Counter Functions

| Function | Behavior | When to use |
|---|---|---|
| `rate(x[5m])` | Average per-second increase over the window | Default choice for turning a Counter into a usable rate |
| `irate(x[5m])` | Uses only the last two points | More reactive to sudden spikes, noisier — use only when short-term reactivity matters more than smoothness |
| `increase(x[5m])` | Total increase over the window, not per-second | When the total count over a period is what's being asked, not a rate |

## 5. Gauge / Time-Window Functions

| Function | Behavior |
|---|---|
| `avg_over_time()`, `max_over_time()`, `min_over_time()`, `sum_over_time()`, `count_over_time()` | Standard reductions over a time window |
| `quantile_over_time(q, x[range])` | The q-th percentile value seen during the window |
| `stddev_over_time()` | Variability over the window |
| `deriv(x[range])` | Per-second slope — positive means increasing, negative decreasing |
| `predict_linear(x[range], seconds)` | Projects the current linear trend forward by the given number of seconds |

## 6. Aggregation Operators

| Operator | Behavior |
|---|---|
| `sum`, `avg`, `min`, `max`, `count`, `stddev`, `stdvar` | Combine values, grouped `by (label)` or `without (label)` |
| `topk(N, ...)` / `bottomk(N, ...)` | The N highest/lowest series |
| `quantile(q, ...)` | The q-th percentile across series |

## 7. Binary Operators and Vector Matching

Arithmetic (`+ - * /`) and comparison (`> < == >= <=`) operators work between two vectors. When combining two different metrics, use `on(label)` or `ignoring(label)` to control which labels must match, and `group_left`/`group_right` when the two sides have a different number of series per match group.

## 8. Time Modifiers

| Modifier | Behavior |
|---|---|
| `offset 1d` | Shifts the query back in time — placed inside the range selector: `rate(metric[5m] offset 1d)` |
| `[1h:1m]` (subquery) | Required to run a time-window function over an *already-computed expression*, not a bare metric — specifies both total window and resolution step |

## 9. Gotchas

| Gotcha | Correct handling |
|---|---|
| Counter reset on restart | Handled correctly by `rate()`/`increase()`; never handled by raw subtraction across timestamps |
| Rate window too short | Use a window at least 4× the scrape interval, or results become noisy/undefined |
| Target stops responding | Produces empty results after ~5 minutes (staleness), not zeroes |
| Function boundary extrapolation | `rate()`/`increase()` slightly extrapolate at the exact edges of their window — expect small imprecision there, not exact-to-the-second values |
| Combining two vectors without `on()`/`ignoring()` | Produces a many-to-many matching error if label sets don't align one-to-one — this is a signal to add explicit vector matching, not to simplify the query |
| Sub-second step resolution | Prometheus API rejects any step under 1 second. Never produce a sub-second step (e.g., `500ms`) in the `time_range.step` parameter. |

## 10. Time Expression Grammar (Tightened)

The Generator must strictly adhere to this format:
- `time_range.from` / `.to` MUST match exactly `now` or `now-<N><unit>` (where unit is `s`, `m`, `h`, `d`, `w`). 
- `time_range.step` MUST match `<N><unit>` with the same units, resolving to at least 1 second. No other phrasing is valid.