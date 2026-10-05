"""
label_discovery.py

Confirms, against a LIVE Prometheus instance, which label keys actually
exist for each metric the skill package's Metric Directories reference.
This is the runtime half of SKILL.md §5 Principle 9: "label keys are
sourced dynamically, never from this skill's reference files." Reference
files intentionally never enumerate label keys; this module is where they
actually get confirmed, once, at startup (and optionally refreshed later),
then formatted into a compact block the query-generation LLM call is given
alongside the domain reference content.

Uses /api/v1/series rather than /api/v1/labels because /api/v1/series lets
us scope discovery to one metric name at a time (`match[]=metric_name`),
which is what we actually need -- "what labels does node_cpu_seconds_total
carry" -- rather than every label key in the entire Prometheus instance.

LABEL VALUES (added alongside the original key-only discovery): Principle 9
as originally implemented only confirmed label KEYS ("is `instance` a real
label on this metric?"), never the VALUES those keys actually take ("is
`node-01` a real value of `instance` right now?"). That gap let a
plausible-looking but wrong value -- e.g. `instance="node-01"` when the
live value is actually `"node-01:9200"` (host:port from the scrape
target) -- through as a confident `status: "ok"` result that then silently
executes and returns an empty result, instead of failing closed the same
way an invented label KEY already does. `discover_label_details_for_metric`
closes that gap by also collecting values from the exact same
/api/v1/series response already being fetched for keys (no extra HTTP
call), capped per key at MAX_VALUES_PER_LABEL so a genuinely
high-cardinality label (thousands of distinct values) is never treated as
a closed, confirmed set -- see LabelDetails.truncated_keys.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import requests

DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_LOOKBACK = timedelta(hours=6)

# Above this many distinct observed values for a single label key on a
# single metric, we stop treating the observed set as exhaustive/confirmed
# -- both to keep the prompt block bounded and, more importantly, because
# validator.py's value-membership check must never reject a real value
# just because our own window/pagination happened to miss it. A label that
# exceeds this is recorded in LabelDetails.truncated_keys and deliberately
# left OUT of LabelDetails.values entirely, so both the prompt block and
# the Validator treat it as "cannot confirm either way" (fail-open),
# exactly the same posture already used for a metric whose discovery
# failed outright.
MAX_VALUES_PER_LABEL = 200

_session: requests.Session | None = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
    return _session


@dataclass(frozen=True)
class LabelDetails:
    """Everything confirmed for one metric in one discovery pass. `keys` is
    exactly what `discover_labels_for_metric` has always returned (kept as
    its own field for zero-friction backward compatibility with every
    existing key-only caller). `values` adds, for every key whose distinct
    observed-value count is within MAX_VALUES_PER_LABEL, the sorted list of
    values actually seen. A key present in `keys` but ABSENT from `values`
    means one of two things -- either it exceeded the cap (listed in
    `truncated_keys`) or it's a key with zero collisions to report because
    no series carried a value for it in this window (should not happen in
    practice, but handled the same fail-open way either way): callers must
    never infer "no values" from a missing key here."""
    keys: list[str] = field(default_factory=list)
    values: dict[str, list[str]] = field(default_factory=dict)
    truncated_keys: frozenset[str] = field(default_factory=frozenset)


def _fetch_series(base_url: str, metric_name: str, lookback: timedelta,
                   timeout: float) -> list[dict] | None:
    """Shared HTTP call behind both the keys-only and keys+values discovery
    functions, so adding value discovery never doubles the number of
    requests made against Prometheus."""
    end = datetime.now(timezone.utc)
    start = end - lookback
    url = base_url.rstrip("/") + "/api/v1/series"
    params = {"match[]": metric_name, "start": start.timestamp(), "end": end.timestamp()}
    try:
        resp = _get_session().get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        body = resp.json()
    except (requests.exceptions.RequestException, ValueError):
        return None

    if body.get("status") != "success":
        return None

    return body.get("data", [])


def discover_label_details_for_metric(base_url: str, metric_name: str,
                                       lookback: timedelta = DEFAULT_LOOKBACK,
                                       timeout: float = DEFAULT_TIMEOUT_SECONDS) -> LabelDetails | None:
    """Returns keys AND values observed on `metric_name` within the lookback
    window, or None if discovery failed -- same failure semantics as
    `discover_labels_for_metric` (connection error, non-2xx, malformed
    response; distinct from a genuine, confirmed-empty result)."""
    series = _fetch_series(base_url, metric_name, lookback, timeout)
    if series is None:
        return None

    value_sets: dict[str, set[str]] = {}
    for s in series:
        for k, v in s.items():
            if k == "__name__":
                continue
            value_sets.setdefault(k, set()).add(v)

    keys = sorted(value_sets.keys())
    values: dict[str, list[str]] = {}
    truncated: set[str] = set()
    for k, vals in value_sets.items():
        if len(vals) > MAX_VALUES_PER_LABEL:
            truncated.add(k)
        else:
            values[k] = sorted(vals)

    return LabelDetails(keys=keys, values=values, truncated_keys=frozenset(truncated))


def discover_label_details_for_metrics(base_url: str, metric_names: list[str],
                                        lookback: timedelta = DEFAULT_LOOKBACK,
                                        timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict[str, LabelDetails | None]:
    """Runs discover_label_details_for_metric for every metric name given.
    Each metric is independent -- one metric's discovery failing never
    blocks the others from resolving."""
    return {name: discover_label_details_for_metric(base_url, name, lookback=lookback, timeout=timeout)
            for name in metric_names}


def discover_labels_for_metric(base_url: str, metric_name: str,
                                lookback: timedelta = DEFAULT_LOOKBACK,
                                timeout: float = DEFAULT_TIMEOUT_SECONDS) -> list[str] | None:
    """Returns the sorted list of label keys observed on `metric_name` within
    the lookback window, or None if discovery failed (connection error,
    non-2xx, malformed response) -- distinct from a genuine, confirmed-empty
    result ([] -- the metric exists but somehow carries no labels beyond
    __name__, or no series were found in the window at all).

    Thin wrapper around `discover_label_details_for_metric` kept for
    backward compatibility with existing keys-only callers/tests -- makes
    the same single HTTP call, just discards the values half."""
    details = discover_label_details_for_metric(base_url, metric_name, lookback=lookback, timeout=timeout)
    return None if details is None else details.keys


def discover_labels_for_metrics(base_url: str, metric_names: list[str],
                                 lookback: timedelta = DEFAULT_LOOKBACK,
                                 timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict[str, list[str] | None]:
    """Runs discover_labels_for_metric for every metric name given. Each
    metric is independent -- one metric's discovery failing (e.g. a
    Prometheus hiccup mid-scan) never blocks the others from resolving."""
    return {name: discover_labels_for_metric(base_url, name, lookback=lookback, timeout=timeout)
            for name in metric_names}


def format_labels_for_prompt(labels_by_metric: dict[str, list[str] | None]) -> str:
    """Formats discovered labels into a compact block for the query-
    generation LLM call. Explicitly distinguishes "confirmed, these are the
    keys" from "discovery failed, do not guess" and from "confirmed, no
    labels beyond the metric name" -- three different states that must never
    be collapsed into one, since Principle 9 requires the model to refuse
    (declined/parameter_requires_clarification) rather than guess when a
    scope constraint can't be mapped to a confirmed label key."""
    if not labels_by_metric:
        return "No metrics required label discovery for this request."

    lines = ["Live label keys confirmed from the running Prometheus instance "
             "(source of truth for Principle 9 -- do not use a label key not "
             "listed here for a given metric; do not invent one by analogy):"]
    for metric, keys in sorted(labels_by_metric.items()):
        if keys is None:
            lines.append(f"- `{metric}`: DISCOVERY FAILED (could not reach Prometheus "
                          f"or parse its response). Do not assume any label key exists "
                          f"for this metric right now -- if the user's request requires "
                          f"a scope constraint, use declined/parameter_requires_clarification "
                          f"rather than guessing.")
        elif not keys:
            lines.append(f"- `{metric}`: confirmed, no label keys beyond the metric "
                          f"name were found in the current window.")
        else:
            lines.append(f"- `{metric}`: {', '.join(keys)}")
    return "\n".join(lines)


