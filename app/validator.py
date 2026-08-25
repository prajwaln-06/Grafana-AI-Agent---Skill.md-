"""
validator.py

Deterministic (no-LLM) validation of a Generator-produced Output Contract
(SKILL.md Section 9) before it's allowed to reach the Executor.

WHY THIS REPLACES THE OLD LLM-BASED VALIDATOR: the previous pipeline spent a
third Gemini call asking a model to "check another model's work" against
rules that are almost entirely mechanical -- closed enums, required-field
presence, a query being non-empty, a label key appearing in a fixed
discovered list. None of that needs judgement, and asking an LLM to do it
adds latency, cost, and (worse) a genuinely non-deterministic pass/fail: the
same contract could validate differently across two runs. Every check below
is either a plain structural check or a set-membership check against data
the pipeline already deterministically discovered (metric names from the
Metric Directory, label keys from live Prometheus discovery) -- nothing
here requires natural-language judgement.

WHAT THIS FILE CHECKS, and where each rule comes from:

  1. STRUCTURAL CONFORMANCE to SKILL.md Section 9's Output Contract --
     every status has an exact, closed set of required fields; `mode` must
     match the actual result-object count (Section 6 Step 6); enums
     (`status`, `reason`, `measurement_used.type`) must be one of their
     documented values.
  2. SECTION 6 STEP 7's SANITY PASS -- for `ok`/`panic_mode_best_effort`
     results only: the query is non-empty and its shape (string vs. dict)
     matches its `data_source`.
  3. PRINCIPLE 1 / STEP 3g (SKILL.md Section 5 / 6) -- the metric name(s) in
     `measurement_used` must be metrics that actually appear in a Metric
     Directory this request opened, never a name invented mid-generation.
     Enforced only for `data_source: "prometheus"`, since that's the only
     backend with a live Metric Directory today.
  4. PRINCIPLE 9 (SKILL.md Section 5) -- every label key appearing in a
     PromQL query's selectors or aggregation clauses must be one of the
     keys `label_discovery.py` actually confirmed live for a metric
     involved in that query -- never invented "by analogy."
  5. prometheus-fundamentals.md's "Time Expression Grammar (Tightened)" --
     `time_range.from` / `.to` / `.step` must match the documented grammar
     and `time_utils` must be able to resolve them.
  6. SECTION 12's ALERT-RULE-CREATION NON-FABRICATION RULES -- for
     `alert_rule_proposed` results only: `alert_rule.condition_query` must
     reference the resolved metric (never a different, invented
     expression), and `alert_rule.comparison` (operator + threshold),
     `alert_rule.for_duration`, and `alert_rule.folder` must all be present
     -- none of these four are ever supplied by a reference file or invented
     mid-generation, so their absence here means the Generator should have
     produced `declined`/`parameter_requires_clarification` instead. This
     status is never in EXECUTABLE_STATUSES (executor.py) and this module
     has no say over what happens after validation passes -- see
     app/api/routes_alerts.py for the separate confirmation step.

DESIGN PRINCIPLE -- prefer a warning over a false rejection: any check this
module cannot confidently evaluate (e.g. an OpenSearch field name -- there
is no OpenSearch domain reference to confirm field names against yet, see
opensearch-fundamentals.md's status note) is skipped or downgraded to a
warning, never a hard failure. A validator with more false positives than
the LLM-based one it replaces would be a worse validator, not a stricter
one. Only checks with an unambiguous, mechanical right answer reject the
contract outright.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app import time_utils

# ---- public result type -------------------------------------------------------


@dataclass
class ValidationResult:
    passed: bool
    reason: str = ""
    warnings: list[str] = field(default_factory=list)


# ---- constants mirroring SKILL.md Section 9's closed enums --------------------

_VALID_STATUSES = {
    "ok", "panic_mode_best_effort", "ambiguous_metric", "unsupported_metric",
    "unmapped", "declined", "out_of_scope_action", "alert_rule_proposed",
}
_EXECUTABLE_STATUSES = {"ok", "panic_mode_best_effort"}
# NOTE: "alert_rule_proposed" (SKILL.md §12) is deliberately absent from
# _EXECUTABLE_STATUSES -- see executor.py's module docstring. It must never
# be auto-executed; it is validated here for shape/non-fabrication only.
_ALWAYS_SINGLE_STATUSES = {"unmapped", "declined", "out_of_scope_action", "alert_rule_proposed"}
_VALID_DECLINED_REASONS = {
    "nonsensical_input", "prompt_injection_attempt", "parameter_requires_clarification",
}
_VALID_MEASUREMENT_TYPES = {"raw_metric", "derived_measurement"}
_VALID_COMPARISON_OPERATORS = {">", "<", ">=", "<=", "==", "!="}

# Matches label="value" / label!="value" / label=~"value" / label!~"value"
# selector clauses, wherever they occur in the query string -- this syntax
# is unique to selectors, so scanning the whole string (not just inside
# {...}) is safe and catches every selector regardless of nesting.
_SELECTOR_LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*(?:=~|!~|=|!=)\s*"')
# Matches the label list inside by(...)/without(...)/on(...)/ignoring(...)/
# group_left(...)/group_right(...) aggregation/vector-matching clauses.
_AGG_CLAUSE_RE = re.compile(
    r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\(([^)]*)\)"
)


# ---- entry point ----------------------------------------------------------------


def validate_contract(
    contract: dict,
    *,
    known_metrics: set[str] | None = None,
    labels_by_metric: dict[str, list[str] | None] | None = None,
    known_references: set[str] | None = None,
    known_datasources: set[str] | None = None,
) -> ValidationResult:
    """Validates a full Output Contract response (either `mode: "single"`
    with fields inline, or `mode: "multi"` with a `results` array).

    known_metrics: every Prometheus metric name available across every
      Metric Directory this request opened (empty set if no Prometheus
      reference was matched).
    labels_by_metric: raw discovery output from
      label_discovery.discover_labels_for_metrics -- {metric_name: [keys]
      | None}. None means discovery failed for that metric (not "no
      labels exist"); Principle 9 checking is skipped for that metric
      specifically, since we cannot distinguish a real key from a
      fabricated one without a confirmed list to check against.
    known_references: every reference_path this request actually opened
      (matched references + their sibling overview.md files), so
      `reference_used` can be confirmed rather than trusted blindly.
    known_datasources: every data_source string actually in play for this
      request (from the Router's matched_references), so `data_source`
      can't silently smuggle in a value nothing was routed to.
    """
    known_metrics = known_metrics or set()
    labels_by_metric = labels_by_metric or {}
    known_references = known_references or set()
    known_datasources = known_datasources or set()

    if not isinstance(contract, dict):
        return ValidationResult(False, f"Contract is not a JSON object (got {type(contract).__name__}).")

    entries_or_error = _extract_entries(contract)
    if isinstance(entries_or_error, ValidationResult):
        return entries_or_error
    entries = entries_or_error

    warnings: list[str] = []
    for i, entry in enumerate(entries):
        result = _validate_entry(
            entry, known_metrics=known_metrics, labels_by_metric=labels_by_metric,
            known_references=known_references, known_datasources=known_datasources,
        )
        if not result.passed:
            prefix = f"results[{i}]: " if contract.get("mode") == "multi" else ""
            return ValidationResult(False, f"{prefix}{result.reason}")
        warnings.extend(result.warnings)

    return ValidationResult(True, warnings=warnings)


# ---- top-level mode/results structure ------------------------------------------


def _extract_entries(contract: dict) -> list[dict] | ValidationResult:
    if "mode" not in contract:
        return ValidationResult(False, "Response is missing the required top-level 'mode' field.")
    mode = contract["mode"]
    if mode not in ("single", "multi"):
        return ValidationResult(False, f"'mode' must be 'single' or 'multi'; got {mode!r}.")

    if mode == "single":
        entry = {k: v for k, v in contract.items() if k != "mode"}
        if "status" not in entry:
            return ValidationResult(False, "mode:'single' response has no 'status' field.")
        return [entry]

    # mode == "multi"
    if "results" not in contract or not isinstance(contract["results"], list):
        return ValidationResult(False, "mode:'multi' response is missing a 'results' array.")
    if "synthesis" not in contract:
        return ValidationResult(
            False,
            "mode:'multi' response is missing the 'synthesis' field (Section 9 "
            "requires the key even when its value is null).",
        )
    entries = contract["results"]
    if len(entries) < 2:
        return ValidationResult(
            False,
            f"mode:'multi' must contain 2+ result objects (Section 6 Step 6); "
            f"found {len(entries)}. A single result must use mode:'single' instead.",
        )
    for entry in entries:
        if not isinstance(entry, dict) or "status" not in entry:
            return ValidationResult(False, "One of 'results'' entries is not an object with a 'status' field.")
    return entries


# ---- per-entry validation --------------------------------------------------------


def _validate_entry(entry: dict, *, known_metrics: set[str], labels_by_metric: dict,
                     known_references: set[str], known_datasources: set[str]) -> ValidationResult:
    status = entry.get("status")
    if status not in _VALID_STATUSES:
        return ValidationResult(False, f"Unknown status {status!r}; must be one of {sorted(_VALID_STATUSES)}.")

    if status in _EXECUTABLE_STATUSES:
        return _validate_ok_entry(entry, known_metrics=known_metrics, labels_by_metric=labels_by_metric,
                                   known_references=known_references, known_datasources=known_datasources)
    if status == "ambiguous_metric":
        return _validate_ambiguous(entry)
    if status == "unsupported_metric":
        return _require_fields(entry, ["reference_used", "requested_measurement", "explanation"], status)
    if status == "unmapped":
        return _require_fields(entry, ["explanation"], status)
    if status == "declined":
        return _validate_declined(entry)
    if status == "out_of_scope_action":
        return _require_fields(entry, ["requested_action", "explanation"], status)
    if status == "alert_rule_proposed":
        return _validate_alert_rule_proposed(entry, known_references=known_references,
                                              known_datasources=known_datasources)
    return ValidationResult(False, f"No validation rule implemented for status {status!r} -- this is a gap in "
                                    f"validator.py, not necessarily a bad contract; treat as fail-safe.")


def _require_fields(entry: dict, fields_: list[str], status: str) -> ValidationResult:
    missing = [f for f in fields_ if not entry.get(f)]
    if missing:
        return ValidationResult(False, f"status {status!r} is missing required, non-empty field(s): {missing}.")
    return ValidationResult(True)


def _validate_ambiguous(entry: dict) -> ValidationResult:
    base = _require_fields(entry, ["reference_used", "clarification", "explanation"], "ambiguous_metric")
    if not base.passed:
        return base
    candidates = entry.get("candidates")
    if not isinstance(candidates, list) or len(candidates) < 2:
        return ValidationResult(False, "status 'ambiguous_metric' must list 2+ 'candidates' -- "
                                        "ambiguity between fewer than two options isn't ambiguity.")
    for c in candidates:
        if not isinstance(c, dict) or not c.get("name") or not c.get("purpose"):
            return ValidationResult(False, "Every 'ambiguous_metric' candidate needs a non-empty 'name' and 'purpose'.")
    return ValidationResult(True)


def _validate_declined(entry: dict) -> ValidationResult:
    base = _require_fields(entry, ["reason", "explanation"], "declined")
    if not base.passed:
        return base
    reason = entry.get("reason")
    if reason not in _VALID_DECLINED_REASONS:
        return ValidationResult(False, f"'declined' reason {reason!r} is not one of {sorted(_VALID_DECLINED_REASONS)}.")
    if reason == "parameter_requires_clarification" and not entry.get("clarification"):
        return ValidationResult(False, "declined/parameter_requires_clarification must include a non-empty 'clarification'.")
    return ValidationResult(True)


def _validate_alert_rule_proposed(entry: dict, *, known_references: set[str],
                                   known_datasources: set[str]) -> ValidationResult:
    """SKILL.md Section 12 / Section 9's `alert_rule_proposed` status.

    This result is NEVER auto-executed -- it's deliberately absent from
    executor.py's EXECUTABLE_STATUSES. It only ever reaches a real Grafana
    write via the separate, explicit confirmation flow described in Section
    12.1 (app/api/routes_alerts.py + app/grafana_client.py). This function's
    only job is confirming the PROPOSAL itself is well-formed and not
    fabricated -- it has no opinion on what happens after confirmation.
    """
    required = ["reference_used", "measurement_used", "data_source", "alert_rule", "explanation"]
    base = _require_fields(entry, required, "alert_rule_proposed")
    if not base.passed:
        return base

    if known_references and entry["reference_used"] not in known_references:
        return ValidationResult(
            False,
            f"reference_used {entry['reference_used']!r} was not one of the references this "
            f"request actually opened ({sorted(known_references)}) -- looks fabricated or stale.",
        )

    measurement = entry.get("measurement_used")
    if not isinstance(measurement, dict):
        return ValidationResult(False, "'measurement_used' must be an object.")
    m_type = measurement.get("type")
    if m_type not in _VALID_MEASUREMENT_TYPES:
        return ValidationResult(False, f"measurement_used.type {m_type!r} must be one of {sorted(_VALID_MEASUREMENT_TYPES)}.")
    name = measurement.get("name")
    if not name:
        return ValidationResult(False, "measurement_used.name is required and must be non-empty.")
    source_metrics = measurement.get("source_metrics", [])
    if m_type == "derived_measurement" and not source_metrics:
        return ValidationResult(False, "measurement_used.type is 'derived_measurement' but 'source_metrics' is empty; "
                                        "Section 5 Principle 7 requires multiple distinct source metrics for that classification.")
    if m_type == "raw_metric" and source_metrics:
        return ValidationResult(False, "measurement_used.type is 'raw_metric' but 'source_metrics' is non-empty; "
                                        "a raw metric with transformations applied is still 'raw_metric' per Principle 7.")

    data_source = (entry.get("data_source") or "").strip().lower()
    if known_datasources and data_source not in known_datasources:
        return ValidationResult(
            False,
            f"data_source {data_source!r} was not among the datasources this request actually "
            f"routed to ({sorted(known_datasources)}).",
        )
    if data_source != "prometheus":
        # Grafana alert rules are provisioned against a Prometheus datasource
        # in this deployment (grafana_client.py); no other backend is wired
        # up for alert-rule creation yet.
        return ValidationResult(
            False,
            f"'alert_rule_proposed' currently only supports data_source 'prometheus'; got {data_source!r}.",
        )

    alert_rule = entry.get("alert_rule")
    if not isinstance(alert_rule, dict):
        return ValidationResult(False, "'alert_rule' must be an object.")

    if not alert_rule.get("title"):
        return ValidationResult(False, "alert_rule.title is required and must be non-empty.")

    condition_query = alert_rule.get("condition_query")
    if not isinstance(condition_query, str) or not condition_query.strip():
        return ValidationResult(False, "alert_rule.condition_query is required and must be a non-empty PromQL string.")

    # Section 12.4: the condition must be derived from the SAME verified base
    # expression as the resolved metric -- mirrors _validate_prometheus_entry's
    # substring check for the same reason (a fabricated condition wouldn't
    # mention the metric it claims to be about).
    for metric_name in [name, *source_metrics]:
        if metric_name and metric_name not in condition_query:
            return ValidationResult(
                False,
                f"measurement_used references {metric_name!r} but that metric name does not appear "
                f"anywhere in alert_rule.condition_query {condition_query!r} -- Section 12.4 requires "
                f"the condition to be derived from that metric's own verified base expression.",
            )

    # Section 12.4/12.5: threshold and comparison direction are never
    # supplied by a reference file and are never invented -- they must
    # already have come from the user by the time this is validated.
    comparison = alert_rule.get("comparison")
    if not isinstance(comparison, dict):
        return ValidationResult(False, "alert_rule.comparison is required and must be an object with 'operator' and 'threshold'.")
    operator = comparison.get("operator")
    if operator not in _VALID_COMPARISON_OPERATORS:
        return ValidationResult(False, f"alert_rule.comparison.operator {operator!r} must be one of "
                                        f"{sorted(_VALID_COMPARISON_OPERATORS)}.")
    threshold = comparison.get("threshold")
    # Defensive coercion: if the Generator produced a numeric string
    # (e.g. "90", "90.5", or "90%" with a trailing unit) despite being told
    # to emit a JSON number, coerce it here rather than fail validation --
    # the underlying non-fabrication rule is about not INVENTING a
    # threshold when none was given, not about JSON typing. A string that
    # cleanly parses as a number carries exactly the same user-supplied
    # information the number would.
    if isinstance(threshold, str):
        stripped = threshold.strip().rstrip("%").rstrip("°").strip()
        try:
            threshold = float(stripped)
            comparison["threshold"] = threshold
        except ValueError:
            pass  # leave the original value; next check will reject it
    if threshold is None or isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        return ValidationResult(False, "alert_rule.comparison.threshold is required and must be a number -- "
                                        "Section 12.4 never invents a threshold, so it must come from the user, "
                                        "never be missing.")

    if not alert_rule.get("for_duration"):
        return ValidationResult(False, "alert_rule.for_duration is required -- Section 12.5 never invents a "
                                        "duration; it must come from the user, or the result should have been "
                                        "'declined'/'parameter_requires_clarification' instead.")

    # Section 12.5: folder is supplied by the surrounding application (from
    # deployment config), never invented by the Generator; datasource_uid is
    # always null at this stage, resolved only at confirmation time.
    if not alert_rule.get("folder"):
        return ValidationResult(False, "alert_rule.folder is required -- Section 12.5 expects the surrounding "
                                        "application to have supplied the deployment's default alert folder by "
                                        "this point (see app/config.py's GRAFANA_DEFAULT_FOLDER_UID).")
    if alert_rule.get("datasource_uid") is not None:
        return ValidationResult(
            False,
            "alert_rule.datasource_uid must be null at this stage (Section 12.5) -- resolving it is the "
            "surrounding application's job at confirmation time (app/grafana_client.py), never the Generator's.",
        )

    return ValidationResult(True)


def _validate_ok_entry(entry: dict, *, known_metrics: set[str], labels_by_metric: dict,
                        known_references: set[str], known_datasources: set[str]) -> ValidationResult:
    status = entry["status"]
    warnings: list[str] = []

    required = ["reference_used", "measurement_used", "data_source", "query", "explanation"]
    base = _require_fields(entry, required, status)
    if not base.passed:
        return base

    if status == "panic_mode_best_effort" and not entry.get("caveat"):
        return ValidationResult(False, "status 'panic_mode_best_effort' is missing the required 'caveat' field.")

    if known_references and entry["reference_used"] not in known_references:
        return ValidationResult(
            False,
            f"reference_used {entry['reference_used']!r} was not one of the references this "
            f"request actually opened ({sorted(known_references)}) -- looks fabricated or stale.",
        )

    measurement = entry.get("measurement_used")
    if not isinstance(measurement, dict):
        return ValidationResult(False, "'measurement_used' must be an object.")
    m_type = measurement.get("type")
    if m_type not in _VALID_MEASUREMENT_TYPES:
        return ValidationResult(False, f"measurement_used.type {m_type!r} must be one of {sorted(_VALID_MEASUREMENT_TYPES)}.")
    name = measurement.get("name")
    if not name:
        return ValidationResult(False, "measurement_used.name is required and must be non-empty.")
    source_metrics = measurement.get("source_metrics", [])
    if m_type == "derived_measurement" and not source_metrics:
        return ValidationResult(False, "measurement_used.type is 'derived_measurement' but 'source_metrics' is empty; "
                                        "Section 5 Principle 7 requires multiple distinct source metrics for that classification.")
    if m_type == "raw_metric" and source_metrics:
        return ValidationResult(False, "measurement_used.type is 'raw_metric' but 'source_metrics' is non-empty; "
                                        "a raw metric with transformations applied is still 'raw_metric' per Principle 7.")

    data_source = (entry.get("data_source") or "").strip().lower()
    if known_datasources and data_source not in known_datasources:
        return ValidationResult(
            False,
            f"data_source {data_source!r} was not among the datasources this request actually "
            f"routed to ({sorted(known_datasources)}).",
        )

    query = entry.get("query")

    if data_source == "prometheus":
        result = _validate_prometheus_entry(entry, query, name, source_metrics, known_metrics, labels_by_metric)
        if not result.passed:
            return result
        warnings.extend(result.warnings)
    elif data_source == "opensearch":
        result = _validate_opensearch_entry(entry, query)
        if not result.passed:
            return result
        warnings.extend(result.warnings)
    else:
        # An unrecognized data_source with no known_datasources constraint to
        # catch it (e.g. tests calling this in isolation) -- still confirm
        # the bare minimum from Step 7's sanity pass: query non-empty.
        if not query:
            return ValidationResult(False, "Query is empty.")
        warnings.append(f"data_source {data_source!r} is not 'prometheus' or 'opensearch' -- "
                         f"skipped datasource-specific checks (Step 7's shape check, Principle 9 label check).")

    return ValidationResult(True, warnings=warnings)


def _validate_prometheus_entry(entry: dict, query, name: str, source_metrics: list[str],
                                known_metrics: set[str], labels_by_metric: dict) -> ValidationResult:
    # Section 6 Step 7 sanity pass: shape matches data source.
    if not isinstance(query, str) or not query.strip():
        return ValidationResult(False, "data_source is 'prometheus' but 'query' is not a non-empty PromQL string.")

    # Principle 1 / Step 3g: the metric(s) actually used must be ones the
    # Metric Directory this request opened actually defines.
    metrics_to_check = [name, *source_metrics]
    if known_metrics:
        for metric_name in metrics_to_check:
            if metric_name not in known_metrics:
                return ValidationResult(
                    False,
                    f"measurement_used references {metric_name!r}, which is not in any Metric "
                    f"Directory this request opened ({sorted(known_metrics)}) -- looks fabricated.",
                )
            if metric_name not in query:
                return ValidationResult(
                    False,
                    f"measurement_used.name is {metric_name!r} but that metric name does not "
                    f"appear anywhere in the query string {query!r}.",
                )

    # Time Expression Grammar (Tightened), prometheus-fundamentals.md --
    # applies to range results. query_type "instant" (if the pipeline's
    # proposed contract addition is in use) carries a single `time` field
    # instead; accept either shape here without forcing the not-yet-adopted
    # field to be present.
    query_type = entry.get("query_type", "range")
    if query_type == "instant":
        time_field = entry.get("time_range", {}).get("time") if isinstance(entry.get("time_range"), dict) else entry.get("time")
        if time_field:
            try:
                time_utils.resolve_instant(time_field)
            except time_utils.TimeParseError as e:
                return ValidationResult(False, f"Instant time expression is invalid: {e}")
    else:
        time_range = entry.get("time_range")
        if not isinstance(time_range, dict):
            return ValidationResult(False, "data_source is 'prometheus' with query_type 'range' but 'time_range' is missing or not an object.")
        for endpoint_key in ("from", "to", "step"):
            if not time_range.get(endpoint_key):
                return ValidationResult(False, f"time_range.{endpoint_key} is required.")
        # Delegate to time_utils rather than re-implementing the Time
        # Expression Grammar here -- one parser, one place it can drift out
        # of sync with prometheus-fundamentals.md's documented grammar.
        # This call also confirms start < end (time_utils.resolve_time_range
        # itself raises TimeParseError otherwise), so there's no separate
        # ordering check needed after it.
        try:
            time_utils.resolve_time_range(time_range)
        except time_utils.TimeParseError as e:
            return ValidationResult(False, f"time_range is invalid: {e}")

    # Principle 9: every label key actually used in the query must be one
    # of the runtime-confirmed keys for a metric this query involves.
    warnings: list[str] = []
    used_labels = _extract_promql_label_keys(query)
    if used_labels:
        confirmed: set[str] = set()
        any_confirmed_source = False
        for metric_name in metrics_to_check:
            keys = labels_by_metric.get(metric_name)
            if keys is not None:
                any_confirmed_source = True
                confirmed.update(keys)
        if any_confirmed_source:
            invented = used_labels - confirmed
            if invented:
                return ValidationResult(
                    False,
                    f"Query uses label key(s) {sorted(invented)} not present in the runtime-confirmed "
                    f"label list for {metrics_to_check} ({sorted(confirmed)}) -- Principle 9 violation "
                    f"(invented or assumed-by-analogy label key).",
                )
        else:
            warnings.append(
                f"Query uses label key(s) {sorted(used_labels)} but label discovery did not succeed for "
                f"any of {metrics_to_check}, so Principle 9 compliance could not be confirmed either way."
            )

    return ValidationResult(True, warnings=warnings)


def _validate_opensearch_entry(entry: dict, query) -> ValidationResult:
    # Section 6 Step 7 sanity pass: shape matches data source.
    if not isinstance(query, dict) or not query:
        return ValidationResult(False, "data_source is 'opensearch' but 'query' is not a non-empty DSL object.")
    index = entry.get("index")
    if not index:
        return ValidationResult(False, "data_source is 'opensearch' but 'index' is missing (Section 9: "
                                        "'index' replaces 'time_range' for an OpenSearch-bound result).")
    # No OpenSearch domain reference exists yet (opensearch-fundamentals.md's
    # own status note), so there is no confirmed field/index-pattern catalog
    # to check `query`'s field names or `index`'s pattern against -- doing so
    # would mean guessing, which this module deliberately never does. Once an
    # opensearch-logs (or similar) domain exists, this function is the place
    # to add the same kind of Principle-9 field-key check
    # `_validate_prometheus_entry` does for PromQL label keys, using
    # field_discovery's live `Attributes.*` discovery the same way.
    return ValidationResult(True, warnings=[
        "OpenSearch entry passed structural checks only (no domain reference exists yet to confirm "
        "index patterns or field names against -- see opensearch-fundamentals.md's status note)."
    ])


def _extract_promql_label_keys(query: str) -> set[str]:
    keys: set[str] = set()
    keys.update(m.group(1) for m in _SELECTOR_LABEL_RE.finditer(query))
    for m in _AGG_CLAUSE_RE.finditer(query):
        inner = m.group(1)
        keys.update(part.strip() for part in inner.split(",") if part.strip())
    keys.discard("__name__")
    return keys