"""
field_discovery.py

The OpenSearch-side counterpart to label_discovery.py. Per the blueprint's
§B.3/§B.4 finding, OpenSearch's static/dynamic boundary lands differently
than Prometheus's: the document ENVELOPE (`@timestamp`, `Severity`, `Body`,
`Resource.host.name`, `Resource.service.name`, `@version`) is fixed and
project-authoritative -- safe to document directly in reference files, the
same status as a Prometheus metric's documented type/unit. Only
`Attributes.*` sub-keys are the genuinely dynamic part, playing the same
role Prometheus label keys play for Principle 9.

This module therefore does NOT re-discover the envelope fields (those come
from the reference files themselves) -- it only confirms, live, the
`Attributes.*` keys actually observed for the index pattern(s) a request
needs, plus (optionally) which of the static index prefixes currently have
at least one real index.
"""
from __future__ import annotations

from app import opensearch_client as osc

# The three static, project-defined index patterns (blueprint §B.2: syslog/
# consolelog are daily-rotated, heartbeat is not). This is a TEMPORARY
# fallback, not the long-term home for this fact -- once
# `opensearch-logs/overview.md` exists, its index directory should become
# the single source of truth (the same way Prometheus metric names are read
# from an exporter's Metric Directory rather than hardcoded in
# label_discovery.py) and this constant should be replaced by a small parser
# reading that section, mirroring skill_index.metric_directory(). Every
# function below accepts an explicit `index_patterns` override so callers
# don't have to wait for that to happen to stop depending on this constant.
DEFAULT_INDEX_PATTERNS = ("syslog-*", "consolelog-*", "heartbeat")


def discover_attributes_for_pattern(base_url: str, index_pattern: str) -> dict:
    """Returns {"keys": [...] | None, "sample_documents": [...] | None} for
    one index pattern -- both are populated on a best-effort basis; either
    can independently be None if that specific discovery call failed,
    without blocking the other. Mapping-based discovery gives reliable key
    names; sampling additionally gives example values (see
    opensearch_client.sample_recent_documents's docstring for why both are
    useful together rather than picking one)."""
    keys = osc.discover_attribute_keys(base_url, index_pattern)
    samples = osc.sample_recent_documents(base_url, index_pattern, size=10)
    sample_attributes = None
    if samples:
        sample_attributes = [s.get("Attributes", {}) for s in samples if s.get("Attributes")]
    return {"keys": keys, "sample_documents": sample_attributes}


def discover_attributes_for_all_known_patterns(
    base_url: str, index_patterns: tuple[str, ...] = DEFAULT_INDEX_PATTERNS,
) -> dict[str, dict]:
    """Runs discover_attributes_for_pattern for every known index pattern up
    front, before the query-generation LLM call runs -- so whichever
    pattern(s) it ultimately selects, the Attributes discovery for that
    pattern is already available in the prompt context. Mirrors
    label_discovery.discover_labels_for_metrics' same "discover for
    everything plausible up front" approach, for the same reason: the
    generation phase is what decides the final selection, so discovery
    can't be deferred until after that decision without a chicken-and-egg
    problem."""
    return {pattern: discover_attributes_for_pattern(base_url, pattern) for pattern in index_patterns}


def discover_live_indices(base_url: str) -> list[str] | None:
    """Live GET /_cat/indices -- confirms which concrete, date-rotated
    indices currently exist. Per the blueprint, this is a nice-to-have
    confirmation, never a blocker: query construction should keep using the
    wildcarded prefix patterns (`syslog-*` etc.) regardless of what this
    returns, the same way PromQL construction doesn't need to know the exact
    current scrape timestamp before building a query."""
    return osc.list_indices(base_url)


def format_attributes_for_prompt(discovery_by_pattern: dict[str, dict]) -> str:
    """Formats discovered Attributes.* keys (and example values, where
    available) into a compact block for the query-generation LLM call --
    the OpenSearch-side equivalent of label_discovery.format_labels_for_prompt.
    Explicitly separates "confirmed keys" from "discovery failed" from
    "confirmed empty," for the same reason label_discovery does: a model
    must never be able to mistake a failed discovery call for a confirmed
    absence of attributes."""
    if not discovery_by_pattern:
        return "No OpenSearch index patterns required Attributes discovery for this request."

    lines = ["Live `Attributes.*` sub-field keys confirmed from the running "
             "OpenSearch instance (do not use an Attributes key not listed "
             "here; do not invent one by analogy with another log event):"]
    for pattern, discovery in sorted(discovery_by_pattern.items()):
        keys = discovery.get("keys")
        if keys is None:
            lines.append(f"- `{pattern}`: DISCOVERY FAILED (could not reach OpenSearch "
                          f"or parse its mapping response). Do not assume any "
                          f"Attributes key exists for this index pattern right now.")
        elif not keys:
            lines.append(f"- `{pattern}`: confirmed, no `Attributes.*` sub-keys are "
                          f"currently mapped for this pattern (may mean no matching "
                          f"documents have been indexed yet).")
        else:
            lines.append(f"- `{pattern}`: {', '.join(keys)}")

        samples = discovery.get("sample_documents")
        if samples:
            example = samples[0]
            lines.append(f"  Example observed Attributes value on `{pattern}`: {example}")
    return "\n".join(lines)