def format_label_values_for_prompt(details_by_metric: dict[str, "LabelDetails | None"]) -> str:
    """Formats discovered label VALUES into a compact block for the query-
    generation LLM call, alongside (not instead of) `format_labels_for_prompt`.
    Same three-state discipline as the keys block: a metric with no details
    at all is omitted here (its DISCOVERY FAILED line already covers it in
    the keys block); a key with a confirmed, exhaustive value list is
    spelled out so the model can match a user's phrase to the REAL value
    (e.g. `node-01:9200`, not a guessed `node-01`) instead of guessing; a
    key whose value set was too large to confirm exhaustively is called out
    by name specifically so the model knows not to treat "I didn't see it
    listed" as "it doesn't exist" for that key."""
    if not details_by_metric:
        return ""

    lines = ["Live label VALUES confirmed from the running Prometheus instance "
             "for keys with a small enough value set to enumerate exhaustively "
             "(source of truth for exact-match scope constraints -- if the "
             "user's phrase for a scope constraint is not an exact match to one "
             "of these values, do not guess the closest-looking one; use "
             "declined/parameter_requires_clarification instead). When more than "
             "one label key below could plausibly express the same scope (e.g. a "
             "bare identifier key alongside a differently-formatted one for the "
             "same entity, such as `node_id` vs. `instance` for the same host), "
             "prefer whichever key's values contain an EXACT literal match to the "
             "user's phrase over one that would require guessing a transformation "
             "(stripping/adding a port, a prefix, or any other reformatting):"]
    any_line = False
    for metric, details in sorted(details_by_metric.items()):
        if details is None:
            continue
        for key, values in sorted(details.values.items()):
            lines.append(f"- `{metric}` / `{key}`: {', '.join(values)}")
            any_line = True
        for key in sorted(details.truncated_keys):
            lines.append(f"- `{metric}` / `{key}`: too many distinct values in the current "
                          f"window to enumerate exhaustively ({MAX_VALUES_PER_LABEL}+ observed) -- "
                          f"do not assume a user's phrase is or isn't a real value for this key; "
                          f"prefer a regex selector (=~) or declined/parameter_requires_clarification "
                          f"over an exact-match guess.")
            any_line = True
    if not any_line:
        return ""
    return "\n".join(lines)
