"""
analysis.py

Five questions from the mentor's list cannot be answered by ONE OpenSearch
query. Each has a small, deterministic recipe here:

  absence              "Which switches stopped sending heartbeats?"
                       list who reported over the query's window (the baseline),
                       list who reported in the last few minutes, subtract.
                       A query can only find documents that exist, never "missing".
  context_window       "What did hmscd report before and after the link-down event?"
                       find the event(s) first, then fetch the minutes around each.
  ingest_lag           "How long after creation were events ingested?"
                       ingest time minus event time per returned row; doing the
                       subtraction inside OpenSearch would need a script (forbidden).
  text_ratio_shortfall "Fabric-manager messages with fewer switches available than expected"
                       a message like '30/32 switches available' needs 30 < 32, which
                       a text search cannot test; returned rows are parsed here.
  recurrence           "Which ports have recurring faults?"
                       counts in how many SEPARATE intervals each entity appears,
                       because one fault re-sent every hour looks like many events.

Rules this module keeps (they come from the rest of the package):
  * The model writes only the ordinary query plus a few parameters in `analysis`;
    guard.check_entry() has validated both (reject, never repair) before any of
    this runs.
  * Every follow-up query is BUILT HERE FROM THE VALIDATED QUERY and is then
    checked again by guard.check_entry() inside the executor's `runner` before it
    is sent. Only plain scalar values (string, number, boolean) are ever copied
    out of a returned document into a follow-up query.
  * Field names, heartbeat settings, regexes and intervals come from the registry
    (`analyses` block), never from this file.
  * This module never talks to OpenSearch. The executor passes in `runner`, which
    is the only path to the cluster, so the executor stays the only caller.
  * Output is a `_search`-shaped body that the executor normalises through
    results.py, so redaction, count exactness, severity assessment and the other
    metadata apply to analysis rows exactly as to ordinary rows.
"""
from __future__ import annotations

import copy
import math
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app.opensearch_catalog.context import Limits
from app.opensearch_catalog.field_catalog import OP_TERM, PatternFieldView
from app.opensearch_catalog.registry import (
    ANALYSIS_ABSENCE, ANALYSIS_CONTEXT_WINDOW, ANALYSIS_INGEST_LAG, ANALYSIS_RECURRENCE,
    ANALYSIS_TEXT_RATIO_SHORTFALL, PatternEntry, parse_short_duration_seconds,
)
from app.opensearch_catalog.severity import _lookup

# (dsl, purpose) -> raw OpenSearch response body. Raises AnalysisError.
Runner = Callable[[dict, str], dict]

MAX_TEXT_SCANNED_CHARS = 4000


class AnalysisError(Exception):
    """A query of the analysis could not be run. `status` is an execution_status value."""

    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status, self.message = status, message


@dataclass
class AnalysisResult:
    raw: dict                       # a _search-shaped body for results.normalize_search_response
    query: dict                     # the query whose aggregation shape describes `raw`
    summary: dict                   # the `analysis` block (type, counts, parameters, explanation)
    has_input: bool                 # the analysis had data to work on (an empty answer is then real)
    row_annotations: list | None = None    # parallel to raw hits: per-row `analysis` annotation
    warnings: list[str] = field(default_factory=list)


