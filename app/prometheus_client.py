"""
prometheus_client.py

Thin, hardened wrapper around Prometheus's HTTP API. Two query shapes:

  query_instant() -> GET /api/v1/query      (a single "right now" value)
  query_range()   -> GET /api/v1/query_range (a series over a time window)

Both return a small, uniform ExecutionOutcome regardless of success/failure
-- callers (executor.py) never need to catch exceptions from this module for
ordinary operational failures (timeout, connection refused, non-2xx, bad
JSON). Only programming errors raise.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import requests

DEFAULT_TIMEOUT_SECONDS = 15
MAX_STEP_WIDEN_ATTEMPTS = 3

_session: requests.Session | None = None


def _get_session() -> requests.Session:
    """One pooled requests.Session reused across calls, instead of opening a
    fresh TCP connection per query -- meaningful under any real concurrency,
    trivial to get wrong by accident with bare requests.get() everywhere."""
    global _session
    if _session is None:
        _session = requests.Session()
    return _session


@dataclass
class ExecutionOutcome:
    status: str                  # "success" | "empty_result" | "endpoint_unreachable"
                                  # | "endpoint_error" | "timeout"
    raw_data: dict | None = None  # the backend's `data` object, on success
    error: str | None = None
    step_widened: bool = False    # True if an automatic step-widening retry
                                   # was needed to avoid a "too many samples"
                                   # rejection -- surfaced so the caller can
                                   # note it in the response rather than
                                   # silently changing resolution.


def query_instant(base_url: str, promql: str, time: datetime,
                   timeout: float = DEFAULT_TIMEOUT_SECONDS) -> ExecutionOutcome:
    """GET /api/v1/query -- a single value (or small vector across labels)
    at one instant. This is the correct call for "what is X right now"
    questions; query_range with a short window is a workaround, not the
    same thing (it returns points-over-a-window, not a true instant read)."""
    params = {"query": promql, "time": time.timestamp()}
    return _get(base_url, "/api/v1/query", params, timeout)


def query_range(base_url: str, promql: str, start: datetime, end: datetime,
                 step_seconds: int,
                 timeout: float = DEFAULT_TIMEOUT_SECONDS) -> ExecutionOutcome:
    """GET /api/v1/query_range -- a series over [start, end] at the given
    step. Automatically widens the step (up to MAX_STEP_WIDEN_ATTEMPTS times)
    if Prometheus rejects the request for returning too many samples, rather
    than surfacing that as a bare endpoint_error the first time -- this is
    exactly the kind of transient, mechanically-recoverable failure a
    production caller shouldn't have to retry by hand."""
    attempt_step = step_seconds
    widened = False
    for attempt in range(MAX_STEP_WIDEN_ATTEMPTS):
        params = {
            "query": promql,
            "start": start.timestamp(),
            "end": end.timestamp(),
            "step": attempt_step,
        }
        outcome = _get(base_url, "/api/v1/query_range", params, timeout)
        if outcome.status == "endpoint_error" and _looks_like_too_many_samples(outcome.error):
            attempt_step *= 4
            widened = True
            continue
        outcome.step_widened = widened
        return outcome
    return outcome


def _looks_like_too_many_samples(error: str | None) -> bool:
    if not error:
        return False
    lowered = error.lower()
    return "too many samples" in lowered or "exceeded maximum resolution" in lowered


def _get(base_url: str, path: str, params: dict, timeout: float) -> ExecutionOutcome:
    url = base_url.rstrip("/") + path
    try:
        resp = _get_session().get(url, params=params, timeout=timeout)
    except requests.exceptions.Timeout:
        return ExecutionOutcome(status="timeout", error=f"Request to {url} exceeded {timeout}s timeout.")
    except requests.exceptions.ConnectionError as e:
        return ExecutionOutcome(status="endpoint_unreachable", error=f"Could not connect to {url}: {e}")
    except requests.exceptions.RequestException as e:
        return ExecutionOutcome(status="endpoint_error", error=f"Request to {url} failed: {e}")

    try:
        body = resp.json()
    except ValueError:
        return ExecutionOutcome(
            status="endpoint_error",
            error=f"{url} returned non-JSON response (HTTP {resp.status_code}).",
        )

    if resp.status_code != 200 or body.get("status") != "success":
        error_msg = body.get("error") or f"HTTP {resp.status_code}"
        return ExecutionOutcome(status="endpoint_error", error=error_msg)

    data = body.get("data", {})
    result = data.get("result", [])
    if not result:
        return ExecutionOutcome(status="empty_result", raw_data=data)
    return ExecutionOutcome(status="success", raw_data=data)
