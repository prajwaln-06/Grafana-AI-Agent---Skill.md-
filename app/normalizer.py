"""
normalizer.py

Converts raw backend responses (Prometheus's JSON API shapes, OpenSearch's
_search response shape) into the execution-contract result shapes the
frontend actually consumes. This is the ONE place backend-specific response
parsing happens -- everything downstream of this module works with the same
few, backend-agnostic shapes regardless of which datasource answered.

Three result shapes come out of this module (discriminated by
`execution.result_type`):

  "series"  -- {labels, points} objects, identical in shape whether the
               points came from a Prometheus range/instant query or an
               OpenSearch date_histogram (optionally with a terms
               sub-aggregation). This is what a line/multi-line chart wants.
  "buckets" -- {key, doc_count} objects from a non-time-bucketed OpenSearch
               terms aggregation (e.g. "count of failures by host", no time
               axis at all). This is what a bar chart / table wants.
  "hits"    -- normalized log documents from a plain OpenSearch search (no
               aggregation). This is what a log viewer / table wants.

See HANDOFF.md's "Output contract" section for the full rationale and worked examples.

JSON-safety: every numeric value that leaves this module has already been
sanitized -- NaN/+Inf/-Inf never reach json.dumps as bare tokens, because
those are not valid JSON per spec and most JS `JSON.parse`/`fetch().json()`
implementations throw on them. A single malformed sample must never be able
to break parsing of an entire response for the frontend.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


# ---- JSON-safety -------------------------------------------------------------


def safe_float(value: Any) -> float | None:
    """Converts a raw numeric value to a JSON-safe float, or None if it isn't
    representable in strict JSON (NaN, +Inf, -Inf) or isn't numeric at all
    (Prometheus's "StaleNaN" marker sample, an OpenSearch null bucket value,
    etc). Callers that need to know *whether* sanitization happened (to set
    `had_invalid_samples`) should check `value is not None and result is
    None` at the call site -- this function itself stays a pure converter."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ---- shared output dataclasses ------------------------------------------------


@dataclass
class Point:
    timestamp: str
    value: float | None


@dataclass
class Series:
    labels: dict[str, str]
    points: list[Point]
    legend_label: str = ""  # human-friendly label for direct use in a chart
                             # legend, computed once here so the frontend
                             # never has to reconstruct one from raw label
                             # dicts. See _legend_label_for().

    def to_dict(self) -> dict:
        return {
            "labels": self.labels,
            "legend_label": self.legend_label,
            "points": [{"timestamp": p.timestamp, "value": p.value} for p in self.points],
        }


@dataclass
class Bucket:
    key: str
    doc_count: int
    sub_buckets: list["Bucket"] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {"key": self.key, "doc_count": self.doc_count}
        if self.sub_buckets:
            d["sub_buckets"] = [b.to_dict() for b in self.sub_buckets]
        return d


@dataclass
class Hit:
    timestamp: str | None
    severity: str | None
    body: str | None
    resource: dict[str, str]
    attributes: dict[str, Any]

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "severity": self.severity,
            "body": self.body,
            "resource": self.resource,
            "attributes": self.attributes,
        }


@dataclass
class NormalizedResult:
    result_type: str                 # "series" | "buckets" | "hits"
    series: list[Series] = field(default_factory=list)
    buckets: list[Bucket] = field(default_factory=list)
    hits: list[Hit] = field(default_factory=list)
    had_invalid_samples: bool = False
    total_hits: int | None = None    # only meaningful for result_type=="hits"
    truncated: bool = False
    original_count: int | None = None

    @property
    def count(self) -> int:
        if self.result_type == "series":
            return len(self.series)
        if self.result_type == "buckets":
            return len(self.buckets)
        return len(self.hits)

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"result_type": self.result_type}
        if self.result_type == "series":
            d["series"] = [s.to_dict() for s in self.series]
            d["series_count"] = len(self.series)
            if len(self.series) > 1:
                d["comparison"] = _comparison_block(self.series)
        elif self.result_type == "buckets":
            d["buckets"] = [b.to_dict() for b in self.buckets]
            d["bucket_count"] = len(self.buckets)
        elif self.result_type == "hits":
            d["hits"] = [h.to_dict() for h in self.hits]
            d["hit_count"] = len(self.hits)
            d["total_hits"] = self.total_hits
        d["had_invalid_samples"] = self.had_invalid_samples
        d["truncated"] = self.truncated
        if self.truncated:
            d["original_count"] = self.original_count
        return d


