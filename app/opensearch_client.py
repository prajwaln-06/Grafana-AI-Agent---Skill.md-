"""
opensearch_client.py

Thin, hardened wrapper around OpenSearch's HTTP API. No authentication is
wired in by default (the current deployment target is a local, unauthenticated
instance at http://localhost:9600 -- see config.py), but every function
accepts an optional `auth` so adding basic auth or a bearer token later is a
config change, not a rewrite.

Two execution entry points, mirroring prometheus_client.py's split:

  search()      -> POST /<index_pattern>/_search  (raw document hits, or an
                    aggregation if the DSL body includes one -- OpenSearch
                    uses the SAME endpoint for both; there is no separate
                    "aggregation endpoint" the way Prometheus has query vs.
                    query_range as genuinely different URLs)

Plus schema/runtime discovery, the OpenSearch-side equivalent of
label_discovery.py's live Prometheus label verification:

  list_indices()        -> GET /_cat/indices?format=json
  get_field_mapping()   -> GET /<index_pattern>/_mapping
  discover_attribute_keys() -> derives observed `Attributes.*` sub-field
                                names from the mapping response -- this is
                                the OpenSearch analogue of Principle 9: never
                                a static catalog, always confirmed live.
"""
from __future__ import annotations

from dataclasses import dataclass

import requests

DEFAULT_TIMEOUT_SECONDS = 15

_session: requests.Session | None = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
    return _session


@dataclass
class ExecutionOutcome:
    status: str                  # "success" | "empty_result" | "endpoint_unreachable"
                                  # | "endpoint_error" | "timeout"
    raw_body: dict | None = None  # the full OpenSearch response body, on success
    error: str | None = None


def search(base_url: str, index_pattern: str, dsl_body: dict,
           timeout: float = DEFAULT_TIMEOUT_SECONDS,
           auth: tuple[str, str] | None = None) -> ExecutionOutcome:
    """
    POST /<index_pattern>/_search with the given DSL body. Handles BOTH a
    plain document search (no `aggs` key -- returns hits) and an aggregation
    query (`aggs` present, typically with "size": 0) -- normalizer.py
    inspects the response shape afterward to decide which of the two it's
    looking at; this function's job is just "execute the DSL body, return
    the raw body or a typed failure."

    index_pattern uses OpenSearch's own wildcard syntax directly, e.g.
    "syslog-*" or "syslog-*,consolelog-*" -- never a single resolved daily
    index name (matches the wildcard-pattern convention the authoritative
    material's own query examples use, so an index rolling over at midnight
    never silently drops out of a query mid-window).
    """
    url = f"{base_url.rstrip('/')}/{index_pattern}/_search"
    try:
        resp = _get_session().post(url, json=dsl_body, timeout=timeout, auth=auth)
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

    if resp.status_code != 200:
        error_msg = _extract_opensearch_error(body) or f"HTTP {resp.status_code}"
        return ExecutionOutcome(status="endpoint_error", error=error_msg)

    hits_total = body.get("hits", {}).get("total", {})
    total_value = hits_total.get("value") if isinstance(hits_total, dict) else hits_total
    has_aggs = bool(body.get("aggregations"))

    if not has_aggs and (total_value or 0) == 0:
        return ExecutionOutcome(status="empty_result", raw_body=body)

    return ExecutionOutcome(status="success", raw_body=body)


def _extract_opensearch_error(body: dict) -> str | None:
    err = body.get("error")
    if isinstance(err, dict):
        return err.get("reason") or err.get("type") or str(err)
    if isinstance(err, str):
        return err
    return None


# ---- schema / runtime discovery -----------------------------------------------


def list_indices(base_url: str, timeout: float = DEFAULT_TIMEOUT_SECONDS,
                  auth: tuple[str, str] | None = None) -> list[str] | None:
    """GET /_cat/indices?format=json -- returns currently-existing index
    names, or None on any failure (callers should treat that as "discovery
    unavailable right now," not fail the whole pipeline; the static index
    prefixes documented in the skill package remain usable even if this
    call fails, exactly per the blueprint's own static/dynamic split)."""
    url = f"{base_url.rstrip('/')}/_cat/indices"
    try:
        resp = _get_session().get(url, params={"format": "json"}, timeout=timeout, auth=auth)
        resp.raise_for_status()
        return [row["index"] for row in resp.json()]
    except (requests.exceptions.RequestException, ValueError, KeyError):
        return None


def get_field_mapping(base_url: str, index_pattern: str,
                       timeout: float = DEFAULT_TIMEOUT_SECONDS,
                       auth: tuple[str, str] | None = None) -> dict | None:
    """GET /<index_pattern>/_mapping -- returns the raw mapping response
    (keyed by concrete index name, each with a `mappings.properties` tree),
    or None on failure. This is what discover_attribute_keys() parses."""
    url = f"{base_url.rstrip('/')}/{index_pattern}/_mapping"
    try:
        resp = _get_session().get(url, timeout=timeout, auth=auth)
        resp.raise_for_status()
        return resp.json()
    except (requests.exceptions.RequestException, ValueError):
        return None


def discover_attribute_keys(base_url: str, index_pattern: str,
                             timeout: float = DEFAULT_TIMEOUT_SECONDS,
                             auth: tuple[str, str] | None = None) -> list[str] | None:
    """
    Derives the currently-observed `Attributes.*` sub-field names for the
    given index pattern from OpenSearch's dynamic mapping -- the OpenSearch
    analogue of label_discovery.py's Prometheus label-key verification.
    `Attributes` is mapped `type: object` with no explicit sub-properties, so
    OpenSearch dynamically maps whatever's actually been indexed under it;
    inspecting `<index>/_mapping` after data exists surfaces exactly those
    real, currently-present keys. Returns None (not []) if mapping discovery
    itself failed -- distinct from a genuine, successfully-confirmed empty
    result -- so callers don't confuse "couldn't check" with "checked, none
    exist."
    """
    mapping = get_field_mapping(base_url, index_pattern, timeout=timeout, auth=auth)
    if mapping is None:
        return None

    keys: set[str] = set()
    for index_body in mapping.values():
        properties = index_body.get("mappings", {}).get("properties", {})
        attributes_props = properties.get("Attributes", {}).get("properties", {})
        keys.update(attributes_props.keys())
    return sorted(keys)


def sample_recent_documents(base_url: str, index_pattern: str, size: int = 20,
                             timeout: float = DEFAULT_TIMEOUT_SECONDS,
                             auth: tuple[str, str] | None = None) -> list[dict] | None:
    """
    Complementary discovery path to discover_attribute_keys(): pulls the N
    most recent raw documents (sorted by @timestamp desc) instead of relying
    on mapping introspection alone. Two things this gives that mapping
    inspection alone doesn't: (1) a fallback if `Attributes` mapping ever
    comes back sparse/empty for reasons unrelated to whether data exists
    (mapping propagation lag, a fresh index), and (2) EXAMPLE VALUES for
    each key, not just key names -- e.g. confirming Severity actually takes
    values like "WARN"/"ERROR" in practice, which is useful context for
    query construction beyond just knowing the field exists. Returns None on
    failure, [] on a genuinely empty index pattern.
    """
    body = {
        "size": size,
        "sort": [{"@timestamp": {"order": "desc", "unmapped_type": "date"}}],
        "query": {"match_all": {}},
    }
    outcome = search(base_url, index_pattern, body, timeout=timeout, auth=auth)
    if outcome.status not in ("success", "empty_result"):
        return None
    if outcome.status == "empty_result":
        return []
    return [h.get("_source", {}) for h in outcome.raw_body.get("hits", {}).get("hits", [])]
