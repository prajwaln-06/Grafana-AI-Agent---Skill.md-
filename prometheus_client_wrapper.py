"""
prometheus_client_wrapper.py

Deterministic HTTP client for Prometheus's query_range API. Deliberately
contains no LLM calls and no interpretation of the data — its only job is
to send the exact query the Generator/Validator phases already produced
and hand back exactly what Prometheus said, or a structured error if it
couldn't.

Design choice (confirmed with the project owner): execution against the
live endpoint is plain code, not an LLM agent step. An LLM narrating or
"cleaning up" numeric time-series data on the way through risks silently
altering values — the same fabrication risk the rest of this skill
architecture is built to avoid. This module never lets that happen because
it never touches an LLM at all.
"""
import requests

DEFAULT_TIMEOUT_SECONDS = 10


class PrometheusExecutionResult:
    """
    Structured result of attempting one query_range call. Never raises for
    "expected" failure modes (unreachable, timeout, bad query) — those are
    represented as data so the caller (executor.py) can attach them to the
    output contract instead of crashing the whole pipeline over one bad
    sub-result.
    """

    def __init__(self, execution_status, raw_json=None, error=None,
                 endpoint=None, request_params=None):
        self.execution_status = execution_status  # see STATUS_* constants
        self.raw_json = raw_json
        self.error = error
        self.endpoint = endpoint
        self.request_params = request_params

    def to_dict(self):
        d = {"execution_status": self.execution_status}
        if self.error is not None:
            d["error"] = self.error
        return d


STATUS_SUCCESS = "success"
STATUS_EMPTY = "empty_result"
STATUS_UNREACHABLE = "endpoint_unreachable"
STATUS_ENDPOINT_ERROR = "endpoint_error"
STATUS_TIMEOUT = "timeout"


def query_range(base_url, promql, start_ts, end_ts, step_seconds,
                 timeout=DEFAULT_TIMEOUT_SECONDS):
    """
    Calls Prometheus's GET /api/v1/query_range.

    base_url: e.g. "http://localhost:9090" (no trailing slash needed)
    promql: the exact query string produced by the Generator/Validator phases
    start_ts / end_ts: float unix timestamps (seconds)
    step_seconds: int

    Returns a PrometheusExecutionResult. Never raises for network/HTTP/
    Prometheus-side query errors — those are all folded into the result's
    execution_status/error fields.
    """
    url = base_url.rstrip("/") + "/api/v1/query_range"
    params = {
        "query": promql,
        "start": start_ts,
        "end": end_ts,
        "step": step_seconds,
    }

    try:
        resp = requests.get(url, params=params, timeout=timeout)
    except requests.exceptions.Timeout:
        return PrometheusExecutionResult(
            STATUS_TIMEOUT,
            error=f"Request to {url} timed out after {timeout}s.",
            endpoint=base_url, request_params=params,
        )
    except requests.exceptions.ConnectionError as e:
        return PrometheusExecutionResult(
            STATUS_UNREACHABLE,
            error=f"Could not reach Prometheus at {base_url}: {e}",
            endpoint=base_url, request_params=params,
        )
    except requests.exceptions.RequestException as e:
        return PrometheusExecutionResult(
            STATUS_ENDPOINT_ERROR,
            error=f"Request to {url} failed: {e}",
            endpoint=base_url, request_params=params,
        )

    if resp.status_code != 200:
        return PrometheusExecutionResult(
            STATUS_ENDPOINT_ERROR,
            error=f"Prometheus returned HTTP {resp.status_code}: {resp.text[:500]}",
            endpoint=base_url, request_params=params,
        )

    try:
        body = resp.json()
    except ValueError:
        return PrometheusExecutionResult(
            STATUS_ENDPOINT_ERROR,
            error=f"Prometheus response was not valid JSON: {resp.text[:500]}",
            endpoint=base_url, request_params=params,
        )

    # Prometheus's own envelope: {"status": "success"|"error", "data": {...}}
    # or, on error, {"status": "error", "errorType": ..., "error": "..."}
    if body.get("status") != "success":
        return PrometheusExecutionResult(
            STATUS_ENDPOINT_ERROR,
            error=(
                f"Prometheus rejected the query "
                f"({body.get('errorType', 'unknown')}): "
                f"{body.get('error', 'no message provided')}"
            ),
            endpoint=base_url, request_params=params,
        )

    result = body.get("data", {}).get("result", [])
    if not result:
        return PrometheusExecutionResult(
            STATUS_EMPTY, raw_json=body,
            endpoint=base_url, request_params=params,
        )

    return PrometheusExecutionResult(
        STATUS_SUCCESS, raw_json=body,
        endpoint=base_url, request_params=params,
    )
