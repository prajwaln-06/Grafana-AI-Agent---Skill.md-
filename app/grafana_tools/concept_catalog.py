"""Concept catalog and semantic metric resolver for Prometheus.

Maps natural language operational concepts (e.g., 'memory', 'cpu', 'gpu temp',
'disk') to verified Prometheus metrics and standard SRE formulas without
requiring users to memorize raw exporter metric names.
"""
from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Operational Concepts Catalog
# ---------------------------------------------------------------------------

CONCEPTS: list[dict[str, Any]] = [
    {
        "id": "cpu",
        "title": "CPU Utilization",
        "synonyms": [
            "cpu", "processor", "core", "cores", "cpu utilization",
            "cpu usage", "cpu load", "processor usage", "cpu %",
        ],
        "candidates": [
            "node_cpu_seconds_total",
            "container_cpu_usage_seconds_total",
            "node_load1",
        ],
        "default_viz": "timeseries",
        "unit": "percent",
        "description": "Calculates CPU utilization percentage by inverting idle time.",
    },
    {
        "id": "memory",
        "title": "Memory Utilization",
        "synonyms": [
            "memory", "ram", "mem", "memory usage", "memory utilization",
            "mem usage", "ram usage", "memory used", "ram used",
        ],
        "candidates": [
            "node_memory_MemAvailable_bytes",
            "container_memory_working_set_bytes",
            "process_resident_memory_bytes",
        ],
        "default_viz": "timeseries",
        "unit": "percent",
        "description": "Calculates memory utilization percentage based on available RAM.",
    },
    {
        "id": "disk",
        "title": "Disk Space Used",
        "synonyms": [
            "disk", "storage", "filesystem", "disk space", "disk usage",
            "disk utilization", "storage usage", "drive usage", "hdd", "ssd",
        ],
        "candidates": [
            "node_filesystem_avail_bytes",
            "node_disk_read_bytes_total",
            "node_disk_written_bytes_total",
        ],
        "default_viz": "gauge",
        "unit": "percent",
        "description": "Shows filesystem capacity used (excluding temp/overlay mounts).",
    },
    {
        "id": "network",
        "title": "Network Traffic",
        "synonyms": [
            "network", "bandwidth", "traffic", "net in", "net out",
            "network throughput", "bytes received", "bytes sent", "net traffic",
        ],
        "candidates": [
            "node_network_receive_bytes_total",
            "node_network_transmit_bytes_total",
        ],
        "default_viz": "timeseries",
        "unit": "bytes",
        "description": "Rates of network bytes transferred per second.",
    },
    {
        "id": "gpu_temp",
        "title": "GPU Temperature",
        "synonyms": [
            "gpu temp", "gpu temperature", "gpu thermal", "nvidia temp",
            "gpu heat", "gpu temperatures",
        ],
        "candidates": [
            "DCGM_FI_DEV_GPU_TEMP",
        ],
        "default_viz": "gauge",
        "unit": "celsius",
        "description": "NVIDIA GPU core temperature in Celsius.",
    },
    {
        "id": "gpu_util",
        "title": "GPU Utilization",
        "synonyms": [
            "gpu util", "gpu utilization", "gpu usage", "gpu compute",
            "nvidia util", "nvidia usage", "gpu core",
        ],
        "candidates": [
            "DCGM_FI_DEV_GPU_UTIL",
        ],
        "default_viz": "timeseries",
        "unit": "percent",
        "description": "NVIDIA GPU compute core utilization percentage.",
    },
    {
        "id": "gpu_memory",
        "title": "GPU Framebuffer Memory",
        "synonyms": [
            "gpu memory", "gpu ram", "vram", "gpu vram", "gpu fb",
            "framebuffer", "gpu memory usage",
        ],
        "candidates": [
            "DCGM_FI_DEV_FB_USED",
        ],
        "default_viz": "timeseries",
        "unit": "bytes",
        "description": "NVIDIA GPU VRAM framebuffer memory used.",
    },
    {
        "id": "load",
        "title": "System Load Average",
        "synonyms": [
            "load", "load average", "system load", "load1", "load 1m",
        ],
        "candidates": [
            "node_load1",
            "node_load5",
            "node_load15",
        ],
        "default_viz": "timeseries",
        "unit": "short",
        "description": "1-minute system load average.",
    },
    {
        "id": "uptime",
        "title": "System Uptime",
        "synonyms": [
            "uptime", "system uptime", "boot time", "node uptime",
        ],
        "candidates": [
            "node_boot_time_seconds",
            "up",
        ],
        "default_viz": "stat",
        "unit": "s",
        "description": "Time since last system boot in seconds.",
    },
]

