"""
executor.py

Phase 4: deterministic execution. Takes a validated Output Contract (SKILL.md
§9, status "ok" or "panic_mode_best_effort" only) and attaches an
`execution` block to each such result by actually running the query against
the backend it targets. No LLM calls happen here -- this is why it's safe to
run unconditionally after validation passes, and why a bug here is a code
bug, not a model-output problem.

`status: "alert_rule_proposed"` (SKILL.md §12) is deliberately excluded from
EXECUTABLE_STATUSES below and therefore never reaches any of the
`_execute_*` functions in this file -- it passes through `_execute_one_entry`
unchanged, exactly like `ambiguous_metric`, `unsupported_metric`, `unmapped`,
`declined`, and `out_of_scope_action` already do. This is intentional and
load-bearing: an alert rule must never be created by this module or by
anything in the Router -> Generator -> Validator -> Executor pipeline this
file is the last stage of. Actual creation only ever happens via the
separate, explicit confirmation endpoint described in SKILL.md §12.1 (see
`app/api/routes_alerts.py` and `app/grafana_client.py`), which this module
has no dependency on and never imports.

Two hardening properties this module guarantees that the pre-migration
executor did not:

  1. PER-ENTRY ISOLATION. In a `mode: "multi"` response, one entry's failure
     (malformed contract, unexpected exception, whatever) never discards the
     other entries' results. Each entry gets its own try/except; a failure
     becomes that entry's `execution_status: "endpoint_error"`, not a
     500-style collapse of the whole response.
  2. JSON-SAFE OUTPUT. Every numeric value has passed through normalizer.py's
     NaN/Inf sanitization before this module hands the result back -- see
     normalizer.py's module docstring for why that matters.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app import normalizer, opensearch_client, prometheus_client, time_utils
from app.config import Settings
from chart_selection.selector import select_chart_type

logger = logging.getLogger(__name__)

# Deliberately does NOT include "alert_rule_proposed" (SKILL.md §12) -- that
# status must stay pending, untouched by this module, until the separate
# confirmation endpoint (app/api/routes_alerts.py) explicitly creates the
# rule via app/grafana_client.py. Adding it here would defeat the entire
# propose/confirm safety boundary §12 exists to enforce; do not add it
# without re-reading §12.1 first.
EXECUTABLE_STATUSES = {"ok", "panic_mode_best_effort"}


def execute_contract(contract: dict, settings: Settings) -> dict:
    """Top-level entry point. Mutates nothing on the input; returns a new
    dict with `execution` blocks attached where applicable. Handles both
    `mode: "single"` (fields at top level) and `mode: "multi"` (`results`
    array) per SKILL.md §9."""
    mode = contract.get("mode")
    if mode == "multi":
        results = contract.get("results", [])
        executed_results = [_execute_one_entry_isolated(r, settings) for r in results]
        return {**contract, "results": executed_results}

    # mode == "single": the contract's own top level IS the one result.
    return _execute_one_entry_isolated(contract, settings)


def _execute_one_entry_isolated(entry: dict, settings: Settings) -> dict:
    """Wraps _execute_one_entry in a catch-all so that ANY exception --
    a missing key in a malformed entry, a normalizer bug, whatever -- turns
    into a well-formed execution_status: "endpoint_error" for THIS entry
    only, rather than propagating up and discarding sibling entries in a
    multi-result response. This is the fix for the isolation bug the
    pre-migration executor had."""
    try:
        return _execute_one_entry(entry, settings)
    except Exception as exc:  # noqa: BLE001 -- intentionally broad: this is
        # the last line of defense for one entry in a batch, not a place to
        # be selective about exception types.
        logger.exception("Unhandled error executing contract entry: %s", entry.get("status"))
        if entry.get("status") not in EXECUTABLE_STATUSES:
            return entry
        return {
            **entry,
            "execution": {
                "execution_status": "endpoint_error",
                "error": f"Internal execution error: {exc}",
                "result_type": None,
                "endpoint": None,
                "fetched_at": _now_iso(),
            },
        }


def _execute_one_entry(entry: dict, settings: Settings) -> dict:
    if entry.get("status") not in EXECUTABLE_STATUSES:
        # Per execution-contract.md: no other status ever receives an
        # execution block. Pass through unchanged.
        return entry

    data_source = (entry.get("data_source") or "").strip().lower()
    if data_source == "prometheus":
        execution = _execute_prometheus_entry(entry, settings)
    elif data_source == "opensearch":
        execution = _execute_opensearch_entry(entry, settings)
    else:
        execution = {
            "execution_status": "not_executed",
            "error": f"Unrecognized or missing data_source: {entry.get('data_source')!r}.",
            "result_type": None,
            "endpoint": None,
            "fetched_at": _now_iso(),
        }

    return {**entry, "execution": execution}


# ---- Prometheus ---------------------------------------------------------------


def _execute_prometheus_entry(entry: dict, settings: Settings) -> dict:
    base_url = settings.prometheus_url
    promql = entry.get("query")
    if not promql or not isinstance(promql, str):
        return _error_block("endpoint_error", "Contract entry has no usable PromQL string in `query`.", base_url)

    # SKILL.md §8 "Instant vs. range" / §9 defines `query_type` as a
    # required, closed-enum field on every ok/panic_mode_best_effort
    # Prometheus result. The default here is only a defensive fallback for
    # a malformed/legacy entry that omits it -- validator.py already rejects
    # any explicitly-present value outside {"instant", "range"} before a
    # contract reaches this module.
    query_type = entry.get("query_type", "range")

    if query_type == "instant":
        time_field = entry.get("time_range", {}).get("time") or entry.get("time", "now")
        try:
            instant = time_utils.resolve_instant(time_field)
        except time_utils.TimeParseError as e:
            return _error_block("endpoint_error", str(e), base_url)

        outcome = prometheus_client.query_instant(base_url, promql, instant,
                                                    timeout=settings.prometheus_timeout_seconds)
        resolved_time_range = {"instant": _iso(instant)}
    else:
        time_range = entry.get("time_range")
        if not time_range:
            return _error_block("endpoint_error", "Contract entry has no `time_range`.", base_url)
        try:
            resolved = time_utils.resolve_time_range(time_range)
        except time_utils.TimeParseError as e:
            return _error_block("endpoint_error", str(e), base_url)

        step_seconds, widened = time_utils.cap_step_for_max_points(
            resolved["start"], resolved["end"], resolved["step_seconds"],
            max_points=settings.max_points_per_series,
        )
        outcome = prometheus_client.query_range(
            base_url, promql, resolved["start"], resolved["end"], step_seconds,
            timeout=settings.prometheus_timeout_seconds,
        )
        resolved_time_range = {
            "start": _iso(resolved["start"]),
            "end": _iso(resolved["end"]),
            "step_seconds": step_seconds,
            "step_widened": widened,
        }

    if outcome.status != "success":
        return _error_block(outcome.status, outcome.error, base_url, resolved_time_range=resolved_time_range)

    normalized = normalizer.normalize_prometheus_result(
        outcome.raw_data, max_series=settings.max_series_per_result
    )
    return _success_block(normalized, base_url, resolved_time_range=resolved_time_range)


# ---- OpenSearch -----------------------------------------------------------------


def _execute_opensearch_entry(entry: dict, settings: Settings) -> dict:
    base_url = settings.opensearch_url
    dsl_body = entry.get("query")
    if not dsl_body or not isinstance(dsl_body, dict):
        return _error_block("endpoint_error", "Contract entry has no usable DSL object in `query`.", base_url)

    index = entry.get("index")
    if not index:
        return _error_block("endpoint_error", "Contract entry has no `index` (required for OpenSearch results).", base_url)
    index_pattern = ",".join(index) if isinstance(index, list) else str(index)

    auth = settings.opensearch_auth  # None by default -- see config.py
    outcome = opensearch_client.search(
        base_url, index_pattern, dsl_body,
        timeout=settings.opensearch_timeout_seconds, auth=auth,
    )

    resolved_time_range = _best_effort_extract_time_range(dsl_body)

    if outcome.status not in ("success", "empty_result"):
        return _error_block(outcome.status, outcome.error, base_url, resolved_time_range=resolved_time_range)

    if outcome.status == "empty_result":
        return {
            "execution_status": "empty_result",
            "result_type": None,
            "endpoint": base_url,
            "fetched_at": _now_iso(),
            "resolved_time_range": resolved_time_range,
        }

    normalized = normalizer.normalize_opensearch_result(
        outcome.raw_body,
        max_series=settings.max_series_per_result,
        max_hits=settings.max_hits_per_result,
    )
    return _success_block(normalized, base_url, resolved_time_range=resolved_time_range)


def _best_effort_extract_time_range(dsl_body: dict) -> dict | None:
    """Best-effort, display-only extraction of a `range` clause on a date
    field from the DSL body, so the frontend can show an absolute resolved
    window for OpenSearch results too -- OpenSearch itself resolves date
    math (now-1h, now/d, ...) server-side, so this is never required for
    correct execution, only for a nicer response. Returns None if no
    recognizable date range clause is found; never raises."""
    try:
        candidates = _find_range_clauses(dsl_body)
        for field_name, bounds in candidates:
            if field_name in ("@timestamp", "Timestamp"):
                gte, lte = bounds.get("gte"), bounds.get("lte")
                if gte and lte:
                    start = time_utils.resolve_relative_time(gte) if isinstance(gte, str) and gte.startswith("now") else None
                    end = time_utils.resolve_relative_time(lte) if isinstance(lte, str) and lte.startswith("now") else None
                    return {
                        "from_expression": gte,
                        "to_expression": lte,
                        "start": _iso(start) if start else None,
                        "end": _iso(end) if end else None,
                    }
    except Exception:  # noqa: BLE001 -- purely cosmetic extraction, never fatal
        return None
    return None


def _find_range_clauses(node: Any) -> list[tuple[str, dict]]:
    found = []
    if isinstance(node, dict):
        if "range" in node and isinstance(node["range"], dict):
            for field_name, bounds in node["range"].items():
                if isinstance(bounds, dict):
                    found.append((field_name, bounds))
        for value in node.values():
            found.extend(_find_range_clauses(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_find_range_clauses(item))
    return found


# ---- shared helpers ---------------------------------------------------------------


def _success_block(normalized: normalizer.NormalizedResult, endpoint: str,
                    resolved_time_range: dict | None) -> dict:
    norm_dict = normalized.to_dict()
    chart_type = select_chart_type(norm_dict)
    return {
        "execution_status": "success" if normalized.count > 0 else "empty_result",
        "chart_type": chart_type,
        "resolved_time_range": resolved_time_range,
        "endpoint": endpoint,
        "fetched_at": _now_iso(),
        **norm_dict,
    }


def _error_block(execution_status: str, error: str | None, endpoint: str,
                  resolved_time_range: dict | None = None) -> dict:
    return {
        "execution_status": execution_status,
        "error": error,
        "result_type": None,
        "endpoint": endpoint,
        "fetched_at": _now_iso(),
        "resolved_time_range": resolved_time_range,
    }


def _now_iso() -> str:
    return _iso(datetime.now(timezone.utc))


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