# ---- frontend-friendliness: comparison metadata + legend labels -----------------


def _legend_label_for(labels: dict[str, str]) -> str:
    """Builds a human-readable legend string from a series' label dict so
    the frontend never has to know which label keys are semantically
    meaningful -- it can render this string directly on a chart with no
    further label-dict inspection. Falls back gracefully for the
    zero-label (single-series) case.

    Includes the label KEY, not just its value: a values-only legend like
    "node-1:9100, idle" is ambiguous the moment a series varies along more
    than one dimension -- the frontend (or a person reading the chart)
    can't tell which value is the instance and which is the CPU mode
    without separately inspecting the raw `labels` dict, which defeats the
    entire purpose of a pre-built legend string. "instance=node-1:9100,
    mode=idle" is unambiguous on its own.
    """
    if not labels:
        return "value"
    return ", ".join(f"{k}={v}" for k, v in labels.items())


def _comparison_block(series: list[Series]) -> dict:
    """Deterministic (never LLM-guessed) metadata describing what a
    multi-series result is being compared *by*. Computed from the actual
    label keys present across series -- the differentiating dimension is
    whichever label key(s) actually vary between series. This directly
    answers the "how do I plot node-1 vs node-2 on one chart" question: the
    frontend can render one line per `series[]` entry using `legend_label`
    without inspecting raw label dicts at all."""
    all_keys: set[str] = set()
    for s in series:
        all_keys.update(s.labels.keys())

    varying_keys = []
    for key in sorted(all_keys):
        values = {s.labels.get(key) for s in series}
        if len(values) > 1:
            varying_keys.append(key)

    return {
        "differentiated_by": varying_keys,
        "series_count": len(series),
    }


# ---- Prometheus normalization -----------------------------------------------


def normalize_prometheus_result(raw: dict, max_series: int = 200) -> NormalizedResult:
    """
    raw is Prometheus's `data` object from either /api/v1/query (instant,
    resultType "vector" or "scalar") or /api/v1/query_range (resultType
    "matrix"). Always produces result_type "series" -- Prometheus has no
    document/hit concept, so "series" is the only shape it ever needs.
    """
    result_type = raw.get("resultType")
    raw_result = raw.get("result", [])
    had_invalid = False

    series_list: list[Series] = []

    if result_type == "matrix":
        for entry in raw_result:
            labels = dict(entry.get("metric", {}))
            points = []
            for ts, val in entry.get("values", []):
                v = safe_float(val)
                if v is None and val is not None:
                    had_invalid = True
                points.append(Point(timestamp=_iso(datetime.fromtimestamp(float(ts), tz=timezone.utc)), value=v))
            series_list.append(Series(labels=labels, points=points, legend_label=_legend_label_for(labels)))

    elif result_type == "vector":
        for entry in raw_result:
            labels = dict(entry.get("metric", {}))
            ts, val = entry.get("value", [None, None])
            v = safe_float(val)
            if v is None and val is not None:
                had_invalid = True
            points = [Point(timestamp=_iso(datetime.fromtimestamp(float(ts), tz=timezone.utc)), value=v)] if ts is not None else []
            series_list.append(Series(labels=labels, points=points, legend_label=_legend_label_for(labels)))

    elif result_type == "scalar":
        ts, val = raw_result if raw_result else (None, None)
        v = safe_float(val)
        if v is None and val is not None:
            had_invalid = True
        points = [Point(timestamp=_iso(datetime.fromtimestamp(float(ts), tz=timezone.utc)), value=v)] if ts is not None else []
        series_list.append(Series(labels={}, points=points, legend_label="value"))

    else:
        # Unknown/future resultType (e.g. "string") -- degrade to an empty
        # series set rather than raising, since the caller (executor.py)
        # already distinguishes "empty_result" from "success" by series
        # count, and an unrecognized shape genuinely has no data we can
        # safely interpret as points.
        pass

    truncated = False
    original_count = len(series_list)
    if len(series_list) > max_series:
        series_list = series_list[:max_series]
        truncated = True

    return NormalizedResult(
        result_type="series",
        series=series_list,
        had_invalid_samples=had_invalid,
        truncated=truncated,
        original_count=original_count if truncated else None,
    )