# ---------------------------------------------------------------------------
# Stopwords and Visualization Parsing Helpers
# ---------------------------------------------------------------------------

STOP_WORDS = {
    "a", "an", "the", "and", "or", "to", "for", "in", "on", "at", "of",
    "add", "create", "make", "show", "display", "plot", "put", "update",
    "modify", "dashboard", "panel", "panels", "chart", "charts", "graph",
    "graphs", "metric", "metrics", "with", "from", "overview", "fleet",
}

VIZ_PATTERNS = [
    (r"\b(time\s*series|timeseries)\b", "timeseries"),
    (r"\b(gauge)\b", "gauge"),
    (r"\b(stat|single\s*stat)\b", "stat"),
    (r"\b(bar\s*chart|barchart|bar)\b", "barchart"),
    (r"\b(table)\b", "table"),
    (r"\b(pie\s*chart|piechart|pie)\b", "piechart"),
    (r"\b(histogram)\b", "histogram"),
    (r"\b(heatmap)\b", "heatmap"),
]


def extract_visualization_override(text: str) -> str | None:
    """Extract explicit visualization type if mentioned by the user."""
    for pattern, viz in VIZ_PATTERNS:
        if re.search(pattern, text, re.I):
            return viz
    return None


# ---------------------------------------------------------------------------
# Fast Path: Concept Resolution
# ---------------------------------------------------------------------------

def resolve_concept_panels(text: str, live_metrics: set[str]) -> list[dict[str, Any]]:
    """Scan user text for known concept synonyms and map to live Prometheus metrics.

    Returns a list of panel dictionaries ready for the proposal builder.
    """
    lower_text = text.lower()
    override_viz = extract_visualization_override(text)

    # 1. Collect all valid candidate matches across all concepts
    potential_matches: list[dict[str, Any]] = []
    for concept in CONCEPTS:
        concept_id = concept["id"]
        chosen_metric = next((c for c in concept["candidates"] if c in live_metrics), None)
        if not chosen_metric:
            continue

        for syn in concept["synonyms"]:
            pattern = rf"(?<![A-Za-z0-9_]){re.escape(syn)}(?![A-Za-z0-9_])"
            for m in re.finditer(pattern, lower_text):
                potential_matches.append({
                    "concept_id": concept_id,
                    "concept": concept,
                    "metric": chosen_metric,
                    "start": m.start(),
                    "end": m.end(),
                    "length": m.end() - m.start(),
                })

    # 2. Sort candidates by length descending (longest, most specific phrases first)
    potential_matches.sort(key=lambda m: m["length"], reverse=True)

    # 3. Filter out overlapping matches and duplicate concept IDs
    selected_matches: list[dict[str, Any]] = []
    occupied_spans: list[tuple[int, int]] = []
    seen_concept_ids: set[str] = set()

    for pm in potential_matches:
        cid = pm["concept_id"]
        if cid in seen_concept_ids:
            continue
        # Check span overlap
        start, end = pm["start"], pm["end"]
        overlaps = any(
            not (end <= o_start or start >= o_end)
            for o_start, o_end in occupied_spans
        )
        if not overlaps:
            seen_concept_ids.add(cid)
            occupied_spans.append((start, end))
            viz = override_viz or pm["concept"]["default_viz"]
            selected_matches.append({
                "measurement": None,
                "metric": pm["metric"],
                "title": pm["concept"]["title"],
                "visualization": viz,
                "existingVisualization": "",
                "allowEquivalent": bool(re.search(r"\b(another|additional|extra|second)\b", text, re.I)),
                "position": start,
                "conceptId": cid,
            })

    # Return panels sorted by appearance position in the user's prompt
    return sorted(selected_matches, key=lambda p: p["position"])


