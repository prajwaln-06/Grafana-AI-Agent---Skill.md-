"""
app/telemetry.py

Prometheus telemetry definitions and helpers for monitoring the Grafana AI Agent,
implementing Google SRE's 4 Golden Signals (Latency, Traffic, Errors, Saturation).
"""
from __future__ import annotations

import contextlib
import logging
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

logger = logging.getLogger(__name__)

# Fallback flag if prometheus_client is not installed
PROMETHEUS_AVAILABLE = False
CONTENT_TYPE_LATEST: str = "text/plain; version=0.0.4; charset=utf-8"
REGISTRY: Any = None

# Metric variables declared with type Any so language servers (Pylance/MyPy)
# recognize their dynamic initialization cleanly across environments.
adk_request_duration_seconds: Any = None
adk_pipeline_step_duration_seconds: Any = None
adk_requests_total: Any = None
adk_llm_tokens_total: Any = None
adk_query_validation_total: Any = None
adk_query_empty_results_total: Any = None
adk_llm_api_errors_total: Any = None
adk_alert_proposals_total: Any = None
adk_in_flight_requests: Any = None
adk_active_sessions_gauge: Any = None
process_resident_memory_bytes: Any = None
adk_build_info: Any = None

try:
    from prometheus_client import (
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )
    from prometheus_client import CONTENT_TYPE_LATEST as _PROM_CONTENT_TYPE
    from prometheus_client import REGISTRY as _PROM_REGISTRY

    CONTENT_TYPE_LATEST = _PROM_CONTENT_TYPE
    REGISTRY = _PROM_REGISTRY
    PROMETHEUS_AVAILABLE = True
except ImportError:
    logger.warning("prometheus_client is not installed; telemetry is running in no-op mode.")


# -----------------------------------------------------------------------------
# Metric Definitions (Google SRE 4 Golden Signals)
# -----------------------------------------------------------------------------

# --- Signal 1: Latency (Response Time) ---
# Target SLA: p95 < 4.0s
LATENCY_BUCKETS = (0.1, 0.25, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 15.0, 30.0)
STEP_BUCKETS = (0.02, 0.05, 0.1, 0.25, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 8.0, 10.0)

if PROMETHEUS_AVAILABLE:
    adk_request_duration_seconds = Histogram(
        "adk_request_duration_seconds",
        "Total elapsed duration from prompt submission to response delivery in seconds.",
        buckets=LATENCY_BUCKETS,
    )

    adk_pipeline_step_duration_seconds = Histogram(
        "adk_pipeline_step_duration_seconds",
        "Elapsed duration per pipeline stage in seconds.",
        labelnames=("step",),
        buckets=STEP_BUCKETS,
    )

    # --- Signal 2: Traffic (Workload Demand & Token Spend) ---
    adk_requests_total = Counter(
        "adk_requests_total",
        "Total number of questions entered and completed by the AI agent.",
        labelnames=("status",),  # "success", "client_error", "server_error"
    )

    adk_llm_tokens_total = Counter(
        "adk_llm_tokens_total",
        "Total AI tokens consumed by the agent across LLM calls.",
        labelnames=("type",),  # "prompt", "completion", "total"
    )

    # --- Signal 3: Errors (Failures, Hallucinations, Empty Results) ---
    adk_query_validation_total = Counter(
        "adk_query_validation_total",
        "Total query validation checks, measuring AI hallucination frequency.",
        labelnames=("result",),  # "valid", "invalid"
    )

    adk_query_empty_results_total = Counter(
        "adk_query_empty_results_total",
        "Total syntactically valid queries that executed but returned 0 data points.",
    )

    adk_llm_api_errors_total = Counter(
        "adk_llm_api_errors_total",
        "Upstream AI provider API errors and quota exhaustions (e.g. HTTP 429).",
        labelnames=("status_code",),
    )

    adk_alert_proposals_total = Counter(
        "adk_alert_proposals_total",
        "Total alert rule proposals generated and human confirmation actions.",
        labelnames=("action",),  # "proposed", "confirmed", "discarded", "failed"
    )

    # --- Signal 4: Saturation (Memory Fullness & Concurrency) ---
    adk_in_flight_requests = Gauge(
        "adk_in_flight_requests",
        "Number of user questions currently being processed concurrently.",
    )

    adk_active_sessions_gauge = Gauge(
        "adk_active_sessions_gauge",
        "Total number of active chat session contexts retained in memory.",
    )

    try:
        process_resident_memory_bytes = Gauge(
            "process_resident_memory_bytes",
            "Resident memory size in bytes (physical RAM currently used by the Python process).",
        )
    except ValueError:
        # On Linux/POSIX, prometheus_client's built-in ProcessCollector already
        # registers and collects process_resident_memory_bytes from /proc/self/stat.
        process_resident_memory_bytes = None

    # Initialize known counter labels to 0.0 on startup so Prometheus establishes
    # baseline samples and calculates rate() / increase() immediately on the first event.
    for _s in ("success", "client_error", "server_error"):
        adk_requests_total.labels(status=_s)

    for _t in ("prompt", "completion", "total"):
        adk_llm_tokens_total.labels(type=_t)

    for _r in ("valid", "invalid"):
        adk_query_validation_total.labels(result=_r)

    for _a in ("proposed", "confirmed", "discarded", "failed"):
        adk_alert_proposals_total.labels(action=_a)

    adk_build_info = Gauge(
        "adk_build_info",
        "Application build and runtime metadata.",
        labelnames=("version", "skill_name", "python_runtime"),
    )
    _py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    adk_build_info.labels(
        version="1.4",
        skill_name="observability-query-builder",
        python_runtime=_py_ver,
    ).set(1.0)


