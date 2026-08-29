"""PromQL builder for Prometheus metrics.

Contains well-known query patterns for common exporters and a generic
fallback heuristic. This is NOT a metric registry — it knows nothing about
which metrics exist. Callers discover metrics via Prometheus schema APIs and
pass concrete metric names here.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Well-known PromQL patterns
# Keyed by exact metric name. Provides precise queries for exporters whose
# metrics require non-trivial expressions (e.g. cpu idle inversion, ratios).
# The fallback for any unrecognised metric is build_promql().
# ---------------------------------------------------------------------------

# Each value is a callable(metric, label, matcher) -> str
# where `matcher` is the already-formatted label selector string.
_PATTERNS: dict[str, str] = {
    # node_exporter CPU: invert idle mode to get utilisation percentage
    "node_cpu_seconds_total": (
        "100 - (avg by ({label}) (rate(node_cpu_seconds_total{{{matcher},mode=\"idle\"}}[5m])) * 100)"
    ),
    # node_exporter memory: available/total ratio → percentage used
    "node_memory_MemAvailable_bytes": (
        "100 * (1 - node_memory_MemAvailable_bytes{{{matcher}}}"
        " / node_memory_MemTotal_bytes{{{matcher}}})"
    ),
    # node_exporter disk: available/total ratio → percentage used, exclude tmpfs
    "node_filesystem_avail_bytes": (
        "100 * (1 - node_filesystem_avail_bytes{{{matcher},fstype!~\"tmpfs|overlay\"}}"
        " / node_filesystem_size_bytes{{{matcher},fstype!~\"tmpfs|overlay\"}})"
    ),
    # node_exporter network: rate of bytes received, exclude loopback
    "node_network_receive_bytes_total": (
        "rate(node_network_receive_bytes_total{{{matcher},device!~\"lo\"}}[5m])"
    ),
    # DCGM GPU utilisation — raw gauge, no transformation needed
    "DCGM_FI_DEV_GPU_UTIL": "DCGM_FI_DEV_GPU_UTIL{{{matcher}}}",
    # DCGM GPU temperature — raw gauge
    "DCGM_FI_DEV_GPU_TEMP": "DCGM_FI_DEV_GPU_TEMP{{{matcher}}}",
}

# ---------------------------------------------------------------------------
# Target label discovery order (single source of truth)
# ---------------------------------------------------------------------------

TARGET_LABEL_CANDIDATES: tuple[str, ...] = (
    "node_id", "instance", "node", "host", "hostname"
)

# ---------------------------------------------------------------------------
# Unit inference heuristic
# ---------------------------------------------------------------------------

def infer_unit(metric: str) -> str:
    """Return a sensible Grafana unit string based on the metric name."""
    lower = metric.lower()
    if "bytes" in lower:
        return "bytes"
    if "seconds" in lower or "_duration" in lower:
        return "s"
    if "ratio" in lower or "utilization" in lower or "fraction" in lower:
        return "percentunit"
    if "temp" in lower:
        return "celsius"
    if "percent" in lower or "_pct" in lower:
        return "percent"
    return "short"


# ---------------------------------------------------------------------------
# PromQL builder
# ---------------------------------------------------------------------------

def build_promql(
    metric: str,
    label: str,
    target: str | None,
    *,
    wildcard: bool = False,
    metric_type: str = "",
) -> str:
    """Build a PromQL expression for `metric` filtered by `label`=`target`.

    Resolution order:
    1. Well-known pattern from ``_PATTERNS`` (precise, exporter-specific).
    2. Generic heuristic: ``rate()`` for counters/``_total`` metrics,
       plain selector otherwise.

    Args:
        metric:      Exact Prometheus metric name (as returned by discovery).
        label:       Label to filter on (e.g. ``"instance"``).
        target:      Label value, or ``None`` for all instances.
        wildcard:    If True and target is None, emit ``label=~".*"`` instead
                     of an empty equality match.  Use for alerts.
        metric_type: ``"counter"`` / ``"gauge"`` / ``""`` from metadata.
    """
    if target:
        matcher = f'{label}="{target}"'
    elif wildcard:
        matcher = f'{label}=~".*"'
    else:
        matcher = f'{label}=""'

    pattern = _PATTERNS.get(metric)
    if pattern:
        return pattern.format(label=label, matcher=matcher)

    # Generic fallback
    selector = f"{metric}{{{matcher}}}"
    if metric_type == "counter" or metric.endswith("_total"):
        return f"rate({selector}[5m])"
    return selector
