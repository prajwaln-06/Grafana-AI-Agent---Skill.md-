"""
guard.py

Deterministic canonicalisation + validation of a generated OpenSearch result
entry. Nothing here is advisory: a violation REJECTS the query and it never
reaches execution.

What it enforces
  * routing boundary -- the entry names a `pattern_id` from this request's
    context; the index string is looked up from the registry. A model-written
    `index` is accepted only if it equals the registered pattern exactly.
  * the live field catalog -- every field must exist live AND be registry-
    permitted AND support the operation used; text fields are used for exact
    match / grouping only through a mapping-confirmed keyword sub-field.
  * a closed query grammar (bool/term/terms/match/match_phrase/prefix/range/
    exists + the named severity condition) -- no scripts, query_string,
    wildcard/regexp, nested/join, runtime fields, paging.
  * a bounded time window -- exactly one range on the pattern's time field,
    both bounds, width <= the pattern's max_window. A longer window is
    REJECTED (code window_exceeds_max, with a user message); it is never
    shrunk or rewritten.
  * result bounds -- size, terms size, histogram buckets, total buckets,
    aggregation depth/count, track_total_hits, timeout, projection.

Canonicalisation = only (a) expanding the named severity condition and (b)
filling DOCUMENTED safe defaults when a key is absent (recorded in `applied`).
A value the model WROTE is never changed: out-of-range values are rejected,
not clamped, and `.keyword` is never added to a field for the model.

check_entry() is idempotent: feeding its output back in yields ok with no
further defaults applied.
"""
from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app import time_utils
from app.opensearch_catalog.context import Limits, OpenSearchContext, PatternContext
from app.opensearch_catalog.field_catalog import (
    OP_EXISTS, OP_GROUP_BY, OP_MATCH, OP_METRIC, OP_RANGE, OP_SORT, OP_TERM, FieldInfo,
)
from app.opensearch_catalog.severity import (
    CONDITION_ABNORMAL, NAMED_CONDITIONS, SeverityConditionError, expand_abnormal_condition,
    severity_paths,
)

# ---- result codes ---------------------------------------------------------------------------

CODE_CONTEXT_UNAVAILABLE = "context_unavailable"
CODE_UNKNOWN_PATTERN = "unknown_pattern"
CODE_INDEX_MISMATCH = "index_mismatch"
CODE_FORBIDDEN_KEY = "forbidden_key"
CODE_UNKNOWN_FIELD = "unknown_field"
CODE_OPERATION_NOT_SUPPORTED = "operation_not_supported"
CODE_BAD_CLAUSE = "bad_clause"
CODE_BAD_VALUE = "bad_value"
CODE_TIME_RANGE = "time_range_invalid"
CODE_WINDOW_EXCEEDS_MAX = "window_exceeds_max"
CODE_LIMIT = "limit_exceeded"
CODE_AGGREGATION = "aggregation_invalid"
CODE_SEVERITY = "severity_invalid"
CODE_PROJECTION = "projection_violation"

ALLOWED_TOP_LEVEL_KEYS = frozenset(
    {"query", "size", "from", "sort", "aggs", "aggregations", "_source", "track_total_hits", "timeout"})

# Keys that must never appear ANYWHERE in a body (structural, field-agnostic;
# also enforced again by the executor).
FORBIDDEN_KEYS = frozenset({
    "script", "script_fields", "script_score", "runtime_mappings", "function_score", "query_string",
    "simple_query_string", "regexp", "wildcard", "fuzzy", "more_like_this", "percolate", "pit",
    "search_after", "collapse", "highlight", "suggest", "rescore", "stored_fields", "docvalue_fields",
    "indices_boost", "post_filter", "inner_hits", "nested", "has_child", "has_parent", "parent_id",
    "geo_distance", "geo_bounding_box", "geo_shape", "terms_set", "span_near", "intervals",
    "distance_feature", "wrapper", "knn", "neural", "hybrid", "ext", "profile", "explain",
    "stats", "indices", "preference", "routing",
})

_WINDOW_UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400}
_INTERVAL_RE = re.compile(r"^([1-9][0-9]*)(s|m|h|d)$")
_TIMEOUT_RE = re.compile(r"^([1-9][0-9]*)(ms|s)$")
_AGG_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,40}$")


class GuardError(Exception):
    def __init__(self, code: str, reason: str, user_message: str = ""):
        super().__init__(reason)
        self.code, self.reason, self.user_message = code, reason, user_message


@dataclass(frozen=True)
class GuardResult:
    ok: bool
    code: str = ""
    reason: str = ""
    user_message: str = ""
    query: dict | None = None
    index: str | None = None
    pattern_id: str | None = None
    applied: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    window: dict | None = None
    estimated_buckets: int = 0
    approximate_aggregations: tuple[str, ...] = ()


def _fail(code: str, reason: str, user_message: str = "") -> None:
    raise GuardError(code, reason, user_message)


# ---- structural (field-agnostic) check, shared with the executor ------------------------------------