# ---------------------------------------------------------------------------
# Primary Metric Ordering & Title Formatting
# ---------------------------------------------------------------------------

# Preferred order of primary metrics when multiple metrics match a generic query
PRIORITY_METRIC_ORDER = [
    "DCGM_FI_DEV_GPU_UTIL",
    "DCGM_FI_DEV_GPU_TEMP",
    "DCGM_FI_DEV_FB_USED",
    "DCGM_FI_DEV_POWER_USAGE",
    "DCGM_FI_DEV_FB_FREE",
    "node_cpu_seconds_total",
    "node_memory_MemAvailable_bytes",
    "node_filesystem_avail_bytes",
    "node_network_receive_bytes_total",
    "node_network_transmit_bytes_total",
    "node_load1",
    "node_load5",
    "DCGM_FI_DEV_MEM_COPY_UTIL",
    "DCGM_FI_DEV_SM_CLOCK",
    "DCGM_FI_DEV_MEM_CLOCK",
    "DCGM_FI_PROF_DRAM_ACTIVE",
    "DCGM_FI_DEV_ECC_SBE_VOL_TOTAL",
    "DCGM_FI_DEV_ECC_DBE_VOL_TOTAL",
]


TITLE_OVERRIDES: dict[str, str] = {
    "DCGM_FI_DEV_GPU_UTIL": "GPU Utilization",
    "DCGM_FI_DEV_GPU_TEMP": "GPU Temperature",
    "DCGM_FI_DEV_FB_USED": "GPU VRAM Used",
    "DCGM_FI_DEV_FB_FREE": "GPU VRAM Free",
    "DCGM_FI_DEV_POWER_USAGE": "GPU Power Usage",
    "DCGM_FI_DEV_ECC_DBE_VOL_TOTAL": "Double-Bit ECC Errors",
    "DCGM_FI_DEV_ECC_SBE_VOL_TOTAL": "Single-Bit ECC Errors",
    "node_cpu_seconds_total": "CPU Seconds Total",
    "node_memory_MemAvailable_bytes": "Memory Available",
    "node_memory_MemTotal_bytes": "Total Memory",
    "node_filesystem_avail_bytes": "Filesystem Available",
}


def get_metric_title(metric: str) -> str:
    """Return a clean human-readable title for a metric."""
    if metric in TITLE_OVERRIDES:
        return TITLE_OVERRIDES[metric]
    matching_concept = next((c for c in CONCEPTS if metric in c["candidates"]), None)
    if matching_concept:
        return matching_concept["title"]
    raw = metric.replace("DCGM_FI_DEV_", "").replace("DCGM_FI_PROF_", "")
    return " ".join(p.capitalize() for p in raw.split("_") if p)


def get_metric_info(metric: str) -> tuple[str, str]:
    """Compatibility helper returning (title, description)."""
    return get_metric_title(metric), ""


# ---------------------------------------------------------------------------
# Scenario 2: Human-Friendly Disambiguation (Clean, Consistent, No Bloat)
# ---------------------------------------------------------------------------

