"""
executor.py — Phase 4 of the pipeline.

Takes the already-validated output of Phase 3 (validation_tester in the
teammate's agent.py) and, for every result whose status is "ok" or
"panic_mode_best_effort", actually calls the live backend and attaches
a normalized "execution" block. Every other status (ambiguous_metric,
unsupported_metric, unmapped, declined, out_of_scope_action) is passed
through completely unchanged — this phase never invents a query to run
for a status that never produced one.

This is deliberately plain, deterministic code — see the module docstring
in prometheus_client_wrapper.py for why. No LLM call happens anywhere in
this file.

Usage:
    from executor import execute_contract
    enriched = execute_contract(contract_json, prometheus_base_url="http://localhost:9090")
"""
import copy
from datetime import datetime, timezone

from time_utils import resolve_time_range, TimeParseError
from prometheus_client_wrapper import query_range, STATUS_SUCCESS
from normalizer import normalize_prometheus_result

EXECUTABLE_STATUSES = {"ok", "panic_mode_best_effort"}


def _execute_prometheus_entry(entry, base_url):
    """Runs one contract entry's query against Prometheus, returns an
    execution block dict per the Section 15 contract."""
    try:
        resolved = resolve_time_range(entry["time_range"])
    except (TimeParseError, KeyError) as e:
        return {
            "execution_status": "endpoint_error",
            "error": f"Could not resolve time_range: {e}",
        }

    result = query_range(
        base_url=base_url,
        promql=entry["query"],
        start_ts=resolved["start"].timestamp(),
        end_ts=resolved["end"].timestamp(),
        step_seconds=resolved["step_seconds"],
    )

    execution = {
        "execution_status": result.execution_status,
        "resolved_time_range": {
            "start": resolved["start"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": resolved["end"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "step_seconds": resolved["step_seconds"],
        },
        "endpoint": base_url,
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    if result.execution_status == STATUS_SUCCESS:
        series = normalize_prometheus_result(result.raw_json)
        execution["series"] = series
        execution["series_count"] = len(series)
    else:
        execution["series"] = []
        execution["series_count"] = 0
        if result.error:
            execution["error"] = result.error

    return execution


def _execute_opensearch_entry(entry, base_url):
    """
    Stub: no OpenSearch sub-skill or live instance is registered yet
    (documented gap, see main_SKILL.md Section 4). Structurally present
    so wiring in a real OpenSearch client later is a small diff here,
    not a redesign of the contract or the calling code.
    """
    return {
        "execution_status": "not_executed",
        "error": (
            "OpenSearch execution is not implemented yet — no OpenSearch "
            "sub-skill or live instance is registered. This is a documented, "
            "intentional gap, not a bug."
        ),
        "series": [],
        "series_count": 0,
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _execute_one_entry(entry, prometheus_base_url, opensearch_base_url):
    if entry.get("status") not in EXECUTABLE_STATUSES:
        return entry  # untouched — no execution field added

    data_source = entry.get("data_source")
    enriched = copy.deepcopy(entry)

    if data_source == "prometheus":
        enriched["execution"] = _execute_prometheus_entry(entry, prometheus_base_url)
    elif data_source == "opensearch":
        enriched["execution"] = _execute_opensearch_entry(entry, opensearch_base_url)
    else:
        enriched["execution"] = {
            "execution_status": "endpoint_error",
            "error": f"Unrecognized data_source: {data_source!r}",
            "series": [],
            "series_count": 0,
        }

    return enriched


def execute_contract(contract, prometheus_base_url="http://localhost:9090",
                      opensearch_base_url="http://localhost:9200"):
    """
    contract: the full JSON dict produced by Phase 2/3 (mode: "single" or
    "multi"). Returns a new dict — the input is never mutated — with an
    "execution" block attached to every "ok"/"panic_mode_best_effort"
    entry. Every other status passes through unchanged.
    """
    mode = contract.get("mode")

    if mode == "single":
        return _execute_one_entry(contract, prometheus_base_url, opensearch_base_url)

    if mode == "multi":
        enriched = copy.deepcopy(contract)
        enriched["results"] = [
            _execute_one_entry(r, prometheus_base_url, opensearch_base_url)
            for r in contract.get("results", [])
        ]
        return enriched

    raise ValueError(f"Contract has no valid 'mode' field: {contract!r}")