# ---- OpenSearch normalization -------------------------------------------------


def normalize_opensearch_result(raw: dict, max_series: int = 200,
                                 max_hits: int = 500) -> NormalizedResult:
    """
    raw is the full OpenSearch `_search` response body. Branches on shape:

      - `aggregations` present, and its top-level agg is a date_histogram
        (bucket keys are timestamps) -> "series". If that date_histogram has
        a nested terms sub-aggregation, one Series per term-bucket value;
        otherwise one Series with labels={}.
      - `aggregations` present, top-level agg is a terms (or similar
        non-time) aggregation -> "buckets".
      - No `aggregations` at all -> "hits" (plain document search).

    This function inspects the actual response shape rather than trusting a
    caller-supplied "what kind of query was this" flag, so it degrades
    correctly even if the query-construction stage's own bookkeeping is
    wrong or incomplete.
    """
    aggs = raw.get("aggregations")
    if not aggs:
        return _normalize_opensearch_hits(raw, max_hits=max_hits)

    # Exactly one top-level aggregation is the documented/expected shape
    # (opensearch-fundamentals.md's Query Shape: one `aggs` block). If more
    # than one is present, take the first deterministically (dict insertion
    # order) rather than guessing which one the user cares about -- this
    # mirrors how the executor treats any other unexpected-but-not-fatal
    # shape: degrade gracefully, don't raise.
    agg_name, agg_body = next(iter(aggs.items()))
    buckets = agg_body.get("buckets")

    if buckets is None:
        # A metric aggregation (avg/sum/min/max/cardinality) with no
        # buckets at all -- a single scalar-like result. Represent as a
        # one-point, zero-label series so it still fits the "series" shape
        # a frontend already knows how to render as a stat/gauge.
        value = safe_float(agg_body.get("value"))
        had_invalid = agg_body.get("value") is not None and value is None
        point = Point(timestamp=_iso(datetime.now(timezone.utc)), value=value)
        series = Series(labels={"aggregation": agg_name}, points=[point],
                         legend_label=agg_name)
        return NormalizedResult(result_type="series", series=[series],
                                 had_invalid_samples=had_invalid)

    if _looks_like_date_histogram(buckets):
        return _normalize_date_histogram(agg_name, buckets, max_series=max_series)

    return _normalize_terms_buckets(buckets, max_series=max_series)


def _looks_like_date_histogram(buckets: list[dict]) -> bool:
    """date_histogram buckets carry `key_as_string`/an epoch-ms `key`
    representing a timestamp; plain terms buckets carry an arbitrary
    string/number `key` (a field value). Checking for `key_as_string`
    (which OpenSearch only emits for date-typed bucket keys) is a more
    reliable discriminator than assuming based on the query that was sent,
    since it's read directly from what the backend actually returned."""
    if not buckets:
        return False
    return "key_as_string" in buckets[0]


