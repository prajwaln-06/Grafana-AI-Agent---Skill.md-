"""
results.py

Normalises a raw OpenSearch _search response for a REGISTERED index family into
the shared NormalizedResult shapes (hits / buckets / series), carrying the
metadata a reader needs to interpret it correctly:

  count exactness     hits.total value + relation (eq | gte) -> `exact`
  truncation          rows returned vs matched; terms buckets cut off
                      (sum_other_doc_count); series capped
  approximation       cardinality (estimate), terms doc-count error bound
  partial results     timed_out, shard failures (type + redacted/truncated
                      detail, never raw documents)
  query window        reported by the executor (the window that was asked for), kept
                      separate from `observed_range`: the earliest/latest timestamp
                      among the RETURNED rows or non-empty buckets only. It is not
                      the full range of data in OpenSearch (that would need another
                      query) and, with a row cap, may be narrower than the window.
  severity            per-row assessment incl. missing/conflicting fields
  redaction           what was masked

Every string that reaches a user passes through redaction; structured values
(timestamps, counts, severity vocab, aggregation keys of the response) are
preserved. Rows contain ONLY the registry's output projection, never _source.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.normalizer import (
    Bucket, Hit, NormalizedResult, Point, Series, _epoch_ms_to_iso, _iso, _legend_label_for, safe_float,
)
from app.opensearch_catalog.redaction import RedactionPolicy, RedactionStats, redact_text, redact_value, safe_diagnostic
from app.opensearch_catalog.registry import PatternEntry
from app.opensearch_catalog.severity import _lookup, assess_hit

MAX_SHARD_FAILURES_REPORTED = 5


# ---- response metadata -------------------------------------------------------------------------------


def shard_summary(raw: dict) -> dict:
    sh = raw.get("_shards") or {}
    return {k: sh.get(k) for k in ("total", "successful", "skipped", "failed") if k in sh}


def all_shards_failed(raw: dict) -> bool:
    sh = raw.get("_shards") or {}
    return bool(sh.get("failed")) and not sh.get("successful")


def _shard_failures(raw: dict, policy: RedactionPolicy) -> list[dict]:
    out = []
    for f in ((raw.get("_shards") or {}).get("failures") or [])[:MAX_SHARD_FAILURES_REPORTED]:
        reason = f.get("reason") if isinstance(f, dict) else None
        out.append({
            "index": f.get("index") if isinstance(f, dict) else None,
            "shard": f.get("shard") if isinstance(f, dict) else None,
            "type": reason.get("type") if isinstance(reason, dict) else None,
            "detail": safe_diagnostic(reason.get("reason") if isinstance(reason, dict) else None, policy),
        })
    return out


def response_metadata(raw: dict, policy: RedactionPolicy) -> dict:
    shards = shard_summary(raw)
    reasons = []
    if raw.get("timed_out"):
        reasons.append("timed_out")
    if shards.get("failed"):
        reasons.append("shard_failures")
    if raw.get("terminated_early"):
        reasons.append("terminated_early")
    meta: dict[str, Any] = {
        "source": "opensearch",
        "took_ms": raw.get("took"),
        "timed_out": bool(raw.get("timed_out")),
        "shards": shards,
        "partial": bool(reasons),
        "partial_reasons": reasons,
    }
    if shards.get("failed"):
        meta["shard_failures"] = _shard_failures(raw, policy)
        meta["shard_failures_reported"] = min(len(((raw.get("_shards") or {}).get("failures") or [])),
                                              MAX_SHARD_FAILURES_REPORTED)
    return meta


RELATION_UNKNOWN = "unknown"


def _total(raw: dict) -> tuple[int | None, str]:
    """(value, relation). relation is 'eq', 'gte', or 'unknown' when the response
    carries no total at all (the request set track_total_hits: false); an unknown
    total is reported as value None, never as 0 and never as exact."""
    total = (raw.get("hits") or {}).get("total")
    if isinstance(total, dict) and isinstance(total.get("value"), int):
        return total["value"], total.get("relation") or "eq"
    if isinstance(total, int) and not isinstance(total, bool):
        return total, "eq"
    return None, RELATION_UNKNOWN


# ---- helpers -------------------------------------------------------------------------------------------------


def _iter_buckets(buckets: Any):
    """terms/date_histogram -> list of dicts; filters (keyed) -> dict."""
    if isinstance(buckets, dict):
        for name, body in buckets.items():
            if isinstance(body, dict):
                yield name, body
    elif isinstance(buckets, list):
        for b in buckets:
            if isinstance(b, dict):
                yield b.get("key_as_string", b.get("key")), b


def _query_agg_kinds(node: Any, found: set[str] | None = None) -> set[str]:
    found = found if found is not None else set()
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("cardinality", "terms", "date_histogram", "filters", "min", "max", "avg", "sum", "value_count"):
                found.add(k)
            _query_agg_kinds(v, found)
    elif isinstance(node, list):
        for v in node:
            _query_agg_kinds(v, found)
    return found


_BUCKET_AGG_KINDS = ("terms", "date_histogram", "filters")


def _top_agg_kind(query: Any, name: str) -> str | None:
    """The aggregation type of top-level aggregation `name` in the query that was
    sent ('terms', 'date_histogram', ...), or None when the query does not say."""
    if not isinstance(query, dict):
        return None
    aggs = query.get("aggs") or query.get("aggregations")
    spec = aggs.get(name) if isinstance(aggs, dict) else None
    if not isinstance(spec, dict):
        return None
    return next((k for k in spec if k in _BUCKET_AGG_KINDS), None)


def _bucket(key: Any, body: dict, policy: RedactionPolicy, stats: RedactionStats, depth: int = 0) -> Bucket:
    subs: list[Bucket] = []
    metrics: dict[str, float | None] = {}
    for name, val in body.items():
        if name in ("key", "key_as_string", "doc_count", "doc_count_error_upper_bound") or not isinstance(val, dict):
            continue
        if "buckets" in val and depth < 3:
            subs.extend(_bucket(k, b, policy, stats, depth + 1) for k, b in _iter_buckets(val["buckets"]))
        elif "value" in val:
            metrics[name] = safe_float(val["value"])
    return Bucket(key=redact_text(str(key), policy, stats), doc_count=int(body.get("doc_count", 0) or 0),
                  sub_buckets=subs, metrics=metrics or None)


def _sum_other(node: Any) -> int:
    total = 0
    if isinstance(node, dict):
        v = node.get("sum_other_doc_count")
        total += v if isinstance(v, int) else 0
        total += sum(_sum_other(x) for x in node.values())
    elif isinstance(node, list):
        total += sum(_sum_other(x) for x in node)
    return total


def _max_error_bound(node: Any) -> int:
    best = 0
    if isinstance(node, dict):
        v = node.get("doc_count_error_upper_bound")
        best = v if isinstance(v, int) else 0
        best = max([best] + [_max_error_bound(x) for x in node.values()])
    elif isinstance(node, list):
        best = max([0] + [_max_error_bound(x) for x in node])
    return best


# ---- public ---------------------------------------------------------------------------------------------------


def normalize_search_response(raw: dict, *, entry: PatternEntry, query: dict, policy: RedactionPolicy,
                              max_series: int, max_hits: int) -> NormalizedResult:
    stats = RedactionStats()
    meta = response_metadata(raw, policy)
    total, relation = _total(raw)
    aggs = raw.get("aggregations")

    if not aggs:
        result = _hits(raw, entry, policy, stats, max_hits, total, relation, meta)
    else:
        result = _aggregation(raw, aggs, query, policy, stats, max_series, total, relation, meta, entry)

    meta["redaction"] = stats.to_dict(policy)
    sem = entry.severity_semantics
    if sem is not None:
        meta["severity_vocabulary"] = {f.path: f.verification for f in sem.fields}
        provisional = [f.path for f in sem.fields if f.verification == "provisional"]
        if provisional:
            meta["severity_vocabulary_provisional"] = provisional
    result.metadata = meta
    return result


def _hits(raw, entry: PatternEntry, policy, stats, max_hits, total, relation, meta) -> NormalizedResult:
    raw_hits = (raw.get("hits") or {}).get("hits") or []
    sem = entry.severity_semantics
    sev_paths = entry.role_paths("severity") or entry.role_paths("health_state")
    text_paths = entry.role_paths("text")
    rows: list[Hit] = []
    for h in raw_hits[:max_hits]:
        src = h.get("_source") or {}
        flds = {}
        for path in entry.output_projection:
            value = _lookup(src, path)
            if value is not None:
                flds[path] = redact_value(value, policy, stats)
        sev = next((flds[p] for p in sev_paths if isinstance(flds.get(p), str)), None)
        body = next((flds[p] for p in text_paths if isinstance(flds.get(p), str)), None)
        assessment = assess_hit(sem, src)
        if assessment is not None:
            assessment = redact_value(assessment, policy, stats)
        rows.append(Hit(timestamp=flds.get(entry.time_field), severity=sev, body=body, resource={},
                        attributes={}, fields=flds, severity_assessment=assessment))
    stamps = sorted(r.timestamp for r in rows if isinstance(r.timestamp, str))
    truncated = len(raw_hits) > max_hits
    meta["count"] = {"value": total, "relation": relation, "exact": relation == "eq"}
    # more_available is None (unknown) when the response carried no total.
    meta["rows"] = {"returned": len(rows), "matched": total, "matched_exact": relation == "eq",
                    "more_available": (total > len(rows)) if total is not None else None}
    meta["observed_range"] = {"earliest": stamps[0], "latest": stamps[-1],
                              "basis": "earliest and latest @timestamp among the returned rows only; NOT the full range of data available in OpenSearch and NOT the query window"} if stamps else None
    return NormalizedResult(result_type="hits", hits=rows, total_hits=total, truncated=truncated,
                            original_count=len(raw_hits) if truncated else None)


def _aggregation(raw, aggs, query, policy, stats, max_series, total, relation, meta,
                 entry: PatternEntry | None = None) -> NormalizedResult:
    name, body = next(iter(aggs.items()))
    kinds = _query_agg_kinds((query or {}).get("aggs") or {})
    other = _sum_other(aggs)
    err = _max_error_bound(aggs)
    reasons = []
    if "cardinality" in kinds:
        reasons.append("cardinality is an estimate")
    if err > 0:
        reasons.append(f"terms counts may be off by up to {err} documents per bucket")
    if other > 0:
        reasons.append(f"{other} matching documents fall outside the returned buckets")
    meta["count"] = {"value": total, "relation": relation, "exact": relation == "eq"}
    meta["aggregation"] = {"name": name, "approximate": bool(reasons), "approximate_reasons": reasons,
                           "sum_other_doc_count": other, "doc_count_error_upper_bound": err}

    buckets = body.get("buckets") if isinstance(body, dict) else None
    if buckets is None:
        value = safe_float((body or {}).get("value"))
        meta["aggregation"]["type"] = "metric"
        if (body or {}).get("value") is None:
            # e.g. avg/min/max over zero documents: there is no value, so there is
            # no result -- not a point carrying null at an invented timestamp.
            return NormalizedResult(result_type="series", series=[])
        point = Point(timestamp=_iso(datetime.now(timezone.utc)), value=value)
        return NormalizedResult(
            result_type="series", series=[Series(labels={"aggregation": name}, points=[point], legend_label=name)],
            had_invalid_samples=(body or {}).get("value") is not None and value is None)

    items = list(_iter_buckets(buckets))
    top_kind = _top_agg_kind(query, name)
    if top_kind is not None:
        # The query says what was asked for. `key_as_string` alone is not a signal:
        # OpenSearch also sets it on terms buckets of boolean (and date/ip) fields.
        is_histogram = top_kind == "date_histogram"
    else:
        # No query structure to consult (should not happen for a validated query):
        # fall back to the response shape, but never treat boolean keys as times.
        first = buckets[0] if isinstance(buckets, list) and buckets else None
        is_histogram = (isinstance(first, dict) and "key_as_string" in first
                        and first.get("key_as_string") not in ("true", "false"))
    if is_histogram:
        meta["aggregation"]["type"] = "date_histogram"
        return _histogram(items, max_series, policy, stats, meta)

    meta["aggregation"]["type"] = "filters" if isinstance(buckets, dict) else "terms"
    out = [_bucket(k, b, policy, stats) for k, b in items]
    if entry is not None and top_kind == "terms":
        meta["aggregation"]["interpretation"] = interpret_terms(entry, query, out)
    truncated = len(out) > max_series
    meta["aggregation"]["buckets_returned"] = min(len(out), max_series)
    return NormalizedResult(result_type="buckets", buckets=out[:max_series],
                            truncated=truncated or other > 0,
                            original_count=len(out) if truncated else None)


def _terms_levels(query: Any) -> list[str]:
    """Field paths of the top-level `terms` aggregation and of the `terms`
    aggregations nested directly inside it, outermost first."""
    levels: list[str] = []
    aggs = (query or {}).get("aggs") or (query or {}).get("aggregations") if isinstance(query, dict) else None
    while isinstance(aggs, dict) and aggs:
        spec = next((v for v in aggs.values() if isinstance(v, dict)), None)
        if not isinstance(spec, dict):
            break
        terms = spec.get("terms")
        if isinstance(terms, dict) and isinstance(terms.get("field"), str):
            levels.append(terms["field"])
        aggs = spec.get("aggs") or spec.get("aggregations")
    return levels


def _role_of(entry: PatternEntry, field_path: str) -> str | None:
    """Registry role of a (possibly `.keyword`-suffixed) effective field path."""
    for role, paths in entry.roles.items():
        if any(field_path == p or field_path == f"{p}.keyword" for p in paths):
            return role
    return None


def _bucket_keys_at(buckets: list[Bucket], level: int) -> set[str]:
    if level == 0:
        return {b.key for b in buckets}
    out: set[str] = set()
    for b in buckets:
        out |= _bucket_keys_at(b.sub_buckets, level - 1)
    return out


def interpret_terms(entry: PatternEntry, query: Any, buckets: list[Bucket]) -> dict:
    """Deterministic statement of what a `terms` result does and does not establish,
    attached to every grouped result regardless of how the model worded its own
    explanation. It never says anything the data cannot support: counts are
    documents per structured-field group, free text is never grouped, and no
    family has a baseline, so a ranking is never an anomaly finding."""
    levels = _terms_levels(query)
    grouped_by = [{"level": i + 1, "field": f, "role": _role_of(entry, f)} for i, f in enumerate(levels)]
    text_fields = sorted(set(entry.never_group) | set(entry.role_paths("text")))
    out: dict = {
        "grouped_by": grouped_by,
        "count_unit": "documents",
        "establishes": "How many matching documents fall into each group; groups come from structured fields "
                       "only (" + ", ".join(g["field"] for g in grouped_by) + ").",
        "does_not_establish": [
            "Message text (" + ", ".join(text_fields) + ") is never grouped, so these counts do not show that "
            "the same message text occurred repeatedly: repeated activity from one group is not the same as one "
            "identical message repeating.",
            "Groups are ranked by volume only. No baseline or threshold is defined for this index family, so a "
            "high count is not by itself unusual and no anomaly detection was performed.",
        ],
        "anomaly_detection": {"performed": False,
                              "reason": "no baseline or threshold is defined for this index family"},
    }
    for i, f in enumerate(levels):
        if _role_of(entry, f) == "event_code" and entry.event_codes:
            keys = _bucket_keys_at(buckets, i)
            known = {k: {"meaning": entry.event_codes[k]["meaning"], "kind": entry.event_codes[k]["kind"]}
                     for k in sorted(keys) if k in entry.event_codes}
            out["event_code_labels"] = known
            unknown = sorted(k for k in keys if k not in entry.event_codes)
            if unknown:
                out["event_codes_without_verified_meaning"] = unknown
    return out


def _histogram(items, max_series, policy, stats, meta) -> NormalizedResult:
    sub_name, sub_kind = None, None
    for _, b in items:
        for k, v in b.items():
            if k in ("key", "key_as_string", "doc_count") or not isinstance(v, dict):
                continue
            sub_name, sub_kind = k, ("terms" if "buckets" in v else "metric")
            break
        if sub_name:
            break

    def ts(b):
        return b.get("key_as_string") or _epoch_ms_to_iso(b.get("key"))

    nonzero = [ts(b) for _, b in items if (b.get("doc_count") or 0) > 0 and ts(b)]
    meta["observed_range"] = ({"earliest": min(nonzero), "latest": max(nonzero),
                               "basis": "earliest and latest non-empty time bucket among the returned buckets only; NOT the full range of data available in OpenSearch and NOT the query window"}
                              if nonzero else None)

    series: list[Series] = []
    if sub_name is None:
        series = [Series(labels={}, legend_label="count",
                         points=[Point(timestamp=ts(b), value=float(b.get("doc_count", 0))) for _, b in items])]
    elif sub_kind == "terms":
        per: dict[str, list[Point]] = {}
        for _, b in items:
            for k, sb in _iter_buckets((b.get(sub_name) or {}).get("buckets")):
                per.setdefault(redact_text(str(k), policy, stats), []).append(
                    Point(timestamp=ts(b), value=float(sb.get("doc_count", 0))))
        for term, pts in per.items():
            labels = {sub_name: term}
            series.append(Series(labels=labels, points=pts, legend_label=_legend_label_for(labels)))
    else:
        names = sorted({k for _, b in items for k, v in b.items()
                        if k not in ("key", "key_as_string", "doc_count") and isinstance(v, dict) and "value" in v})
        for n in names:
            pts = [Point(timestamp=ts(b), value=safe_float((b.get(n) or {}).get("value"))) for _, b in items]
            series.append(Series(labels={"metric": n}, points=pts, legend_label=n))
    original = len(series)
    truncated = original > max_series
    meta["aggregation"]["buckets_returned"] = len(items)
    return NormalizedResult(result_type="series", series=series[:max_series], truncated=truncated,
                            original_count=original if truncated else None)
