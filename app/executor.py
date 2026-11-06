"""
executor.py

Phase 4: deterministic execution. Takes a validated Output Contract (SKILL.md
§9, status "ok" or "panic_mode_best_effort" only) and attaches an
`execution` block to each such result by actually running the query against
the backend it targets. No LLM calls happen here -- this is why it's safe to
run unconditionally after validation passes, and why a bug here is a code
bug, not a model-output problem.

`status: "alert_rule_proposed"` (SKILL.md §12) is deliberately excluded from
EXECUTABLE_STATUSES below and therefore never reaches any of the
`_execute_*` functions in this file -- it passes through `_execute_one_entry`
unchanged, exactly like `ambiguous_metric`, `unsupported_metric`, `unmapped`,
`declined`, and `out_of_scope_action` already do. This is intentional and
load-bearing: an alert rule must never be created by this module or by
anything in the Router -> Generator -> Validator -> Executor pipeline this
file is the last stage of. Actual creation only ever happens via the
separate, explicit confirmation endpoint described in SKILL.md §12.1 (see
`POST /api/v1/alerts/confirm` in `run_server.py` and `app/grafana_client.py`), which this module
has no dependency on and never imports.

Two hardening properties this module guarantees that the pre-migration
executor did not:

  1. PER-ENTRY ISOLATION. In a `mode: "multi"` response, one entry's failure
     (malformed contract, unexpected exception, whatever) never discards the
     other entries' results. Each entry gets its own try/except; a failure
     becomes that entry's `execution_status: "endpoint_error"`, not a
     500-style collapse of the whole response.
  2. JSON-SAFE OUTPUT. Every numeric value has passed through normalizer.py's
     NaN/Inf sanitization before this module hands the result back -- see
     normalizer.py's module docstring for why that matters.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app import normalizer, opensearch_client, prometheus_client, time_utils
from app.config import Settings

logger = logging.getLogger(__name__)

# Deliberately does NOT include "alert_rule_proposed" (SKILL.md §12) -- that
# status must stay pending, untouched by this module, until the separate
# confirmation endpoint (POST /api/v1/alerts/confirm in run_server.py) explicitly creates the
# rule via app/grafana_client.py. Adding it here would defeat the entire
# propose/confirm safety boundary §12 exists to enforce; do not add it
# without re-reading §12.1 first.
EXECUTABLE_STATUSES = {"ok", "panic_mode_best_effort"}


def execute_contract(contract: dict, settings: Settings) -> dict:
    """Top-level entry point. Mutates nothing on the input; returns a new
    dict with `execution` blocks attached where applicable. Handles both
    `mode: "single"` (fields at top level) and `mode: "multi"` (`results`
    array) per SKILL.md §9."""
    mode = contract.get("mode")
    if mode == "multi":
        results = contract.get("results", [])
        executed_results = [_execute_one_entry_isolated(r, settings) for r in results]
        return {**contract, "results": executed_results}

    # mode == "single": the contract's own top level IS the one result.
    return _execute_one_entry_isolated(contract, settings)


def _execute_one_entry_isolated(entry: dict, settings: Settings) -> dict:
    """Wraps _execute_one_entry in a catch-all so that ANY exception --
    a missing key in a malformed entry, a normalizer bug, whatever -- turns
    into a well-formed execution_status: "endpoint_error" for THIS entry
    only, rather than propagating up and discarding sibling entries in a
    multi-result response. This is the fix for the isolation bug the
    pre-migration executor had."""
    try:
        return _execute_one_entry(entry, settings)
    except Exception as exc:  # noqa: BLE001 -- intentionally broad: this is
        # the last line of defense for one entry in a batch, not a place to
        # be selective about exception types.
        logger.exception("Unhandled error executing contract entry: %s", entry.get("status"))
        if entry.get("status") not in EXECUTABLE_STATUSES:
            return entry
        return {
            **entry,
            "execution": {
                "execution_status": "endpoint_error",
                "error": f"Internal execution error: {exc}",
                "result_type": None,
                "endpoint": None,
                "fetched_at": _now_iso(),
            },
        }


def _execute_one_entry(entry: dict, settings: Settings) -> dict:
    if entry.get("status") not in EXECUTABLE_STATUSES:
        # Per execution-contract.md: no other status ever receives an
        # execution block. Pass through unchanged.
        return entry

    data_source = (entry.get("data_source") or "").strip().lower()
    if data_source == "prometheus":
        execution = _execute_prometheus_entry(entry, settings)
    elif data_source == "opensearch":
        execution = _execute_opensearch_entry(entry, settings)
    else:
        execution = {
            "execution_status": "not_executed",
            "error": f"Unrecognized or missing data_source: {entry.get('data_source')!r}.",
            "result_type": None,
            "endpoint": None,
            "fetched_at": _now_iso(),
        }

    return {**entry, "execution": execution}


# ---- Prometheus ---------------------------------------------------------------


def _execute_prometheus_entry(entry: dict, settings: Settings) -> dict:
    base_url = settings.prometheus_url
    promql = entry.get("query")
    if not promql or not isinstance(promql, str):
        return _error_block("endpoint_error", "Contract entry has no usable PromQL string in `query`.", base_url)

    # SKILL.md §8 "Instant vs. range" / §9 defines `query_type` as a
    # required, closed-enum field on every ok/panic_mode_best_effort
    # Prometheus result. The default here is only a defensive fallback for
    # a malformed/legacy entry that omits it -- validator.py already rejects
    # any explicitly-present value outside {"instant", "range"} before a
    # contract reaches this module.
    query_type = entry.get("query_type", "range")

    if query_type == "instant":
        time_field = entry.get("time_range", {}).get("time") or entry.get("time", "now")
        try:
            instant = time_utils.resolve_instant(time_field)
        except time_utils.TimeParseError as e:
            return _error_block("endpoint_error", str(e), base_url)

        outcome = prometheus_client.query_instant(base_url, promql, instant,
                                                    timeout=settings.prometheus_timeout_seconds)
        resolved_time_range = {"instant": _iso(instant)}
    else:
        time_range = entry.get("time_range")
        if not time_range:
            return _error_block("endpoint_error", "Contract entry has no `time_range`.", base_url)
        try:
            resolved = time_utils.resolve_time_range(time_range)
        except time_utils.TimeParseError as e:
            return _error_block("endpoint_error", str(e), base_url)

        step_seconds, widened = time_utils.cap_step_for_max_points(
            resolved["start"], resolved["end"], resolved["step_seconds"],
            max_points=settings.max_points_per_series,
        )
        outcome = prometheus_client.query_range(
            base_url, promql, resolved["start"], resolved["end"], step_seconds,
            timeout=settings.prometheus_timeout_seconds,
        )
        resolved_time_range = {
            "start": _iso(resolved["start"]),
            "end": _iso(resolved["end"]),
            "step_seconds": step_seconds,
            "step_widened": widened,
        }

    if outcome.status != "success":
        return _error_block(outcome.status, outcome.error, base_url, resolved_time_range=resolved_time_range)

    normalized = normalizer.normalize_prometheus_result(
        outcome.raw_data, max_series=settings.max_series_per_result
    )
    return _success_block(normalized, base_url, resolved_time_range=resolved_time_range)


# ---- OpenSearch -----------------------------------------------------------------


def _execute_opensearch_entry(entry: dict, settings: Settings) -> dict:
    """Registry-governed OpenSearch execution. The index actually searched is
    ALWAYS resolved from the registry (by `pattern_id`, or by an exact match of
    a registered pattern string) -- an index named only by the LLM is never
    queried.

    Independent enforcement (defence in depth, not trust in the validator): the
    entry is re-checked here, immediately before the search, by the FULL guard
    against the live field view for its pattern (rebuilt from the process field
    cache) -- confirmed fields and operations, closed grammar, bounded sizes and
    aggregations, one bounded time window within the cap -- and the query must
    EQUAL the guard's canonical form. A query that fails, or that is not the
    canonical query the guard approves, is never sent. Unavailable field
    discovery also blocks execution (fail closed)."""
    from app.opensearch_catalog import guard, runtime
    from app.opensearch_catalog.context import Limits, OpenSearchContext, PatternContext
    from app.opensearch_catalog.field_catalog import build_field_view
    from app.opensearch_catalog.redaction import RedactionPolicy, safe_diagnostic
    from app.opensearch_catalog.registry import RegistryError
    from app.opensearch_catalog.results import all_shards_failed, normalize_search_response, response_metadata

    base_url = settings.opensearch_url
    dsl_body = entry.get("query")
    if not dsl_body or not isinstance(dsl_body, dict):
        return _error_block("endpoint_error", "Contract entry has no usable DSL object in `query`.", base_url)

    pattern_id, index = entry.get("pattern_id"), entry.get("index")
    if not pattern_id and not index:
        return _error_block("endpoint_error",
                            "Contract entry has no `pattern_id` or `index` (required for OpenSearch results).", base_url)

    policy = RedactionPolicy(mask_usernames=settings.opensearch_redact_usernames,
                             ip_mode=settings.opensearch_redact_ip_mode)
    try:
        registry = runtime.get_registry(settings)
    except RegistryError as exc:
        return _error_block("endpoint_error", f"OpenSearch registry unavailable: {safe_diagnostic(str(exc), policy)}",
                            base_url)

    if pattern_id:
        pattern = registry.get(str(pattern_id))
    else:  # legacy shape: only an index naming exactly one registered pattern is accepted
        pattern = next((p for p in registry.enabled_patterns if isinstance(index, str) and p.index_pattern == index),
                       None)
    if pattern is None or not pattern.enabled:
        return _error_block("not_executed",
                            "The index/pattern is not an approved, enabled OpenSearch source; nothing was queried.",
                            base_url)
    if isinstance(index, str) and pattern_id and index != pattern.index_pattern:
        return _error_block("not_executed", "`index` does not match the approved pattern; nothing was queried.",
                            base_url)

    limits = Limits.from_settings(settings)
    rejected = "Analysis rejected before execution" if entry.get("analysis") is not None \
        else "Query rejected before execution"
    # 1. Field-agnostic rules first (no network needed to reject an obviously unsafe body).
    problem = guard.structural_check(dsl_body, limits)
    if problem:
        return _error_block("not_executed", f"{rejected}: {problem}.", base_url,
                            resolved_time_range=guard.extract_window(dsl_body, pattern.time_field))

    # 2. The full guard against the live field view, exactly as the validator ran it.
    view = build_field_view(pattern, runtime.get_field_cache(settings).get(pattern))
    pc = PatternContext(entry=pattern, view=view)
    if not pc.usable:
        return _error_block("not_executed", "Live field discovery is unavailable for this index family, so the "
                                            "query could not be re-checked and was not run.", base_url,
                            resolved_time_range=guard.extract_window(dsl_body, pattern.time_field))
    ctx = OpenSearchContext(patterns={pattern.pattern_id: pc}, limits=limits)
    check = guard.check_entry({**entry, "pattern_id": pattern.pattern_id}, ctx)
    if not check.ok:
        return _error_block("not_executed", f"{rejected} ({check.code}): {safe_diagnostic(check.reason, policy)}",
                            base_url, resolved_time_range=guard.extract_window(dsl_body, pattern.time_field))
    if check.query != dsl_body or check.analysis != entry.get("analysis"):
        return _error_block("not_executed", f"{rejected}: it is not the canonical query the guard approves (it was "
                                            f"changed or never canonicalised).", base_url,
                            resolved_time_range=check.window)
    window = check.window

    if entry.get("analysis") is not None:
        return _execute_opensearch_analysis(entry, pattern, view, ctx, window, settings, policy, limits, base_url)

    outcome = opensearch_client.search(
        base_url, pattern.index_pattern, check.query,
        timeout=max(settings.opensearch_timeout_seconds, limits.search_timeout_seconds),
        auth=settings.opensearch_auth,
    )

    if outcome.status not in ("success", "empty_result"):
        return _error_block(outcome.status, safe_diagnostic(outcome.error, policy), base_url,
                            resolved_time_range=window)

    raw = outcome.raw_body or {}
    if all_shards_failed(raw):
        meta = response_metadata(raw, policy)
        block = _error_block("endpoint_error", "Every shard failed; no results are available.", base_url,
                             resolved_time_range=window)
        block["metadata"] = meta
        return block

    if outcome.status == "empty_result":
        meta = response_metadata(raw, policy)
        meta["query_window"] = window
        meta["count"] = {"value": 0, "relation": "eq", "exact": True}
        note = _text_search_note(check.query)
        if note:
            meta["text_search_note"] = note
        return {
            "execution_status": "empty_result",
            "result_type": None,
            "endpoint": base_url,
            "fetched_at": _now_iso(),
            "resolved_time_range": window,
            "metadata": meta,
        }

    normalized = normalize_search_response(
        raw, entry=pattern, query=dsl_body, policy=policy,
        max_series=settings.max_series_per_result, max_hits=settings.max_hits_per_result,
    )
    normalized.metadata["query_window"] = window
    block = _success_block(normalized, base_url, resolved_time_range=window)
    # A count-only query (size 0) legitimately returns no rows; the answer is the
    # total in metadata.count. Only "matched nothing" is an empty result.
    if block["execution_status"] == "empty_result" and ((normalized.metadata.get("count") or {}).get("value") or 0) > 0:
        block["execution_status"] = "success"
    if block["execution_status"] == "empty_result":
        note = _text_search_note(check.query)
        if note and isinstance(block.get("metadata"), dict):
            block["metadata"]["text_search_note"] = note
    return block


def _text_search_note(query: dict) -> str | None:
    """For a free-text search that matched nothing: say plainly what was searched and
    why a document describing the same thing may still exist, so an empty answer is
    never mistaken for "it did not happen". None when the query has no text search."""
    from app.opensearch_catalog.text_forms import phrase_variants, text_search_phrases

    shown: list[str] = []
    for p in text_search_phrases(query):
        if not any(p in phrase_variants(s) for s in shown):
            shown.append(p)
    if not shown:
        return None
    searched = ", ".join(repr(s) for s in shown)
    return (f"No document in this window contains the searched text {searched}, including its other "
            f"tenses and plurals. Text search matches whole words in the given order, so a message that "
            f"describes the same thing in different words is not found: try other or fewer words, or a "
            f"wider time window, before concluding that nothing happened.")


def _execute_opensearch_analysis(entry: dict, pattern, view, ctx, window: dict | None, settings: Settings,
                                 policy, limits, base_url: str) -> dict:
    """A multi-step analysis (app/opensearch_catalog/analysis.py). The caller has
    already re-validated the entry with the full guard against the live field view
    (`view`, `ctx`) and confirmed the query and analysis parameters are exactly the
    canonical ones. Every query -- the model's and each follow-up Python builds --
    then goes through `runner`, which checks it with the guard again and sends it
    through opensearch_client, so this module stays the only caller of OpenSearch.
    The combined answer is normalised through results.py like any other result."""
    from app.opensearch_catalog import analysis as os_analysis, guard
    from app.opensearch_catalog.redaction import redact_value, safe_diagnostic
    from app.opensearch_catalog.results import all_shards_failed, normalize_search_response

    atype = (entry.get("analysis") or {}).get("type") if isinstance(entry.get("analysis"), dict) else None
    purposes: list[str] = []
    timeout = max(settings.opensearch_timeout_seconds, limits.search_timeout_seconds)

    def runner(dsl: dict, purpose: str) -> dict:
        follow = {"status": "ok", "data_source": "opensearch", "pattern_id": pattern.pattern_id, "query": dsl}
        checked = guard.check_entry(follow, ctx, followup_of=atype)
        if not checked.ok:
            raise os_analysis.AnalysisError(
                "not_executed", f"{purpose}: query rejected before execution ({checked.code}): "
                                f"{safe_diagnostic(checked.reason, policy)}")
        purposes.append(purpose)
        outcome = opensearch_client.search(base_url, pattern.index_pattern, checked.query, timeout=timeout,
                                           auth=settings.opensearch_auth)
        if outcome.status not in ("success", "empty_result"):
            raise os_analysis.AnalysisError(outcome.status, f"{purpose}: {safe_diagnostic(outcome.error, policy)}")
        raw = outcome.raw_body or {}
        if all_shards_failed(raw):
            raise os_analysis.AnalysisError("endpoint_error", f"{purpose}: every shard failed; no results are available")
        return raw

    try:
        res = os_analysis.run_analysis(entry, pattern, view, limits, runner, window or {})
    except os_analysis.AnalysisError as exc:
        block = _error_block(exc.status, exc.message, base_url, resolved_time_range=window)
        block["analysis"] = {"type": atype, "queries_executed": len(purposes), "query_purposes": purposes,
                             "completed": False}
        return block

    normalized = normalize_search_response(
        res.raw, entry=pattern, query=res.query, policy=policy,
        max_series=settings.max_series_per_result, max_hits=settings.max_hits_per_result,
    )
    if res.row_annotations is not None and normalized.result_type == "hits":
        for hit, note in zip(normalized.hits, res.row_annotations):
            hit.analysis = note
    normalized.metadata["query_window"] = window
    block = _success_block(normalized, base_url, resolved_time_range=window)
    # An analysis that had data to work on and found nothing to report ("no switch is
    # missing", "no shortfall") has answered the question: that is a success.
    if block["execution_status"] == "empty_result" and res.has_input:
        block["execution_status"] = "success"
    summary = {**res.summary, "queries_executed": len(purposes), "query_purposes": purposes,
               "warnings": res.warnings, "completed": True}
    block["analysis"] = redact_value(summary, policy)
    return block


def _best_effort_extract_time_range(dsl_body: dict) -> dict | None:
    """Best-effort, display-only extraction of a `range` clause on a date
    field from the DSL body, so the frontend can show an absolute resolved
    window for OpenSearch results too -- OpenSearch itself resolves date
    math (now-1h, now/d, ...) server-side, so this is never required for
    correct execution, only for a nicer response. Returns None if no
    recognizable date range clause is found; never raises."""
    try:
        candidates = _find_range_clauses(dsl_body)
        for field_name, bounds in candidates:
            if field_name in ("@timestamp", "Timestamp"):
                gte, lte = bounds.get("gte"), bounds.get("lte")
                if gte and lte:
                    start = time_utils.resolve_relative_time(gte) if isinstance(gte, str) and gte.startswith("now") else None
                    end = time_utils.resolve_relative_time(lte) if isinstance(lte, str) and lte.startswith("now") else None
                    return {
                        "from_expression": gte,
                        "to_expression": lte,
                        "start": _iso(start) if start else None,
                        "end": _iso(end) if end else None,
                    }
    except Exception:  # noqa: BLE001 -- purely cosmetic extraction, never fatal
        return None
    return None


def _find_range_clauses(node: Any) -> list[tuple[str, dict]]:
    found = []
    if isinstance(node, dict):
        if "range" in node and isinstance(node["range"], dict):
            for field_name, bounds in node["range"].items():
                if isinstance(bounds, dict):
                    found.append((field_name, bounds))
        for value in node.values():
            found.extend(_find_range_clauses(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_find_range_clauses(item))
    return found


# ---- shared helpers ---------------------------------------------------------------


def _success_block(normalized: normalizer.NormalizedResult, endpoint: str,
                    resolved_time_range: dict | None) -> dict:
    return {
        "execution_status": "success" if normalized.count > 0 else "empty_result",
        "resolved_time_range": resolved_time_range,
        "endpoint": endpoint,
        "fetched_at": _now_iso(),
        **normalized.to_dict(),
    }


def _error_block(execution_status: str, error: str | None, endpoint: str,
                  resolved_time_range: dict | None = None) -> dict:
    return {
        "execution_status": execution_status,
        "error": error,
        "result_type": None,
        "endpoint": endpoint,
        "fetched_at": _now_iso(),
        "resolved_time_range": resolved_time_range,
    }


def _now_iso() -> str:
    return _iso(datetime.now(timezone.utc))


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