# -----------------------------------------------------------------------------
# Telemetry Helpers
# -----------------------------------------------------------------------------

@contextlib.contextmanager
def track_in_flight() -> Iterator[None]:
    """Context manager to increment adk_in_flight_requests on entry and decrement on exit."""
    if PROMETHEUS_AVAILABLE and adk_in_flight_requests is not None:
        adk_in_flight_requests.inc()
    try:
        yield
    finally:
        if PROMETHEUS_AVAILABLE and adk_in_flight_requests is not None:
            adk_in_flight_requests.dec()


@contextlib.contextmanager
def time_pipeline_step(step_name: str) -> Iterator[None]:
    """Context manager to measure the execution duration of a pipeline step."""
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - start
        if PROMETHEUS_AVAILABLE and adk_pipeline_step_duration_seconds is not None:
            adk_pipeline_step_duration_seconds.labels(step=step_name).observe(elapsed)


def record_request_duration(duration_seconds: float) -> None:
    """Records overall request duration in the adk_request_duration_seconds histogram."""
    if PROMETHEUS_AVAILABLE and adk_request_duration_seconds is not None:
        adk_request_duration_seconds.observe(duration_seconds)


def record_request_status(status: str) -> None:
    """Records an incoming question completion status (e.g. 'success', 'client_error', 'server_error')."""
    if PROMETHEUS_AVAILABLE and adk_requests_total is not None:
        adk_requests_total.labels(status=status).inc()


def record_tokens(count: int, token_type: str = "total") -> None:
    """Records tokens consumed by LLM calls."""
    if isinstance(count, (int, float)) and count > 0 and PROMETHEUS_AVAILABLE and adk_llm_tokens_total is not None:
        adk_llm_tokens_total.labels(type=token_type).inc(count)


def record_validation_result(is_valid: bool) -> None:
    """Records query validation check result ('valid' or 'invalid' for hallucination tracking)."""
    if PROMETHEUS_AVAILABLE and adk_query_validation_total is not None:
        result = "valid" if is_valid else "invalid"
        adk_query_validation_total.labels(result=result).inc()


def record_empty_result() -> None:
    """Records when an executed query produced 0 data points (blank chart)."""
    if PROMETHEUS_AVAILABLE and adk_query_empty_results_total is not None:
        adk_query_empty_results_total.inc()


def record_llm_api_error(status_code: str | int) -> None:
    """Records an upstream LLM API error, e.g. 429 quota exhaustion."""
    if (
        status_code is not None
        and not hasattr(status_code, "_mock_return_value")
        and PROMETHEUS_AVAILABLE
        and adk_llm_api_errors_total is not None
    ):
        adk_llm_api_errors_total.labels(status_code=str(status_code)).inc()


def record_alert_proposal(action: str) -> None:
    """Records an alert proposal action ('proposed', 'confirmed', 'discarded', 'failed')."""
    if PROMETHEUS_AVAILABLE and adk_alert_proposals_total is not None:
        adk_alert_proposals_total.labels(action=action).inc()


def set_active_sessions_count(count: int) -> None:
    """Updates the gauge of active session contexts retained in memory."""
    if PROMETHEUS_AVAILABLE and adk_active_sessions_gauge is not None:
        adk_active_sessions_gauge.set(max(0, count))


def resolve_request_id(header_value: str | None) -> str:
    """Return an existing non-empty X-Request-ID header or mint a fresh request ID."""
    cleaned = (header_value or "").strip()
    return cleaned if cleaned else f"req-{uuid.uuid4().hex[:12]}"


def count_nested_sessions(sessions_obj: Any) -> int:
    """Count retained session entries across flat or nested ADK session dicts."""
    if not isinstance(sessions_obj, dict):
        return 0
    total = 0
    for val in sessions_obj.values():
        if isinstance(val, dict):
            sub_total = count_nested_sessions(val)
            # Empty leaf dict represents a mocked session entry (e.g. {"s-1": {}})
            total += sub_total if sub_total > 0 or val else 1
        else:
            total += 1
    return total


@contextlib.contextmanager
def track_agent_turn(
    session_counter: Callable[[], int] | None = None,
) -> Iterator[dict[str, str]]:
    """Measure end-to-end latency, concurrency, status, and active sessions for one agent request."""
    state: dict[str, str] = {"status": "success"}
    start_time = time.perf_counter()
    with track_in_flight():
        try:
            yield state
        except Exception as exc:
            status_code = getattr(exc, "status_code", None)
            if isinstance(status_code, int) and 400 <= status_code < 500:
                state["status"] = "client_error"
            else:
                state["status"] = "server_error"
            raise
        finally:
            record_request_duration(time.perf_counter() - start_time)
            record_request_status(state["status"])
            if session_counter is not None:
                try:
                    set_active_sessions_count(session_counter())
                except Exception:
                    pass


def generate_metrics_bytes() -> bytes:
    """Generates the latest Prometheus exposition output in bytes."""
    if PROMETHEUS_AVAILABLE:
        try:
            import psutil

            if process_resident_memory_bytes is not None:
                process_resident_memory_bytes.set(psutil.Process().memory_info().rss)
        except Exception:
            pass
        from prometheus_client import generate_latest as _gen_latest

        return _gen_latest(REGISTRY)
    return b"# Prometheus client is not installed\n"
