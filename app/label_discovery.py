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
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests

DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_LOOKBACK = timedelta(hours=6)

_session: requests.Session | None = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
    return _session


def discover_labels_for_metric(base_url: str, metric_name: str,
                                lookback: timedelta = DEFAULT_LOOKBACK,
                                timeout: float = DEFAULT_TIMEOUT_SECONDS) -> list[str] | None:
    """Returns the sorted list of label keys observed on `metric_name` within
    the lookback window, or None if discovery failed (connection error,
    non-2xx, malformed response) -- distinct from a genuine, confirmed-empty
    result ([] -- the metric exists but somehow carries no labels beyond
    __name__, or no series were found in the window at all)."""
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

    keys: set[str] = set()
    for series in body.get("data", []):
        keys.update(k for k in series.keys() if k != "__name__")
    return sorted(keys)


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
            if "node_id" in keys:
                lines.append(f"  * Runtime metadata: `node_id` is the confirmed label key for host/node names (e.g. node-00, node-01).")
            if "instance" in keys:
                lines.append(f"  * Runtime metadata: `instance` is the confirmed label key for scrape target addresses (e.g. host:port).")
            if "device" in keys:
                lines.append(f"  * Runtime metadata: `device` is the confirmed label key for filesystem/disk or interface devices.")
            if "gpu" in keys:
                lines.append(f"  * Runtime metadata: `gpu` is the confirmed label key for GPU IDs/numbers.")
    return "\n".join(lines)