def _scan_forbidden(node: Any, path: str = "") -> str | None:
    if isinstance(node, dict):
        for k, v in node.items():
            if k in FORBIDDEN_KEYS:
                return f"{path}/{k}" if path else k
            hit = _scan_forbidden(v, f"{path}/{k}")
            if hit:
                return hit
    elif isinstance(node, list):
        for i, v in enumerate(node):
            hit = _scan_forbidden(v, f"{path}[{i}]")
            if hit:
                return hit
    return None


def structural_check(body: Any, limits: Limits) -> str | None:
    """A reason string if `body` violates a field-agnostic rule, else None.
    Used by check_entry() and, independently, by the executor as defence in
    depth (the executor has no field catalog)."""
    if not isinstance(body, dict) or not body:
        return "the query is not a non-empty DSL object"
    extra = set(body) - ALLOWED_TOP_LEVEL_KEYS
    if extra:
        return f"top-level key(s) not allowed: {sorted(extra)}"
    try:
        text = json.dumps(body)
    except (TypeError, ValueError):
        return "the query is not JSON-serialisable"
    if len(text) > limits.max_query_chars:
        return f"the query is larger than {limits.max_query_chars} characters"
    bad = _scan_forbidden(body)
    if bad:
        return (f"forbidden construct at {bad!r} (scripts, query_string, wildcard/regexp, "
                f"nested/join queries and runtime fields are not allowed)")
    size = body.get("size")
    if size is not None and (isinstance(size, bool) or not isinstance(size, int)
                             or size < 0 or size > limits.max_hits_size):
        return f"size must be an integer between 0 and {limits.max_hits_size}"
    frm = body.get("from")
    if frm not in (None, 0) or isinstance(frm, bool):
        return "paging with `from` is not allowed"
    return None