def _normalize_date_histogram(agg_name: str, buckets: list[dict],
                               max_series: int) -> NormalizedResult:
    # Does this date_histogram have a nested sub-aggregation (e.g. `terms`
    # grouping by Resource.service.name)? If so, one Series per distinct
    # term value across all time buckets; otherwise one Series total.
    sub_agg_name = None
    for bucket in buckets:
        for key in bucket.keys():
            if key not in ("key", "key_as_string", "doc_count") and isinstance(bucket[key], dict):
                sub_agg_name = key
                break
        if sub_agg_name:
            break

    had_invalid = False

    if sub_agg_name is None:
        points = []
        for bucket in buckets:
            ts = bucket.get("key_as_string") or _epoch_ms_to_iso(bucket.get("key"))
            points.append(Point(timestamp=ts, value=float(bucket.get("doc_count", 0))))
        series = [Series(labels={}, points=points, legend_label="count")]
        return NormalizedResult(result_type="series", series=series,
                                 had_invalid_samples=had_invalid)

    # Grouped: build {term_value: [Point, ...]} across all time buckets.
    per_term_points: dict[str, list[Point]] = {}
    for time_bucket in buckets:
        ts = time_bucket.get("key_as_string") or _epoch_ms_to_iso(time_bucket.get("key"))
        sub_buckets = time_bucket.get(sub_agg_name, {}).get("buckets", [])
        for term_bucket in sub_buckets:
            term_value = str(term_bucket.get("key"))
            per_term_points.setdefault(term_value, []).append(
                Point(timestamp=ts, value=float(term_bucket.get("doc_count", 0)))
            )

    series_list = []
    for term_value, points in per_term_points.items():
        labels = {sub_agg_name: term_value}
        series_list.append(Series(labels=labels, points=points,
                                   legend_label=_legend_label_for(labels)))

    truncated = False
    original_count = len(series_list)
    if len(series_list) > max_series:
        series_list = series_list[:max_series]
        truncated = True

    return NormalizedResult(
        result_type="series",
        series=series_list,
        had_invalid_samples=had_invalid,
        truncated=truncated,
        original_count=original_count if truncated else None,
    )


def _normalize_terms_buckets(buckets: list[dict], max_series: int) -> NormalizedResult:
    bucket_list = []
    for b in buckets:
        sub_buckets = []
        for key, val in b.items():
            if key in ("key", "doc_count", "key_as_string"):
                continue
            if isinstance(val, dict) and "buckets" in val:
                for sb in val["buckets"]:
                    sub_buckets.append(Bucket(key=str(sb.get("key")), doc_count=int(sb.get("doc_count", 0))))
        bucket_list.append(Bucket(key=str(b.get("key")), doc_count=int(b.get("doc_count", 0)),
                                   sub_buckets=sub_buckets))

    truncated = False
    original_count = len(bucket_list)
    if len(bucket_list) > max_series:
        bucket_list = bucket_list[:max_series]
        truncated = True

    return NormalizedResult(
        result_type="buckets",
        buckets=bucket_list,
        truncated=truncated,
        original_count=original_count if truncated else None,
    )


def _normalize_opensearch_hits(raw: dict, max_hits: int) -> NormalizedResult:
    hits_block = raw.get("hits", {})
    total = hits_block.get("total", {})
    total_hits = total.get("value") if isinstance(total, dict) else total
    raw_hits = hits_block.get("hits", [])

    hits: list[Hit] = []
    for h in raw_hits[:max_hits]:
        source = h.get("_source", {})
        resource = source.get("Resource", {}) or {}
        attributes = source.get("Attributes", {}) or {}
        hits.append(Hit(
            timestamp=source.get("@timestamp") or source.get("Timestamp"),
            severity=source.get("Severity") or source.get("SeverityText"),
            body=source.get("Body"),
            resource=resource,
            attributes=attributes,
        ))

    truncated = len(raw_hits) > max_hits
    return NormalizedResult(
        result_type="hits",
        hits=hits,
        total_hits=total_hits,
        truncated=truncated,
        original_count=len(raw_hits) if truncated else None,
    )


def _epoch_ms_to_iso(epoch_ms: float | None) -> str | None:
    if epoch_ms is None:
        return None
    return _iso(datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc))
