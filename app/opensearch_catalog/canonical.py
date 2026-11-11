"""
canonical.py

Pipeline hook: applies the guard to every OpenSearch entry of a Generator
contract BEFORE validation, so downstream stages (validator, executor) see the
canonical form -- registry-resolved `index`, expanded named conditions,
documented safe defaults filled in.

  guard ok                      entry.query / entry.index (and, for an
                                analysis question, entry.analysis) replaced
                                by the canonical forms; `guard_notes` records
                                which defaults were applied and any warnings
  window_exceeds_max            entry becomes declined /
                                parameter_requires_clarification carrying the
                                guard's user message (the window is NEVER
                                shortened or rewritten)
  any other rejection           entry left UNCHANGED; the validator re-runs the
                                guard and reports the same rejection (so the
                                existing validation-failure handling applies)
"""
from __future__ import annotations

import logging
from typing import Any

from app.opensearch_catalog import correlation, guard
from app.opensearch_catalog.context import OpenSearchContext

logger = logging.getLogger(__name__)

_RUNNABLE = {"ok", "panic_mode_best_effort"}


def _canonicalise_entry(entry: dict, ctx: OpenSearchContext) -> dict:
    if not isinstance(entry, dict) or entry.get("status") not in _RUNNABLE:
        return entry
    if (entry.get("data_source") or "").strip().lower() != "opensearch":
        return entry
    result = guard.check_entry(entry, ctx)
    if result.ok:
        out = {**entry, "query": result.query, "index": result.index, "pattern_id": result.pattern_id}
        if result.analysis is not None:
            # canonical analysis parameters: documented defaults filled in, nothing the model wrote changed
            out["analysis"] = result.analysis
        notes = {"applied": list(result.applied), "warnings": list(result.warnings)}
        if notes["applied"] or notes["warnings"]:
            out["guard_notes"] = notes
        return out
    if result.code == guard.CODE_WINDOW_EXCEEDS_MAX:
        logger.info("OpenSearch window exceeds the permitted maximum: %s", result.reason)
        return {
            # keep the envelope of a single-mode contract (shape repair runs first)
            **({"mode": entry["mode"]} if "mode" in entry else {}),
            "status": "declined",
            "reason": "parameter_requires_clarification",
            "explanation": result.user_message or result.reason,
            "clarification": result.user_message or "Please narrow the time range.",
            "data_source": "opensearch",
        }
    return entry


def canonicalise_contract(contract: Any, ctx: OpenSearchContext | None) -> Any:
    """Never raises; a non-dict contract or a missing context is returned as-is
    (the validator then fails closed for any OpenSearch entry)."""
    if ctx is None or not isinstance(contract, dict):
        return contract
    try:
        if isinstance(contract.get("results"), list):
            if correlation.needs_tolerance(contract):
                # "near" has no project-defined threshold: ask, never invent one.
                msg = correlation.tolerance_clarification(ctx.limits.analysis_context_max_minutes * 60)
                logger.info("Correlation request without a tolerance: asking the user")
                return {"mode": "single", "status": "declined", "reason": "parameter_requires_clarification",
                        "explanation": "Comparing events across two index families needs a stated time tolerance, "
                                       "and none was given. " + msg,
                        "clarification": msg, "data_source": "opensearch"}
            return {**contract, "results": [_canonicalise_entry(e, ctx) for e in contract["results"]]}
        return _canonicalise_entry(contract, ctx)
    except Exception:  # noqa: BLE001 -- a bug here must not turn into an unchecked query
        logger.exception("OpenSearch canonicalisation failed; leaving the contract for the validator")
        return contract