# ---- small helpers -----------------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime | None:
    """An ISO-8601 string or epoch milliseconds -> aware datetime; anything else -> None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _short(seconds: float) -> str:
    seconds = int(round(seconds))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def _hits(body: dict) -> list[dict]:
    return [h for h in ((body.get("hits") or {}).get("hits") or []) if isinstance(h, dict)]


def _total(body: dict) -> tuple[int | None, str]:
    """(value, relation); (None, 'unknown') when the response has no total
    (track_total_hits: false) -- an absent total is never reported as 0 or exact."""
    total = (body.get("hits") or {}).get("total")
    if isinstance(total, dict) and isinstance(total.get("value"), int):
        return total["value"], total.get("relation") or "eq"
    if isinstance(total, int) and not isinstance(total, bool):
        return total, "eq"
    return None, "unknown"


def _merged_envelope(bodies: list[dict]) -> dict:
    """took / timed_out / _shards over every query of the analysis, so a partial
    follow-up is reported as partial in the final metadata."""
    shards = {"total": 0, "successful": 0, "skipped": 0, "failed": 0}
    failures: list = []
    for b in bodies:
        sh = b.get("_shards") or {}
        for k in shards:
            v = sh.get(k)
            shards[k] += v if isinstance(v, int) else 0
        failures.extend(sh.get("failures") or [])
    if failures:
        shards["failures"] = failures
    return {"took": sum(b.get("took") or 0 for b in bodies if isinstance(b.get("took"), int)),
            "timed_out": any(bool(b.get("timed_out")) for b in bodies),
            "terminated_early": any(bool(b.get("terminated_early")) for b in bodies),
            "_shards": shards}


def _scalar(value: Any, limits: Limits) -> bool:
    """Only a plain scalar copied out of a document may become part of a follow-up query."""
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return math.isfinite(value)
    return isinstance(value, str) and 0 < len(value) <= limits.max_term_chars


def _root_time_ranges(query: dict, time_field: str) -> list[dict]:
    """The bounds objects of the range on the time field among the ROOT bool's
    filter/must children (where the guard requires the single time range to be)."""
    root = (query.get("query") or {}).get("bool") or {}
    found = []
    for key in ("filter", "must"):
        items = root.get(key)
        for item in (items if isinstance(items, list) else [items] if isinstance(items, dict) else []):
            r = item.get("range") if isinstance(item, dict) else None
            if isinstance(r, dict) and isinstance(r.get(time_field), dict):
                found.append(r[time_field])
    return found


# ---- absence -----------------------------------------------------------------------------------------


def _normalise_entity(name: Any, cfg: dict) -> str:
    out = str(name)
    if cfg.get("normalise_lowercase"):
        out = out.lower()
    suffix = cfg.get("normalise_strip_suffix_regex")
    if suffix:
        out = re.sub(suffix, "", out)
    return out


def _terms_buckets(body: dict, name: str) -> tuple[list[dict], int]:
    agg = (body.get("aggregations") or {}).get(name) or {}
    buckets = [b for b in (agg.get("buckets") or []) if isinstance(b, dict) and "key" in b]
    other = agg.get("sum_other_doc_count")
    return buckets, other if isinstance(other, int) else 0


def run_absence(entry: dict, pattern: PatternEntry, limits: Limits, runner: Runner, window: dict) -> AnalysisResult:
    cfg = pattern.analysis(ANALYSIS_ABSENCE)
    query = entry["query"]
    (agg_name, agg_spec), = query["aggs"].items()
    params = entry["analysis"]
    recent = parse_short_duration_seconds(params["recent_window"])

    baseline_body = runner(query, "baseline: who reported during the query window")
    recent_q = copy.deepcopy(query)
    bounds = _root_time_ranges(recent_q, pattern.time_field)
    if len(bounds) != 1:   # the guard guarantees exactly one; fail closed if not
        raise AnalysisError("not_executed", "absence: the query has no single time range to narrow")
    hi_key = "lte" if "lte" in bounds[0] else "lt"
    if window.get("to_expression") == "now":
        new_bounds = {"gte": f"now-{_short(recent)}", hi_key: "now"}
    else:
        end = _parse_time(window.get("end"))
        if end is None:
            raise AnalysisError("not_executed", "absence: the end of the query window could not be read")
        new_bounds = {"gte": _iso(end - timedelta(seconds=recent)), hi_key: _iso(end)}
    bounds[0].clear()
    bounds[0].update(new_bounds)
    recent_body = runner(recent_q, f"recent: who reported in the last {params['recent_window']}")

    baseline, other_base = _terms_buckets(baseline_body, agg_name)
    recent_buckets, other_recent = _terms_buckets(recent_body, agg_name)
    recent_set = {_normalise_entity(b["key"], cfg) for b in recent_buckets}
    seen: dict[str, dict] = {}
    for b in baseline:
        seen.setdefault(_normalise_entity(b["key"], cfg), b)
    missing = sorted(k for k in seen if k not in recent_set)
    out_buckets = [{"key": seen[k]["key"], "doc_count": int(seen[k].get("doc_count") or 0)} for k in missing]

    warnings = []
    complete = not (other_base or other_recent)
    if not complete:
        warnings.append("incomplete_entity_list: more entities exist than the analysis limit, so the missing list may "
                        "be incomplete or contain false alarms")
    if seen and not recent_set:
        warnings.append("no_recent_reports: nothing at all reported in the recent window; this may be an ingestion "
                        "outage rather than every entity being down")
    base_total, base_rel = _total(baseline_body)
    raw = {**_merged_envelope([baseline_body, recent_body]),
           "hits": {"total": {"value": base_total, "relation": base_rel}, "hits": []},
           "aggregations": {agg_name: {"doc_count_error_upper_bound": 0, "sum_other_doc_count": 0,
                                       "buckets": out_buckets}}}
    summary = {
        "type": ANALYSIS_ABSENCE,
        "entity_field": agg_spec["terms"]["field"],
        "baseline_window": {"from": window.get("from_expression"), "to": window.get("to_expression")},
        "recent_window": params["recent_window"],
        "baseline_entities": len(seen),
        "recent_entities": len(recent_set),
        "missing_count": len(missing),
        "complete": complete,
        "heartbeat_interval_seconds": cfg["heartbeat_interval_seconds"],
        "dead_after_intervals": cfg["dead_after_intervals"],
        "threshold_verified": bool(cfg["verified"]),
        "explanation": "Entities that reported during the query window but not during the recent window. Names are "
                       "compared after the registry's normalisation (e.g. an ASIC suffix is ignored), and doc_count is "
                       "the entity's document count in the baseline window.",
    }
    return AnalysisResult(raw=raw, query={"aggs": {agg_name: {"terms": agg_spec["terms"]}}}, summary=summary,
                          has_input=bool(seen), warnings=warnings)


# ---- context window ------------------------------------------------------------------------------------


def _entity_exact_path(pattern: PatternEntry, view: PatternFieldView, role: str) -> tuple[str, str] | None:
    """(source path to read the value from, effective exact-match path to filter on)."""
    for p in pattern.role_paths(role):
        info = view.allowed_fields.get(p)
        if info is not None and OP_TERM in info.ops:
            return p, info.ops[OP_TERM]
    return None


def run_context_window(entry: dict, pattern: PatternEntry, view: PatternFieldView, limits: Limits,
                       runner: Runner) -> AnalysisResult:
    cfg = pattern.analysis(ANALYSIS_CONTEXT_WINDOW)
    params = entry["analysis"]
    tf = pattern.time_field
    before = timedelta(seconds=parse_short_duration_seconds(params["before"]))
    after = timedelta(seconds=parse_short_duration_seconds(params["after"]))
    scope_filter = params.get("context_filter")
    entity = None if scope_filter is not None else _entity_exact_path(pattern, view, cfg["entity_role"])

    anchor_body = runner(entry["query"], "anchor: find the event(s) to look around")
    anchors = _hits(anchor_body)[:limits.analysis_anchor_limit]
    bodies = [anchor_body]
    merged: list[dict] = []
    notes: list[dict] = []
    per_anchor: list[dict] = []
    warnings: list[str] = []
    truncated_any = False
    for idx, anchor in enumerate(anchors):
        src = anchor.get("_source") or {}
        ts = _parse_time(_lookup(src, tf))
        if ts is None:
            warnings.append(f"anchor_{idx}_skipped: the event's {tf} could not be read")
            continue
        if scope_filter is not None:
            scope = copy.deepcopy(scope_filter)
            scope_text = "context_filter"
        else:
            if entity is None:
                raise AnalysisError("not_executed", "context_window: no exact-match entity field to scope the window")
            value = _lookup(src, entity[0])
            if not _scalar(value, limits):
                warnings.append(f"anchor_{idx}_skipped: the event has no plain {entity[0]} value to scope its window")
                continue
            scope = {"term": {entity[1]: value}}
            scope_text = f"same {entity[0]}"
        follow = {"query": {"bool": {"filter": [{"range": {tf: {"gte": _iso(ts - before), "lte": _iso(ts + after)}}},
                                                scope]}},
                  "size": limits.max_hits_size, "sort": [{tf: {"order": "asc"}}],
                  "_source": list(pattern.output_projection), "track_total_hits": True}
        body = runner(follow, f"context: {params['before']} before to {params['after']} after event {idx}")
        bodies.append(body)
        rows = _hits(body)
        total, rel = _total(body)
        truncated = rel == "gte" or (total is not None and total > len(rows))
        truncated_any = truncated_any or truncated
        if truncated:
            warnings.append(f"anchor_{idx}_truncated: more rows exist in this window than the row limit "
                            f"({limits.max_hits_size})")
        n_before = n_after = 0
        anchor_key = (anchor.get("_index"), anchor.get("_id"))
        for h in rows:
            h_ts = _parse_time(_lookup(h.get("_source") or {}, tf))
            if anchor_key[1] is not None and (h.get("_index"), h.get("_id")) == anchor_key:
                position = "anchor"
            elif h_ts is not None and h_ts < ts:
                position, n_before = "before", n_before + 1
            else:
                position, n_after = "after", n_after + 1
            merged.append(h)
            notes.append({"anchor": idx, "position": position,
                          "offset_seconds": round((h_ts - ts).total_seconds(), 3) if h_ts else None})
        per_anchor.append({"anchor": idx, "timestamp": _iso(ts), "rows_before": n_before, "rows_after": n_after,
                           "truncated": truncated, "scope": scope_text})
    if not anchors:
        warnings.append("no_anchor_event: the first query found no matching event, so there is nothing to look around")
    raw = {**_merged_envelope(bodies),
           "hits": {"total": {"value": len(merged), "relation": "gte" if truncated_any else "eq"}, "hits": merged}}
    summary = {
        "type": ANALYSIS_CONTEXT_WINDOW, "before": params["before"], "after": params["after"],
        "scope": "context_filter" if scope_filter is not None else f"same {cfg['entity_role']}",
        "anchors_found": len(anchors), "anchors_expanded": len(per_anchor), "anchors": per_anchor,
        "explanation": "Rows around each matching event, oldest first; each row's analysis.position says whether it "
                       "came before, after, or is the event itself.",
    }
    return AnalysisResult(raw=raw, query={}, summary=summary, has_input=bool(anchors), row_annotations=notes,
                          warnings=warnings)


# ---- ingest lag -----------------------------------------------------------------------------------------


def run_ingest_lag(entry: dict, pattern: PatternEntry, limits: Limits, runner: Runner) -> AnalysisResult:
    cfg = pattern.analysis(ANALYSIS_INGEST_LAG)
    body = runner(entry["query"], "rows to measure")
    rows = _hits(body)
    lags: list[float] = []
    notes: list[dict | None] = []
    for h in rows:
        src = h.get("_source") or {}
        ingest = _parse_time(_lookup(src, pattern.time_field))
        event = next((t for t in (_parse_time(_lookup(src, f)) for f in cfg["event_time_fields"]) if t), None)
        if ingest and event:
            lag = (ingest - event).total_seconds()
            lags.append(lag)
            notes.append({"lag_seconds": round(lag, 3)})
        else:
            notes.append(None)
    total, rel = _total(body)
    warnings: list[str] = []
    summary: dict[str, Any] = {
        "type": ANALYSIS_INGEST_LAG, "event_time_fields": list(cfg["event_time_fields"]),
        "rows_returned": len(rows), "rows_measured": len(lags),
        "documents_matched": {"value": total, "relation": rel},
        "event_time_verified": bool(cfg["verified"]),
        "explanation": f"{pattern.time_field} (ingest time) minus the event's own time, measured on the returned rows "
                       f"only (a sample, newest first), not on every matching event.",
    }
    if lags:
        ordered = sorted(lags)
        summary.update({
            "min_seconds": round(ordered[0], 3),
            "median_seconds": round(statistics.median(ordered), 3),
            "p90_seconds": round(ordered[max(0, math.ceil(0.9 * len(ordered)) - 1)], 3),
            "max_seconds": round(ordered[-1], 3),
            "mean_seconds": round(statistics.fmean(ordered), 3),
            "negative_lag_rows": sum(1 for x in ordered if x < 0),
        })
        if summary["negative_lag_rows"]:
            warnings.append("negative_lag: some rows were ingested before their event time, so the event clock is "
                            "ahead of the ingest clock for them")
    else:
        warnings.append("no_lag_measurable: no returned row had both an ingest time and an event time")
    return AnalysisResult(raw=body, query={}, summary=summary, has_input=bool(rows), row_annotations=notes,
                          warnings=warnings)


# ---- "n/m available" shortfall -----------------------------------------------------------------------------


def run_text_ratio_shortfall(entry: dict, pattern: PatternEntry, view: PatternFieldView, limits: Limits,
                             runner: Runner) -> AnalysisResult:
    cfg = pattern.analysis(ANALYSIS_TEXT_RATIO_SHORTFALL)
    regex = re.compile(cfg["regex"])
    compare = cfg["compare"]
    text_paths = [p for p in pattern.role_paths(cfg["text_role"]) if p in view.allowed_fields]
    body = runner(entry["query"], "rows whose message may contain an n/m count")
    rows = _hits(body)
    flagged, notes = [], []
    parsed = 0
    for h in rows:
        src = h.get("_source") or {}
        text = next((v for v in (_lookup(src, p) for p in text_paths) if isinstance(v, str)), None)
        m = regex.search(text[:MAX_TEXT_SCANNED_CHARS]) if text else None
        if not m:
            continue
        parsed += 1
        n, of = int(m.group(1)), int(m.group(2))
        if (n < of) if compare == "lt" else (n > of) if compare == "gt" else (n != of):
            flagged.append(h)
            notes.append({"available": n, "expected": of})
    total, rel = _total(body)
    complete = rel == "eq" and total is not None and total <= len(rows)
    warnings = [] if complete else [
        "scan_limited: more matching rows exist than were scanned (the row limit), so some shortfalls may be missing; "
        "narrow the time range to scan them all"]
    raw = {**_merged_envelope([body]),
           "hits": {"total": {"value": len(flagged), "relation": "eq" if complete else "gte"}, "hits": flagged}}
    summary = {
        "type": ANALYSIS_TEXT_RATIO_SHORTFALL, "compare": compare,
        "rows_scanned": len(rows), "rows_matching_pattern": parsed, "rows_flagged": len(flagged),
        "documents_matched": {"value": total, "relation": rel}, "scan_complete": complete,
        "pattern_verified": bool(cfg["verified"]),
        "explanation": "Rows whose message holds an 'n/m' count where the first number is "
                       + {"lt": "smaller than", "gt": "larger than", "ne": "different from"}[compare]
                       + " the second. Rows without such a count are ignored.",
    }
    return AnalysisResult(raw=raw, query={}, summary=summary, has_input=bool(rows), row_annotations=notes,
                          warnings=warnings)


# ---- recurrence --------------------------------------------------------------------------------------------


def run_recurrence(entry: dict, pattern: PatternEntry, limits: Limits, runner: Runner, window: dict) -> AnalysisResult:
    cfg = pattern.analysis(ANALYSIS_RECURRENCE)
    query = entry["query"]
    (agg_name, agg_spec), = query["aggs"].items()
    (hist_name, _), = agg_spec["aggs"].items()
    body = runner(query, f"entities per {cfg['interval']} interval")
    agg = (body.get("aggregations") or {}).get(agg_name) or {}
    ranked = []
    for b in agg.get("buckets") or []:
        if not isinstance(b, dict) or "key" not in b:
            continue
        sub = (b.get(hist_name) or {}).get("buckets") or []
        active = sum(1 for sb in sub if isinstance(sb, dict) and int(sb.get("doc_count") or 0) > 0)
        ranked.append({"key": b.get("key_as_string", b["key"]), "doc_count": int(b.get("doc_count") or 0),
                       "active_intervals": {"value": active}})
    ranked.sort(key=lambda r: (-r["active_intervals"]["value"], -r["doc_count"]))
    warnings: list[str] = []
    if int(agg.get("sum_other_doc_count") or 0) > 0:
        # OpenSearch picks the terms buckets by document count BEFORE the ranking by
        # active intervals happens here, so an entity that recurs with few documents
        # can be outside the returned set while a one-off burst is inside it.
        warnings.append(
            f"ranking_limited: only the {len(ranked)} entities with the most documents were ranked "
            f"({agg.get('sum_other_doc_count')} documents belong to other entities); an entity that recurs "
            f"regularly but with few documents may be missing. Narrow the time range or filter to see them")
    raw = {**_merged_envelope([body]), "hits": body.get("hits") or {"total": {"value": 0, "relation": "eq"}, "hits": []},
           "aggregations": {agg_name: {"doc_count_error_upper_bound": agg.get("doc_count_error_upper_bound", 0),
                                       "sum_other_doc_count": agg.get("sum_other_doc_count", 0), "buckets": ranked}}}
    interval_s = parse_short_duration_seconds(cfg["interval"])
    summary = {
        "type": ANALYSIS_RECURRENCE, "entity_field": agg_spec["terms"]["field"], "interval": cfg["interval"],
        "intervals_in_window": math.ceil(window["seconds"] / interval_s) if window.get("seconds") else None,
        "ranked_by": "active_intervals, then doc_count", "interval_verified": bool(cfg["verified"]),
        "explanation": "doc_count is the number of documents; metrics.active_intervals is in how many separate "
                       f"{cfg['interval']} intervals the entity appeared. A high active_intervals means it keeps coming "
                       "back; a high doc_count alone may be one burst or re-sent duplicates.",
    }
    return AnalysisResult(raw=raw, query={"aggs": {agg_name: {"terms": agg_spec["terms"]}}}, summary=summary,
                          has_input=bool(ranked), warnings=warnings)


# ---- dispatch --------------------------------------------------------------------------------------------------


def run_analysis(entry: dict, pattern: PatternEntry, view: PatternFieldView, limits: Limits, runner: Runner,
                 window: dict) -> AnalysisResult:
    """`entry` is the canonical, validated contract entry (query + analysis)."""
    atype = (entry.get("analysis") or {}).get("type")
    if pattern.analysis(atype) is None:
        raise AnalysisError("not_executed", f"analysis {atype!r} is not configured for {pattern.pattern_id!r}")
    if atype == ANALYSIS_ABSENCE:
        return run_absence(entry, pattern, limits, runner, window)
    if atype == ANALYSIS_CONTEXT_WINDOW:
        return run_context_window(entry, pattern, view, limits, runner)
    if atype == ANALYSIS_INGEST_LAG:
        return run_ingest_lag(entry, pattern, limits, runner)
    if atype == ANALYSIS_TEXT_RATIO_SHORTFALL:
        return run_text_ratio_shortfall(entry, pattern, view, limits, runner)
    if atype == ANALYSIS_RECURRENCE:
        return run_recurrence(entry, pattern, limits, runner, window)
    raise AnalysisError("not_executed", f"unknown analysis {atype!r}")