def get_disambiguation_candidates(text: str, live_metrics: list[str]) -> list[str]:
    """Retrieve candidate metrics that match user text for disambiguation."""
    lower_text = text.lower()
    live_set = set(live_metrics)
    candidates: list[str] = []

    # 1. First check if any concepts match the text and gather their candidates in live_metrics
    for concept in CONCEPTS:
        if any(re.search(rf"(?<![A-Za-z0-9_]){re.escape(syn)}(?![A-Za-z0-9_])", lower_text) for syn in concept["synonyms"]):
            for cand in concept["candidates"]:
                if cand in live_set and cand not in candidates:
                    candidates.append(cand)

    # 2. Also search live metrics by non-stopword tokens
    tokens = [
        re.escape(w.lower())
        for w in re.findall(r"[A-Za-z0-9_]+", text)
        if len(w) >= 3 and w.lower() not in STOP_WORDS
    ]
    if tokens:
        pattern = re.compile("|".join(tokens), re.I)
        for m in live_metrics:
            if pattern.search(m) and m not in candidates:
                candidates.append(m)
    elif not candidates:
        candidates = [m for m in live_metrics[:10]]

    # Prioritize primary operational metrics over obscure hardware counters
    def candidate_sort_key(metric: str) -> tuple[int, int, str]:
        if metric in PRIORITY_METRIC_ORDER:
            return (0, PRIORITY_METRIC_ORDER.index(metric), metric)
        return (1, 0, metric)

    candidates.sort(key=candidate_sort_key)

    # Limit to top 6 relevant candidates to keep the output readable
    return candidates[:6]


def explain_disambiguation(text: str, live_metrics: list[str]) -> str:
    """Build a clean, consistent, unbloated choice list when multiple metrics match.

    Each option is a single clean line: [Number]. [Title] ([metric_name]).
    """
    candidates = get_disambiguation_candidates(text, live_metrics)
    if not candidates:
        return explain_not_found(text, live_metrics)

    lines = [
        f"Clarification required: I found {len(candidates)} metrics matching your request. Which one would you like to add?\n"
    ]
    for idx, metric in enumerate(candidates, 1):
        lines.append(f"{idx}. **{get_metric_title(metric)}** (`{metric}`)")

    lines.append("\n*Tip: You can reply with the number, metric name, or refine your request.*")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Scenario 3: Helpful Explanation When Metric Not Found
# ---------------------------------------------------------------------------

def explain_not_found(text: str, live_metrics: list[str]) -> str:
    """Explain what services are actively discovered on the cluster.

    Helps users understand what is available instead of failing silently.
    """
    cleaned = " ".join(w for w in text.split() if w.lower() not in STOP_WORDS)
    target_subject = f"'{cleaned}'" if cleaned else "the requested metric"

    # Identify active exporters by inspecting prefix patterns in live metrics
    detected_services: list[str] = []
    if any(m.startswith("node_") for m in live_metrics):
        detected_services.append("• **Linux Host Metrics (`node_exporter`)**: CPU, Memory, Disk, Network, Load, Uptime")
    if any(m.startswith("DCGM_") for m in live_metrics):
        detected_services.append("• **NVIDIA GPU Metrics (`dcgm-exporter`)**: GPU Temperature, Utilization, VRAM, Power")
    if any(m.startswith("container_") or m.startswith("kube_") for m in live_metrics):
        detected_services.append("• **Container Metrics (`cAdvisor / Kube`)**: Pod CPU, Memory, Restarts")
    if any(m.startswith("process_") for m in live_metrics):
        detected_services.append("• **Application Process Metrics**: Resident memory, Open FDs, CPU time")

    if not detected_services:
        detected_services.append("• Standard Prometheus time-series metrics")

    services_list = "\n".join(detected_services)

    return (
        f"Could not find {target_subject} in your Prometheus instance.\n\n"
        f"**Currently monitored services on your cluster:**\n"
        f"{services_list}\n\n"
        f"**What you can try:**\n"
        f"- Use common concepts (e.g. *\"memory\"*, *\"cpu\"*, *\"disk\"*, *\"gpu temp\"*)\n"
        f"- Specify an exact discovered metric name (e.g. `node_memory_MemAvailable_bytes`)"
    )
