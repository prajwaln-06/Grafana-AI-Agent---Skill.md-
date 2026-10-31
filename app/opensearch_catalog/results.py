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
  query window        reported by the executor (requested window), kept
                      separate from the OBSERVED range of the returned
                      results (earliest/latest actually returned)
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


def _total(raw: dict) -> tuple[int | None, str]:
    total = (raw.get("hits") or {}).get("total")
    if isinstance(total, dict):
        return total.get("value"), total.get("relation") or "eq"
    if isinstance(total, int):
        return total, "eq"
    return None, "eq"


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
        result = _aggregation(raw, aggs, query, policy, stats, max_series, total, relation, meta)

    meta["redaction"] = stats.to_dict(policy)
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
    meta["rows"] = {"returned": len(rows), "matched": total, "matched_exact": relation == "eq",
                    "more_available": bool(total is not None and total > len(rows))}
    meta["observed_range"] = {"earliest": stamps[0], "latest": stamps[-1],
                              "basis": "returned rows only; not the full extent of the data"} if stamps else None
    return NormalizedResult(result_type="hits", hits=rows, total_hits=total, truncated=truncated,
                            original_count=len(raw_hits) if truncated else None)


def _aggregation(raw, aggs, query, policy, stats, max_series, total, relation, meta) -> NormalizedResult:
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
        point = Point(timestamp=_iso(datetime.now(timezone.utc)), value=value)
        meta["aggregation"]["type"] = "metric"
        return NormalizedResult(
            result_type="series", series=[Series(labels={"aggregation": name}, points=[point], legend_label=name)],
            had_invalid_samples=(body or {}).get("value") is not None and value is None)

    items = list(_iter_buckets(buckets))
    is_histogram = isinstance(buckets, list) and bool(buckets) and "key_as_string" in buckets[0]
    if is_histogram:
        meta["aggregation"]["type"] = "date_histogram"
        return _histogram(items, max_series, policy, stats, meta)

    meta["aggregation"]["type"] = "filters" if isinstance(buckets, dict) else "terms"
    out = [_bucket(k, b, policy, stats) for k, b in items]
    truncated = len(out) > max_series
    meta["aggregation"]["buckets_returned"] = min(len(out), max_series)
    return NormalizedResult(result_type="buckets", buckets=out[:max_series],
                            truncated=truncated or other > 0,
                            original_count=len(out) if truncated else None)


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
                               "basis": "first and last non-empty time bucket returned; not the full extent of the data"}
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
