"""
label_discovery.py

Solves the "Known Labels: Not yet verified" problem properly, instead of
just telling the LLM to avoid guessing. At startup, this module asks the
live Prometheus instance directly which labels actually exist on each
registered metric, once, and caches the result for the rest of the
session — exactly matching Prometheus's own recommended endpoint for
this ("/api/v1/series"), not the "/api/v1/labels" endpoint, which only
lists label names globally and doesn't tell you which metric they
belong to.

No LLM call happens anywhere in this module — like the Executor, this is
mechanical ground-truth lookup, not judgment.
"""
import re
import requests

DEFAULT_TIMEOUT_SECONDS = 10

# Matches a backticked metric name inside a Metric Directory table row,
# e.g. "| CPU | CPU utilization | `node_cpu_seconds_total` |"
_METRIC_NAME_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`")


def extract_metric_names(sub_skill_markdown: str) -> list[str]:
    """
    Pulls every backticked metric name out of an _index.md's
    '## 3. Metric Directory' section specifically — not the whole file —
    so prose mentioning a metric name elsewhere doesn't produce false
    positives. This reads names that are already documented as ground
    truth; it does not invent any. (Metric definitions themselves now
    live one level deeper, in each domain .md file — but the Metric
    Directory in _index.md is required to list every metric regardless,
    per that template's own rule, so this still gives the complete list.)
    """
    lines = sub_skill_markdown.splitlines()
    in_directory_section = False
    names: set[str] = set()

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## 3. Metric Directory") or stripped.startswith("## Metric Directory"):
            in_directory_section = True
            continue
        if in_directory_section and stripped.startswith("## "):
            break  # reached the next top-level section — stop
        if in_directory_section:
            names.update(_METRIC_NAME_RE.findall(line))

    return sorted(names)


def discover_labels(prometheus_url: str, metric_names: list[str],
                     timeout: int = DEFAULT_TIMEOUT_SECONDS) -> dict:
    """
    One HTTP call (Prometheus's /api/v1/series accepts multiple match[]
    params, OR'd together) that returns the real, currently-live label
    keys for every metric name given.

    Returns: {metric_name: sorted list of label keys (excluding
    "__name__"), ...}. Metrics with no live series yet (e.g. simulator
    not running, or metric never scraped) simply get an empty list —
    this is reported, never silently guessed around.

    On any failure to reach Prometheus at all, returns {} for every
    metric name and lets the caller decide how to proceed (existing
    "do not invent labels" behavior is the safe fallback either way).
    """
    result = {name: [] for name in metric_names}
    if not metric_names:
        return result

    url = prometheus_url.rstrip("/") + "/api/v1/series"
    params = [("match[]", name) for name in metric_names]

    try:
        resp = requests.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        body = resp.json()
    except requests.exceptions.RequestException:
        return result  # Prometheus unreachable — safe empty fallback
    except ValueError:
        return result  # not valid JSON — same safe fallback

    if body.get("status") != "success":
        return result

    labels_by_metric: dict = {name: set() for name in metric_names}
    for series in body.get("data", []):
        metric_name = series.get("__name__")
        if metric_name in labels_by_metric:
            labels_by_metric[metric_name].update(
                k for k in series.keys() if k != "__name__"
            )

    return {name: sorted(labels) for name, labels in labels_by_metric.items()}


def format_labels_for_prompt(labels_by_metric: dict) -> str:
    """
    Renders the discovered labels as a compact block to inject directly
    into Phase 2's prompt — grounded, verified reality instead of the
    sub-file's placeholder text.
    """
    if not labels_by_metric or not any(labels_by_metric.values()):
        return (
            "No live label data was available at session start "
            "(Prometheus may have been unreachable). Treat every "
            "metric's labels as unverified — do not invent label names."
        )

    lines = ["Labels below were fetched live from Prometheus at session start "
             "(via /api/v1/series) — this is verified, current ground truth, "
             "not a guess. Use ONLY these label keys when filtering by entity."]
    for name, labels in sorted(labels_by_metric.items()):
        if labels:
            lines.append(f"- `{name}`: {', '.join(labels)}")
        else:
            lines.append(f"- `{name}`: no live series found — treat as unverified, do not invent a label")
    return "\n".join(lines)