# ---- value helpers -------------------------------------------------------------------------------------


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _parse_bound(value: Any, now: datetime, where: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        _fail(CODE_TIME_RANGE, f"{where}: the time bound must be a date-math or ISO-8601 string")
    text = value.strip()
    if text.lower().startswith("now"):
        try:
            return time_utils.resolve_relative_time(text, reference=now)
        except time_utils.TimeParseError as e:
            _fail(CODE_TIME_RANGE, f"{where}: {e}")
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        _fail(CODE_TIME_RANGE, f"{where}: {text!r} is neither date math (now-24h) nor an ISO-8601 timestamp")
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _duration_text(seconds: float) -> str:
    seconds = int(round(seconds))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def extract_window(query: Any, time_field: str, now: datetime | None = None) -> dict | None:
    """The window of the (single) range clause on `time_field`, as
    {from_expression,to_expression,start,end,seconds}; None if there is none
    or it is not fully bounded/parseable. Never raises. Used by the executor
    to report the window actually queried."""
    now = now or datetime.now(timezone.utc)
    try:
        found: list[dict] = []

        def walk(n):
            if isinstance(n, dict):
                r = n.get("range")
                if isinstance(r, dict) and isinstance(r.get(time_field), dict):
                    found.append(r[time_field])
                for v in n.values():
                    walk(v)
            elif isinstance(n, list):
                for v in n:
                    walk(v)

        walk(query)
        if len(found) != 1:
            return None
        b = found[0]
        lo_key = "gte" if "gte" in b else "gt" if "gt" in b else None
        hi_key = "lte" if "lte" in b else "lt" if "lt" in b else None
        if not lo_key or not hi_key:
            return None
        start, end = _parse_bound(b[lo_key], now, "from"), _parse_bound(b[hi_key], now, "to")
        return {"from_expression": b[lo_key], "to_expression": b[hi_key],
                "start": start.isoformat().replace("+00:00", "Z"),
                "end": end.isoformat().replace("+00:00", "Z"), "seconds": (end - start).total_seconds()}
    except Exception:  # noqa: BLE001 -- reporting helper, never fatal
        return None


# ---- the checker -------------------------------------------------------------------------------------------


class _Checker:
    def __init__(self, pc: PatternContext, limits: Limits, now: datetime):
        self.pc, self.entry, self.view, self.limits, self.now = pc, pc.entry, pc.view, limits, now
        self.applied: list[str] = []
        self.warnings: list[str] = []
        self.clauses = 0
        self.time_ranges: list[dict] = []
        self.window: dict | None = None
        self.approx: list[str] = []
        self.agg_count = 0
        self.group_by_effective = frozenset(self.view.group_by_fields.values())
        self.sev = self.entry.severity_semantics
        self.sev_paths = severity_paths(self.sev)

    # -- fields ------------------------------------------------------------------------------------------------

    def info(self, path: Any, op: str, where: str) -> FieldInfo:
        if not isinstance(path, str) or not path:
            _fail(CODE_BAD_CLAUSE, f"{where}: a field name is required")
        info = self.view.allowed_fields.get(path)
        if info is None:
            if path in self.view.missing_fields:
                why = "is registered but is not present in the live mapping"
            else:
                why = "is not a field this index family permits"
            _fail(CODE_UNKNOWN_FIELD, f"{where}: field {path!r} {why}. Use only the listed fields.")
        if op not in info.ops:
            hint = ""
            if info.keyword_subfield and op in (OP_TERM, OP_GROUP_BY, OP_SORT):
                hint = f" Use its confirmed exact-match sub-field {info.keyword_subfield!r}."
            elif info.family == "text" and op in (OP_TERM, OP_GROUP_BY, OP_SORT):
                hint = " This text field has no confirmed keyword sub-field, so it can only be searched with match/match_phrase."
            elif info.conflict:
                hint = " Its mapping type differs between recent indices."
            _fail(CODE_OPERATION_NOT_SUPPORTED,
                  f"{where}: field {path!r} ({info.type}) does not support '{op}'.{hint}")
        return info

    def base_path(self, path: str) -> str:
        info = self.view.allowed_fields.get(path)
        return info.parent if info is not None and info.parent else path

    # -- clauses -------------------------------------------------------------------------------------------------

    def clause(self, node: Any, where: str, depth: int, top: bool = False) -> dict:
        if not isinstance(node, dict) or len(node) != 1:
            _fail(CODE_BAD_CLAUSE, f"{where}: a clause must be an object with exactly one key")
        self.clauses += 1
        if self.clauses > self.limits.max_clauses:
            _fail(CODE_LIMIT, f"the query has more than {self.limits.max_clauses} clauses")
        (kind, body), = node.items()
        handler = {
            "bool": self._bool, "term": self._term, "terms": self._terms, "match": self._match,
            "match_phrase": self._match, "prefix": self._prefix, "range": self._range,
            "exists": self._exists, "named_condition": self._named,
        }.get(kind)
        if handler is None:
            _fail(CODE_FORBIDDEN_KEY if kind in FORBIDDEN_KEYS else CODE_BAD_CLAUSE,
                  f"{where}: clause type {kind!r} is not allowed")
        w = f"{where}/{kind}"
        if kind == "bool":
            return {"bool": self._bool(body, w, depth)}
        if kind == "named_condition":
            return self._named(body, w)
        if kind == "range":
            return {"range": self._range(body, w, top)}
        if kind == "exists":
            return {"exists": self._exists(body, w)}
        return {kind: handler(body, w, kind)}

    def _bool(self, body: Any, where: str, depth: int) -> dict:
        if depth > self.limits.max_bool_depth:
            _fail(CODE_LIMIT, f"{where}: bool nesting deeper than {self.limits.max_bool_depth}")
        if not isinstance(body, dict) or not body:
            _fail(CODE_BAD_CLAUSE, f"{where}: bool must be a non-empty object")
        extra = set(body) - {"must", "filter", "should", "must_not", "minimum_should_match"}
        if extra:
            _fail(CODE_BAD_CLAUSE, f"{where}: unsupported bool key(s) {sorted(extra)}")
        out: dict[str, Any] = {}
        for key in ("must", "filter", "should", "must_not"):
            if key not in body:
                continue
            val = body[key]
            items = val if isinstance(val, list) else [val]
            if not items:
                _fail(CODE_BAD_CLAUSE, f"{where}/{key}: must not be empty")
            checked = []
            for i, child in enumerate(items):
                cw = f"{where}/{key}[{i}]"
                if key == "must_not" and self._mentions_severity(child) and self.sev is not None \
                        and self.sev.mode == "positive_abnormal":
                    _fail(CODE_SEVERITY,
                          f"{cw}: a must_not on a severity field is not allowed for this index family because it "
                          f"would treat every unrecognised or empty value as abnormal. Use "
                          f"{{\"named_condition\": \"{CONDITION_ABNORMAL}\"}} instead.")
                # Only the ROOT bool's direct filter/must children may hold the
                # time range (depth 1 = the root bool).
                checked.append(self.clause(child, cw, depth + 1, top=(depth == 1 and key in ("filter", "must"))))
            out[key] = checked if isinstance(val, list) else checked[0]
        if "minimum_should_match" in body:
            m = body["minimum_should_match"]
            if isinstance(m, bool) or not isinstance(m, int) or m < 0 or m > 10:
                _fail(CODE_BAD_VALUE, f"{where}: minimum_should_match must be a small integer")
            out["minimum_should_match"] = m
        return out

    def _mentions_severity(self, node: Any) -> bool:
        if isinstance(node, dict):
            for kind in ("term", "terms", "match", "match_phrase", "prefix", "exists"):
                leaf = node.get(kind)
                if isinstance(leaf, dict):
                    names = [leaf["field"]] if kind == "exists" and "field" in leaf else list(leaf)
                    if any(isinstance(n, str) and self.base_path(n) in self.sev_paths for n in names):
                        return True
            return any(self._mentions_severity(v) for v in node.values())
        if isinstance(node, list):
            return any(self._mentions_severity(v) for v in node)
        return False

    def _single_field(self, body: Any, where: str) -> tuple[str, Any]:
        if not isinstance(body, dict) or len(body) != 1:
            _fail(CODE_BAD_CLAUSE, f"{where}: expected exactly one field")
        (path, val), = body.items()
        return path, val

    def _scalar(self, info: FieldInfo, value: Any, where: str) -> Any:
        fam = info.family
        if fam in ("keyword", "text"):
            if not isinstance(value, str) or value == "" or len(value) > self.limits.max_term_chars:
                _fail(CODE_BAD_VALUE, f"{where}: value must be a non-empty string up to {self.limits.max_term_chars} chars")
        elif fam == "numeric":
            if not _is_num(value):
                _fail(CODE_BAD_VALUE, f"{where}: value must be a number for numeric field {info.path!r}")
        elif fam == "boolean":
            if not isinstance(value, bool):
                _fail(CODE_BAD_VALUE, f"{where}: value must be true or false for boolean field {info.path!r}")
        elif not isinstance(value, (str, int, float)) or isinstance(value, bool):
            _fail(CODE_BAD_VALUE, f"{where}: unsupported value type")
        return value

    def _check_severity_value(self, info: FieldInfo, value: Any, where: str) -> None:
        if self.sev is None or not isinstance(value, str):
            return
        base = self.base_path(info.path)
        f = self.sev.field(base)
        if f is None or value in f.known:
            return
        lowered = {k.lower(): k for k in f.known}
        if value.lower() in lowered:
            _fail(CODE_SEVERITY,
                  f"{where}: severity value {value!r} differs only in case from the stored value "
                  f"{lowered[value.lower()]!r}; compare the exact stored case.")
        self.warnings.append(f"{where}: {value!r} is not a documented value of {base!r} "
                             f"(documented: {sorted(f.known)}); it may match nothing")

    def _exact(self, path: Any, where: str) -> tuple[str, FieldInfo]:
        """Exact-match target for `path`. A text field is analysed, so a term
        on it would compare against tokens, not the stored value; the catalog's
        confirmed keyword sub-field is the exact-match path and is substituted
        deterministically and RECORDED (idempotent: the sub-field maps to itself)."""
        info = self.info(path, OP_TERM, where)
        if self.base_path(path) in self.entry.role_paths("text"):
            _fail(CODE_OPERATION_NOT_SUPPORTED,
                  f"{where}: {path!r} is the family's free-text field, which is search-only; use match or "
                  f"match_phrase (not term/terms/prefix)")
        eff = info.ops.get(OP_TERM, path)
        if eff != path:
            self.applied.append(f"{where}: exact match on analysed text field {path!r} applied to its confirmed "
                                f"keyword sub-field {eff!r}")
            return eff, self.info(eff, OP_TERM, where)
        return path, info

    def _term(self, body: Any, where: str, kind: str) -> dict:
        path, val = self._single_field(body, where)
        path, info = self._exact(path, where)
        if isinstance(val, dict):
            if set(val) != {"value"}:
                _fail(CODE_BAD_CLAUSE, f"{where}: only {{\"value\": ...}} is allowed")
            self._scalar(info, val["value"], where)
            self._check_severity_value(info, val["value"], where)
        else:
            self._scalar(info, val, where)
            self._check_severity_value(info, val, where)
        return {path: val}

    def _terms(self, body: Any, where: str, kind: str) -> dict:
        path, val = self._single_field(body, where)
        path, info = self._exact(path, where)
        if not isinstance(val, list) or not val:
            _fail(CODE_BAD_VALUE, f"{where}: terms needs a non-empty list of values (lookups are not allowed)")
        if len(val) > self.limits.max_terms_values:
            _fail(CODE_LIMIT, f"{where}: more than {self.limits.max_terms_values} values")
        for v in val:
            self._scalar(info, v, where)
            self._check_severity_value(info, v, where)
        return {path: val}

    def _match(self, body: Any, where: str, kind: str) -> dict:
        path, val = self._single_field(body, where)
        self.info(path, OP_MATCH, where)
        if self.base_path(path) not in self.entry.role_paths("text"):
            _fail(CODE_OPERATION_NOT_SUPPORTED,
                  f"{where}: match/match_phrase may only be used on the family's text field(s) "
                  f"{list(self.entry.role_paths('text'))}; use term/terms for exact values")
        text = val
        if isinstance(val, dict):
            allowed = {"query"} | ({"operator"} if kind == "match" else set())
            if "query" not in val or set(val) - allowed:
                _fail(CODE_BAD_CLAUSE, f"{where}: only {sorted(allowed)} are allowed")
            if "operator" in val and val["operator"] not in ("and", "or"):
                _fail(CODE_BAD_VALUE, f"{where}: operator must be 'and' or 'or'")
            text = val["query"]
        if not isinstance(text, str) or not text.strip() or len(text) > self.limits.max_text_chars:
            _fail(CODE_BAD_VALUE, f"{where}: search text must be 1-{self.limits.max_text_chars} characters")
        return {path: val}

    def _prefix(self, body: Any, where: str, kind: str) -> dict:
        path, val = self._single_field(body, where)
        path, info = self._exact(path, where)
        if info.family != "keyword":
            _fail(CODE_OPERATION_NOT_SUPPORTED, f"{where}: prefix needs an exact-match (keyword) field")
        text = val["value"] if isinstance(val, dict) and set(val) == {"value"} else val
        if not isinstance(text, str) or not text or "*" in text or "?" in text or len(text) > self.limits.max_term_chars:
            _fail(CODE_BAD_VALUE, f"{where}: prefix must be a plain non-empty string without wildcard characters")
        return {path: val}

    def _exists(self, body: Any, where: str) -> dict:
        if not isinstance(body, dict) or set(body) != {"field"}:
            _fail(CODE_BAD_CLAUSE, f"{where}: expected {{\"field\": <path>}}")
        self.info(body["field"], OP_EXISTS, where)
        return body

    def _range(self, body: Any, where: str, top: bool) -> dict:
        path, val = self._single_field(body, where)
        info = self.info(path, OP_RANGE, where)
        if not isinstance(val, dict) or not val or set(val) - {"gte", "gt", "lte", "lt"}:
            _fail(CODE_BAD_CLAUSE, f"{where}: range supports only gte/gt/lte/lt")
        if path == self.entry.time_field:
            if not top:
                _fail(CODE_TIME_RANGE,
                      f"{where}: the time range must be a direct child of the top-level bool filter/must, "
                      f"and there must be exactly one")
            self.time_ranges.append(val)
        else:
            if info.family == "date":
                _fail(CODE_TIME_RANGE, f"{where}: only the time field {self.entry.time_field!r} may be range-filtered by date")
            for v in val.values():
                if not _is_num(v):
                    _fail(CODE_BAD_VALUE, f"{where}: range bounds must be numbers")
        return {path: val}

    def _named(self, body: Any, where: str) -> dict:
        if body not in NAMED_CONDITIONS:
            _fail(CODE_BAD_CLAUSE, f"{where}: unknown named condition {body!r}")
        try:
            clause, warns = expand_abnormal_condition(self.entry, self.view)
        except SeverityConditionError as e:
            _fail(CODE_SEVERITY, f"{where}: {e}")
        self.warnings.extend(warns)
        self.applied.append(f"expanded named_condition '{body}' using the family's documented severity definition")
        return clause

    # -- time window ------------------------------------------------------------------------------------------------

    def resolve_window(self) -> dict:
        tf = self.entry.time_field
        if len(self.time_ranges) != 1:
            _fail(CODE_TIME_RANGE,
                  f"the query needs exactly one range on the time field {tf!r} as a direct child of the "
                  f"top-level bool filter/must (found {len(self.time_ranges)})",
                  "I need a bounded time range to search. Please say how far back to look (for example "
                  "\"the last 24 hours\").")
        b = self.time_ranges[0]
        lo = "gte" if "gte" in b else "gt" if "gt" in b else None
        hi = "lte" if "lte" in b else "lt" if "lt" in b else None
        if lo is None or hi is None or ("gte" in b and "gt" in b) or ("lte" in b and "lt" in b):
            _fail(CODE_TIME_RANGE, f"the {tf!r} range must have exactly one lower and one upper bound",
                  "I need both a start and an end for the time range. Please say how far back to look.")
        start, end = _parse_bound(b[lo], self.now, "time range lower bound"), _parse_bound(b[hi], self.now, "time range upper bound")
        if end <= start:
            _fail(CODE_TIME_RANGE, "the time range ends before it starts")
        width = (end - start).total_seconds()
        cap = self.entry.max_window_seconds
        window = {"from_expression": b[lo], "to_expression": b[hi],
                  "start": start.isoformat().replace("+00:00", "Z"), "end": end.isoformat().replace("+00:00", "Z"),
                  "seconds": width, "max_seconds": cap, "max_window": self.entry.max_window}
        if width > cap:
            _fail(CODE_WINDOW_EXCEEDS_MAX,
                  f"time window of {_duration_text(width)} exceeds the maximum of {self.entry.max_window} for "
                  f"{self.entry.pattern_id!r}",
                  f"The time range you asked for (about {_duration_text(width)}) is longer than the "
                  f"{self.entry.max_window} maximum for this log source. I have not changed it. "
                  f"Please narrow it to {self.entry.max_window} or less (for example, the last 24 hours).")
        return window

    # -- aggregations --------------------------------------------------------------------------------------------------

    def aggs(self, node: Any, where: str, depth: int, window_seconds: float) -> tuple[dict, int]:
        if not isinstance(node, dict) or not node:
            _fail(CODE_AGGREGATION, f"{where}: aggs must be a non-empty object")
        if depth > self.limits.max_agg_depth:
            _fail(CODE_LIMIT, f"{where}: aggregations nested deeper than {self.limits.max_agg_depth}")
        out, total = {}, 0
        for name, spec in node.items():
            if not _AGG_NAME_RE.match(str(name)):
                _fail(CODE_AGGREGATION, f"{where}: aggregation name {name!r} must be letters/digits/underscore")
            self.agg_count += 1
            if self.agg_count > self.limits.max_aggs:
                _fail(CODE_LIMIT, f"more than {self.limits.max_aggs} aggregations")
            canon, est = self._one_agg(name, spec, f"{where}/{name}", depth, window_seconds)
            out[name] = canon
            total += est
        return out, total

    def _one_agg(self, name: str, spec: Any, where: str, depth: int, window_seconds: float) -> tuple[dict, int]:
        if not isinstance(spec, dict):
            _fail(CODE_AGGREGATION, f"{where}: aggregation must be an object")
        subs_key = next((k for k in ("aggs", "aggregations") if k in spec), None)
        kinds = [k for k in spec if k not in ("aggs", "aggregations")]
        if len(kinds) != 1:
            _fail(CODE_AGGREGATION, f"{where}: exactly one aggregation type is required")
        kind, body = kinds[0], spec[kinds[0]]
        if not isinstance(body, dict):
            _fail(CODE_AGGREGATION, f"{where}/{kind}: must be an object")
        metric = kind in ("cardinality", "min", "max", "avg", "sum", "value_count")
        if metric and subs_key:
            _fail(CODE_AGGREGATION, f"{where}: a {kind} aggregation cannot have sub-aggregations")
        n_buckets = 1
        if kind == "terms":
            canon_body, n_buckets = self._terms_agg(body, f"{where}/terms")
        elif kind == "date_histogram":
            canon_body, n_buckets = self._histogram(body, f"{where}/date_histogram", window_seconds)
        elif kind == "filters":
            canon_body, n_buckets = self._filters(body, f"{where}/filters", depth)
        elif metric:
            canon_body = self._metric(kind, body, f"{where}/{kind}")
        else:
            _fail(CODE_AGGREGATION, f"{where}: aggregation type {kind!r} is not allowed")
        canon: dict[str, Any] = {kind: canon_body}
        sub_est = 1
        if subs_key:
            subs, sub_est = self.aggs(spec[subs_key], f"{where}/aggs", depth + 1, window_seconds)
            canon["aggs"] = subs
            if subs_key == "aggregations":
                self.applied.append("renamed `aggregations` to `aggs`")
        if kind == "cardinality":
            self.approx.append(name)
        return canon, n_buckets * max(1, sub_est)

    def _terms_agg(self, body: dict, where: str) -> tuple[dict, int]:
        extra = set(body) - {"field", "size", "order", "min_doc_count"}
        if extra:
            _fail(CODE_AGGREGATION, f"{where}: unsupported key(s) {sorted(extra)}")
        field_ = body.get("field")
        info = self.info(field_, OP_GROUP_BY, where)
        if field_ not in self.group_by_effective:
            _fail(CODE_AGGREGATION,
                  f"{where}: {field_!r} may not be grouped on for this index family (free text, identifiers and "
                  f"fields outside the group-by list are excluded). Allowed: {sorted(self.group_by_effective)}")
        out = dict(body)
        if "size" not in body:
            out["size"] = self.limits.default_terms_size
            self.applied.append(f"terms size defaulted to {self.limits.default_terms_size}")
        size = out["size"]
        if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= self.limits.max_terms_size:
            _fail(CODE_LIMIT, f"{where}: size must be an integer from 1 to {self.limits.max_terms_size}")
        if "order" in body:
            o = body["order"]
            if not (isinstance(o, dict) and len(o) == 1 and next(iter(o)) in ("_count", "_key")
                    and next(iter(o.values())) in ("asc", "desc")):
                _fail(CODE_AGGREGATION, f"{where}: order must be {{\"_count\"|\"_key\": \"asc\"|\"desc\"}}")
        if "min_doc_count" in body:
            m = body["min_doc_count"]
            if isinstance(m, bool) or not isinstance(m, int) or m < 0:
                _fail(CODE_AGGREGATION, f"{where}: min_doc_count must be a non-negative integer")
        return out, size

    def _histogram(self, body: dict, where: str, window_seconds: float) -> tuple[dict, int]:
        extra = set(body) - {"field", "fixed_interval", "min_doc_count"}
        if extra:
            _fail(CODE_AGGREGATION, f"{where}: unsupported key(s) {sorted(extra)}")
        if body.get("field") != self.entry.time_field:
            _fail(CODE_AGGREGATION, f"{where}: date_histogram must use the time field {self.entry.time_field!r}")
        self.info(body["field"], OP_RANGE, where)
        m = _INTERVAL_RE.match(str(body.get("fixed_interval", "")))
        if not m:
            _fail(CODE_AGGREGATION, f"{where}: fixed_interval is required, like '5m', '1h' or '1d'")
        interval = int(m.group(1)) * _WINDOW_UNITS[m.group(2)]
        buckets = math.ceil(window_seconds / interval)
        if buckets > self.limits.max_histogram_buckets:
            need = math.ceil(window_seconds / self.limits.max_histogram_buckets)
            _fail(CODE_LIMIT,
                  f"{where}: {body['fixed_interval']!r} over this window gives {buckets} buckets (maximum "
                  f"{self.limits.max_histogram_buckets}); use an interval of at least {_duration_text(need)}")
        if "min_doc_count" in body and body["min_doc_count"] not in (0, 1):
            _fail(CODE_AGGREGATION, f"{where}: min_doc_count must be 0 or 1")
        return dict(body), max(1, buckets)

    def _filters(self, body: dict, where: str, depth: int) -> tuple[dict, int]:
        if set(body) != {"filters"} or not isinstance(body["filters"], dict) or not body["filters"]:
            _fail(CODE_AGGREGATION, f"{where}: expected {{\"filters\": {{name: clause, ...}}}}")
        if len(body["filters"]) > self.limits.max_filters_buckets:
            _fail(CODE_LIMIT, f"{where}: more than {self.limits.max_filters_buckets} filters")
        out = {}
        for fname, clause in body["filters"].items():
            if not _AGG_NAME_RE.match(str(fname)):
                _fail(CODE_AGGREGATION, f"{where}: filter name {fname!r} must be letters/digits/underscore")
            out[fname] = self.clause(clause, f"{where}/{fname}", depth + 1)
        return {"filters": out}, len(out)

    def _metric(self, kind: str, body: dict, where: str) -> dict:
        if set(body) != {"field"}:
            _fail(CODE_AGGREGATION, f"{where}: only {{\"field\": <path>}} is allowed")
        path = body["field"]
        if kind in ("cardinality", "value_count"):
            if path not in self.group_by_effective:
                info = self.view.allowed_fields.get(path)
                if info is None or OP_METRIC not in info.ops:
                    _fail(CODE_AGGREGATION, f"{where}: {path!r} must be an exact-match group-by path"
                                            f"{' or numeric field' if kind == 'value_count' else ''}: "
                                            f"{sorted(self.group_by_effective)}")
            self.info(path, OP_EXISTS, where)
        elif kind in ("min", "max") and path == self.entry.time_field:
            self.info(path, OP_RANGE, where)
        else:
            self.info(path, OP_METRIC, where)
        return dict(body)


# ---- public entry point -----------------------------------------------------------------------------------------------


def check_entry(entry: dict, ctx: OpenSearchContext, *, now: datetime | None = None) -> GuardResult:
    """Validate and canonicalise ONE result entry (data_source 'opensearch')."""
    now = now or datetime.now(timezone.utc)
    pid = entry.get("pattern_id") if isinstance(entry, dict) else None
    try:
        if ctx is None or ctx.error:
            _fail(CODE_CONTEXT_UNAVAILABLE,
                  "OpenSearch context is unavailable" + (f": {ctx.error}" if ctx and ctx.error else ""))
        if not isinstance(pid, str) or pid not in ctx.patterns:
            _fail(CODE_UNKNOWN_PATTERN,
                  f"`pattern_id` {pid!r} is missing or was not selected for this request "
                  f"(available: {sorted(ctx.patterns)}); the index is chosen by pattern_id, never written by the model")
        pc = ctx.patterns[pid]
        if not pc.usable:
            _fail(CODE_CONTEXT_UNAVAILABLE,
                  f"live field discovery is not available for {pid!r} ({pc.view.discovery_status}"
                  f"{': ' + pc.view.reason if pc.view.reason else ''}); no query is built from unconfirmed fields")
        supplied = entry.get("index")
        if supplied not in (None, "", pc.entry.index_pattern):
            _fail(CODE_INDEX_MISMATCH,
                  f"`index` {supplied!r} does not equal the registered pattern for {pid!r}; omit `index` and let "
                  f"the system set it")
        limits = ctx.limits
        query = entry.get("query")
        reason = structural_check(query, limits)
        if reason:
            code = CODE_FORBIDDEN_KEY if "forbidden" in reason or "not allowed" in reason else CODE_LIMIT \
                if "larger" in reason or "size" in reason else CODE_BAD_CLAUSE
            _fail(code, reason)

        ck = _Checker(pc, limits, now)
        body = copy.deepcopy(query)
        out: dict[str, Any] = {}

        # query
        q = body.get("query")
        if not isinstance(q, dict) or set(q) != {"bool"}:
            _fail(CODE_TIME_RANGE, "the query must be a top-level bool query containing the time range in `filter`",
                  "I need a bounded time range to search. Please say how far back to look.")
        out["query"] = ck.clause(q, "query", 1)
        window = ck.resolve_window()

        # aggregations
        has_aggs = "aggs" in body or "aggregations" in body
        est = 0
        if has_aggs:
            if "aggs" in body and "aggregations" in body:
                _fail(CODE_AGGREGATION, "give either `aggs` or `aggregations`, not both")
            raw_aggs = body.get("aggs", body.get("aggregations"))
            if "aggregations" in body:
                ck.applied.append("renamed `aggregations` to `aggs`")
            if isinstance(raw_aggs, dict) and len(raw_aggs) != 1:
                _fail(CODE_AGGREGATION, "exactly ONE top-level aggregation is supported per query")
            canon_aggs, est = ck.aggs(raw_aggs, "aggs", 1, window["seconds"])
            if est > limits.max_total_buckets:
                _fail(CODE_LIMIT, f"the aggregation could produce about {est} buckets (maximum {limits.max_total_buckets})")
            out["aggs"] = canon_aggs

        # size / sort / source
        size = body.get("size")
        if has_aggs:
            if size is None:
                out["size"] = 0
                ck.applied.append("size defaulted to 0 for an aggregation query (buckets only)")
            elif size != 0 or isinstance(size, bool):
                _fail(CODE_LIMIT, "an aggregation query must use size 0 (rows and buckets are separate queries)")
            else:
                out["size"] = 0
            if "sort" in body:
                _fail(CODE_BAD_CLAUSE, "`sort` is not used by aggregation queries")
        else:
            if size is None:
                out["size"] = limits.default_hits_size
                ck.applied.append(f"size defaulted to {limits.default_hits_size}")
            else:
                if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= limits.max_hits_size:
                    _fail(CODE_LIMIT, f"size must be an integer from 1 to {limits.max_hits_size} for a row query")
                out["size"] = size
            if "sort" in body:
                out["sort"] = _check_sort(body["sort"], ck)
            else:
                out["sort"] = [{pc.entry.time_field: {"order": "desc"}}]
                ck.applied.append(f"sorted newest first by {pc.entry.time_field}")

        allowed_src = set(pc.entry.output_projection)
        if "_source" in body:
            src = body["_source"]
            if not isinstance(src, list) or not all(isinstance(s, str) for s in src) or not src:
                _fail(CODE_PROJECTION, "`_source` must be a non-empty list of field paths")
            bad = [s for s in src if s not in allowed_src]
            if bad:
                _fail(CODE_PROJECTION, f"`_source` fields not returnable for {pid!r}: {bad}. "
                                       f"Returnable: {sorted(allowed_src)}")
            out["_source"] = src
        elif not has_aggs:
            out["_source"] = list(pc.entry.output_projection)
            ck.applied.append("rows limited to the family's output projection")

        if "track_total_hits" in body:
            t = body["track_total_hits"]
            if not (isinstance(t, bool) or (isinstance(t, int) and 1 <= t <= limits.max_track_total_hits)):
                _fail(CODE_BAD_VALUE, "track_total_hits must be true/false or a bounded integer")
            out["track_total_hits"] = t

        if "timeout" in body:
            m = _TIMEOUT_RE.match(str(body["timeout"]))
            if not m or int(m.group(1)) * _WINDOW_UNITS[m.group(2)] > limits.search_timeout_seconds:
                _fail(CODE_LIMIT, f"timeout must be at most {limits.search_timeout}")
            out["timeout"] = body["timeout"]
        else:
            out["timeout"] = limits.search_timeout
            ck.applied.append(f"server-side timeout {limits.search_timeout} added (an expired timeout is reported as a partial result)")

        if "from" in body and body["from"] not in (0,):
            _fail(CODE_LIMIT, "paging is not allowed")

        return GuardResult(
            ok=True, query=out, index=pc.entry.index_pattern, pattern_id=pid,
            applied=tuple(dict.fromkeys(ck.applied)), warnings=tuple(dict.fromkeys(ck.warnings)),
            window=window, estimated_buckets=est, approximate_aggregations=tuple(ck.approx),
        )
    except GuardError as e:
        return GuardResult(ok=False, code=e.code, reason=e.reason, user_message=e.user_message, pattern_id=pid if isinstance(pid, str) else None)


def _check_sort(sort: Any, ck: _Checker) -> list:
    items = sort if isinstance(sort, list) else [sort]
    if not items or len(items) > 3:
        _fail(CODE_BAD_CLAUSE, "sort needs 1-3 entries")
    out = []
    for i, it in enumerate(items):
        if not isinstance(it, dict) or len(it) != 1:
            _fail(CODE_BAD_CLAUSE, f"sort[{i}]: expected {{field: 'asc'|'desc'}}")
        (path, spec), = it.items()
        ck.info(path, OP_SORT, f"sort[{i}]")
        if any(path == t or path.startswith(t + ".") for t in ck.entry.role_paths("text")):
            _fail(CODE_BAD_CLAUSE, f"sort[{i}]: free-text fields cannot be used for sorting")
        order = spec.get("order") if isinstance(spec, dict) else spec
        if order not in ("asc", "desc") or (isinstance(spec, dict) and set(spec) != {"order"}):
            _fail(CODE_BAD_CLAUSE, f"sort[{i}]: order must be 'asc' or 'desc'")
        out.append(it)
    return out
