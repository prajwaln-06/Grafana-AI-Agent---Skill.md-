"""
field_catalog.py

The OpenSearch runtime field catalog: what the LIVE mappings say exists, and
what each field can do right now. Discovery only -- permission (which
patterns/fields a generated query may use at all) lives in the index registry
(registry.py). This is the OpenSearch counterpart of Prometheus label
discovery (SKILL.md section 5, Principle 9): the model may not use a field
this catalog does not confirm.

Capabilities are DERIVED FROM THE MAPPING by Python -- never asserted by a
person, a skill file or the model:

  keyword        -> exists, term, group_by, sort
  text           -> exists, match; term/group_by/sort ONLY through a keyword
                    sub-field that the mapping actually defines (`.keyword` is
                    never assumed -- long strings may have none)
  date           -> exists, range, sort, group_by (date_histogram)
  numeric        -> exists, term, range, sort, group_by, metric
  boolean / ip   -> exists, term, group_by, sort (+ range for ip)
  anything else  -> no operations (objects are traversed, not leaves; `nested`
                    fields need nested queries this system does not generate)

`ops` maps each operation to the EFFECTIVE field path to use for it, so a
text field with a confirmed keyword sub-field has ops["term"] ==
"<path>.keyword". Multi-field sub-fields are also entered as their own fields
(with `parent` set) so a validator can check a path the model wrote
uniformly.

Merging daily indices: `<pattern>/_mapping` returns one mapping per concrete
index. The newest N (by name) are merged. A path present in only some of them
is kept (`present_in == "some"`); a path whose type FAMILY differs between
indices is a conflict and gets no operations; a keyword sub-field counts as
confirmed only if it exists in every index where its parent does.

Failure behaviour (FieldCatalogCache):
  * fresh cache entry            -> served, status "fresh"
  * fetch fails, entry <= stale  -> served, status "stale"
  * fetch fails, nothing usable  -> status "unavailable" with no fields
                                    (callers FAIL CLOSED; an unavailable
                                    catalog is never "assume the registry is
                                    right")
  * fetch succeeds but matches no index -> unavailable ("no_matching_indices"),
    distinct from a failed fetch. None (failure) is never confused with {}.
A failed fetch is not retried for `failure_backoff_seconds`, so an OpenSearch
outage does not add a full timeout to every request.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Callable

from app import opensearch_client as osc
from app.opensearch_catalog.registry import PatternEntry

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 600
DEFAULT_STALE_TTL_SECONDS = 3600
DEFAULT_MAX_INDICES = 7
DEFAULT_FAILURE_BACKOFF_SECONDS = 30

STATUS_FRESH = "fresh"
STATUS_STALE = "stale"
STATUS_UNAVAILABLE = "unavailable"

REASON_FETCH_FAILED = "fetch_failed"
REASON_NO_MATCHING_INDICES = "no_matching_indices"
REASON_REFRESH_FAILED = "refresh_failed"  # stale copy served because a refresh failed
REASON_REFRESHING = "refresh_in_progress"
REASON_REFRESH_INVALIDATED = "refresh_invalidated"

OP_EXISTS = "exists"
OP_TERM = "term"
OP_MATCH = "match"
OP_RANGE = "range"
OP_SORT = "sort"
OP_GROUP_BY = "group_by"
OP_METRIC = "metric"

_NUMERIC_TYPES = frozenset({"long", "integer", "short", "byte", "double", "float",
                            "half_float", "scaled_float", "unsigned_long"})
_DATE_TYPES = frozenset({"date", "date_nanos"})
_TEXT_TYPES = frozenset({"text", "match_only_text"})
_KEYWORD_TYPES = frozenset({"keyword", "constant_keyword"})


def type_family(mapping_type: str) -> str:
    if mapping_type in _TEXT_TYPES:
        return "text"
    if mapping_type in _KEYWORD_TYPES:
        return "keyword"
    if mapping_type in _DATE_TYPES:
        return "date"
    if mapping_type in _NUMERIC_TYPES:
        return "numeric"
    if mapping_type == "boolean":
        return "boolean"
    if mapping_type == "ip":
        return "ip"
    return "other"


@dataclass(frozen=True)
class FieldInfo:
    path: str
    type: str                          # mapping type of the most recent index that has it
    family: str                        # text | keyword | date | numeric | boolean | ip | other
    ops: dict[str, str]                # operation -> effective field path ({} = not usable)
    keyword_subfield: str | None = None   # confirmed exact-match sub-field path, if any
    keyword_ignore_above: int | None = None  # values longer than this are absent from the sub-field
    parent: str | None = None          # set for multi-field sub-fields
    present_in: str = "all"            # "all" | "some" of the merged indices
    conflict: bool = False             # type family differs between merged indices
    nested: bool = False               # lives under a `nested` field (no operations granted)

    def supports(self, op: str) -> bool:
        return op in self.ops


@dataclass(frozen=True)
class FieldCatalog:
    pattern_id: str
    discovery_status: str              # fresh | stale | unavailable
    reason: str | None = None          # why stale/unavailable (REASON_*), else None
    discovered_at: float | None = None  # epoch seconds of the successful discovery
    concrete_indices_seen: tuple[str, ...] = ()
    mapping_variation: str = "none"    # none | additive | type_conflict
    fields: dict[str, FieldInfo] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        """True when this catalog may be used to confirm fields (fresh or
        stale, with at least one field). `unavailable` -> callers fail closed."""
        return self.discovery_status in (STATUS_FRESH, STATUS_STALE) and bool(self.fields)

    def get(self, path: str) -> FieldInfo | None:
        return self.fields.get(path)

    def supports(self, path: str, op: str) -> bool:
        info = self.fields.get(path)
        return info is not None and info.supports(op)

    def field_for(self, path: str, op: str) -> str | None:
        """The effective path to use for `op` on `path` (e.g. the confirmed
        `.keyword` path for a term filter on a text field), or None."""
        info = self.fields.get(path)
        return info.ops.get(op) if info is not None else None

    def time_field_confirmed(self, time_field: str) -> bool:
        info = self.fields.get(time_field)
        return (info is not None and info.family == "date" and not info.conflict
                and OP_RANGE in info.ops)


# ---- mapping flattening -----------------------------------------------------------


@dataclass(frozen=True)
class _RawField:
    path: str
    type: str
    parent: str | None = None
    ignore_above: int | None = None
    nested: bool = False


def _walk(properties: dict, prefix: str, nested: bool, out: dict[str, _RawField]) -> None:
    for name, node in properties.items():
        if not isinstance(node, dict):
            continue
        path = f"{prefix}{name}"
        node_type = node.get("type")
        children = node.get("properties")
        if node_type in (None, "object") or node_type == "nested":
            if isinstance(children, dict) and children:
                _walk(children, f"{path}.", nested or node_type == "nested", out)
            # An object with no properties has no leaves; nothing to record.
            continue
        out[path] = _RawField(path=path, type=str(node_type), nested=nested)
        sub = node.get("fields")
        if isinstance(sub, dict):
            for sub_name, sub_node in sub.items():
                if isinstance(sub_node, dict) and sub_node.get("type"):
                    sub_path = f"{path}.{sub_name}"
                    ia = sub_node.get("ignore_above")
                    out[sub_path] = _RawField(
                        path=sub_path, type=str(sub_node["type"]), parent=path,
                        ignore_above=ia if isinstance(ia, int) else None, nested=nested,
                    )


def flatten_index_mapping(index_body: dict) -> dict[str, _RawField]:
    """One concrete index's `{"mappings": {"properties": ...}}` -> {path: raw
    field}. Tolerates a missing `mappings`, an index with no properties and
    property names that themselves contain dots."""
    out: dict[str, _RawField] = {}
    if not isinstance(index_body, dict):
        return out
    props = (index_body.get("mappings") or {}).get("properties")
    if isinstance(props, dict):
        _walk(props, "", False, out)
    return out


def _ops_for(family: str, keyword_sub: str | None, path: str) -> dict[str, str]:
    ops: dict[str, str] = {OP_EXISTS: path}
    if family == "keyword":
        ops.update({OP_TERM: path, OP_GROUP_BY: path, OP_SORT: path})
    elif family == "text":
        ops[OP_MATCH] = path
        if keyword_sub:
            ops.update({OP_TERM: keyword_sub, OP_GROUP_BY: keyword_sub, OP_SORT: keyword_sub})
    elif family == "date":
        ops.update({OP_RANGE: path, OP_SORT: path, OP_GROUP_BY: path})
    elif family == "numeric":
        ops.update({OP_TERM: path, OP_RANGE: path, OP_SORT: path, OP_GROUP_BY: path, OP_METRIC: path})
    elif family == "boolean":
        ops.update({OP_TERM: path, OP_GROUP_BY: path, OP_SORT: path})
    elif family == "ip":
        ops.update({OP_TERM: path, OP_RANGE: path, OP_GROUP_BY: path, OP_SORT: path})
    else:
        return {}
    return ops


def build_field_catalog(pattern_id: str, mapping: dict, *,
                        max_indices: int = DEFAULT_MAX_INDICES,
                        discovered_at: float | None = None) -> FieldCatalog:
    """Pure function: raw `<pattern>/_mapping` response -> FieldCatalog.

    Returns an UNAVAILABLE catalog (reason no_matching_indices) when the
    response holds no usable (non-system) index or yields no fields at all. A
    failed fetch is `None` and is handled by the cache, never passed here."""
    names = sorted((n for n in mapping if isinstance(n, str) and not n.startswith(".")), reverse=True)
    selected = names[: max(1, max_indices)]
    if not selected:
        return FieldCatalog(pattern_id=pattern_id, discovery_status=STATUS_UNAVAILABLE,
                            reason=REASON_NO_MATCHING_INDICES)

    per_index = {name: flatten_index_mapping(mapping[name]) for name in selected}
    all_paths: set[str] = set()
    for flat in per_index.values():
        all_paths.update(flat)
    if not all_paths:
        return FieldCatalog(pattern_id=pattern_id, discovery_status=STATUS_UNAVAILABLE,
                            reason=REASON_NO_MATCHING_INDICES,
                            concrete_indices_seen=tuple(selected))

    total = len(selected)
    # selected is newest-first, so the first entry found for a path is the most recent.
    def occurrences(path: str) -> list[tuple[str, _RawField]]:
        return [(n, per_index[n][path]) for n in selected if path in per_index[n]]

    # Pass 1: non-sub-field paths.
    merged: dict[str, dict] = {}
    for path in all_paths:
        occ = occurrences(path)
        raws = [r for _, r in occ]
        if any(r.parent for r in raws):
            continue  # sub-fields are resolved in pass 2
        families = {type_family(r.type) for r in raws}
        merged[path] = {
            "raw": raws[0],
            "indices": {n for n, _ in occ},
            "conflict": len(families) > 1,
            "nested": any(r.nested for r in raws),
        }

    # Pass 2: sub-fields. Confirmed only if present in every index where the parent is.
    confirmed_sub: dict[str, tuple[str, _RawField]] = {}   # sub path -> (parent, newest raw)
    keyword_for_parent: dict[str, str] = {}
    for path in sorted(all_paths):
        occ = occurrences(path)
        raws = [r for _, r in occ]
        if not any(r.parent for r in raws):
            continue
        parent = raws[0].parent
        parent_info = merged.get(parent)
        if parent_info is None:
            continue
        if not parent_info["indices"] <= {n for n, _ in occ}:
            continue  # missing from some index that has the parent -> not confirmed
        if len({type_family(r.type) for r in raws}) > 1:
            continue
        confirmed_sub[path] = (parent, raws[0])
    # A parent's exact-match sub-field is a CONFIRMED keyword-typed sub-field:
    # `<parent>.keyword` if it is one, otherwise the only one there is. Several
    # keyword sub-fields with none named `keyword` is ambiguous, so none is
    # chosen (the parent then supports no exact match) -- never guess.
    keyword_candidates: dict[str, list[str]] = {}
    for sub_path, (parent, raw) in sorted(confirmed_sub.items()):
        if type_family(raw.type) == "keyword":
            keyword_candidates.setdefault(parent, []).append(sub_path)
    for parent, cands in keyword_candidates.items():
        if f"{parent}.keyword" in cands:
            keyword_for_parent[parent] = f"{parent}.keyword"
        elif len(cands) == 1:
            keyword_for_parent[parent] = cands[0]

    fields: dict[str, FieldInfo] = {}
    any_conflict = False
    any_partial = False
    for path, info in merged.items():
        raw: _RawField = info["raw"]
        family = type_family(raw.type)
        present = "all" if len(info["indices"]) == total else "some"
        any_partial = any_partial or present == "some"
        conflict = info["conflict"]
        any_conflict = any_conflict or conflict
        kw = keyword_for_parent.get(path)
        kw_ignore = confirmed_sub[kw][1].ignore_above if kw else None
        usable = not conflict and not info["nested"]
        fields[path] = FieldInfo(
            path=path, type=raw.type, family=family,
            ops=_ops_for(family, kw, path) if usable else {},
            keyword_subfield=kw if usable else None,
            keyword_ignore_above=kw_ignore if usable else None,
            present_in=present, conflict=conflict, nested=info["nested"],
        )
    for sub_path, (parent, raw) in confirmed_sub.items():
        parent_info = fields.get(parent)
        if parent_info is None:
            continue
        family = type_family(raw.type)
        usable = not parent_info.conflict and not parent_info.nested
        fields[sub_path] = FieldInfo(
            path=sub_path, type=raw.type, family=family,
            ops=_ops_for(family, None, sub_path) if usable else {},
            keyword_ignore_above=raw.ignore_above, parent=parent,
            present_in=parent_info.present_in, conflict=False, nested=parent_info.nested,
        )

    variation = "type_conflict" if any_conflict else ("additive" if any_partial else "none")
    return FieldCatalog(
        pattern_id=pattern_id, discovery_status=STATUS_FRESH, reason=None,
        discovered_at=discovered_at, concrete_indices_seen=tuple(selected),
        mapping_variation=variation, fields=fields,
    )


# ---- cache -------------------------------------------------------------------------


MappingFetcher = Callable[[str], "dict | None"]


def make_mapping_fetcher(base_url: str, *, auth: tuple[str, str] | None = None,
                         timeout: float = osc.DEFAULT_TIMEOUT_SECONDS) -> MappingFetcher:
    """Fetcher for FieldCatalogCache backed by GET /<pattern>/_mapping.
    Returns None on any failure (opensearch_client.get_field_mapping's contract)."""
    def fetch(index_pattern: str) -> dict | None:
        return osc.get_field_mapping(base_url, index_pattern, timeout=timeout, auth=auth)
    return fetch


@dataclass
class _Refresh:
    finished: threading.Event = field(default_factory=threading.Event)
    invalidated: bool = False
    catalog: FieldCatalog | None = None
    warm_after: PatternEntry | None = None


class FieldCatalogCache:
    """Per-pattern TTL cache with a single refresh and bounded stale reads."""

    def __init__(self, fetch_mapping: MappingFetcher, *,
                 ttl_seconds: float = DEFAULT_TTL_SECONDS,
                 stale_ttl_seconds: float = DEFAULT_STALE_TTL_SECONDS,
                 max_indices: int = DEFAULT_MAX_INDICES,
                 failure_backoff_seconds: float = DEFAULT_FAILURE_BACKOFF_SECONDS,
                 clock: Callable[[], float] = time.time):
        if stale_ttl_seconds < ttl_seconds:
            raise ValueError("stale_ttl_seconds must be >= ttl_seconds")
        self._fetch = fetch_mapping
        self._ttl = ttl_seconds
        self._stale_ttl = stale_ttl_seconds
        self._max_indices = max_indices
        self._backoff = failure_backoff_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._good: dict[str, FieldCatalog] = {}          # last successful catalog per pattern_id
        self._last_failure: dict[str, float] = {}          # pattern_id -> time of last failed fetch
        self._last_failure_reason: dict[str, str] = {}
        self._refreshes: dict[str, _Refresh] = {}

    def invalidate(self, pattern_id: str | None = None) -> None:
        """Discard data and prevent outstanding fetches from publishing it again."""
        with self._lock:
            if pattern_id is None:
                self._good.clear()
                self._last_failure.clear()
                self._last_failure_reason.clear()
                for refresh in self._refreshes.values():
                    refresh.invalidated = True
                    refresh.warm_after = None
            else:
                self._good.pop(pattern_id, None)
                self._last_failure.pop(pattern_id, None)
                self._last_failure_reason.pop(pattern_id, None)
                if refresh := self._refreshes.get(pattern_id):
                    refresh.invalidated = True
                    refresh.warm_after = None

    def snapshot(self) -> dict[str, dict]:
        """Read-only view of what is cached, per pattern_id, for an operator status
        page. Never fetches anything (calling it cannot reach OpenSearch)."""
        now = self._clock()
        with self._lock:
            good = dict(self._good)
            fails = dict(self._last_failure)
            reasons = dict(self._last_failure_reason)
        out: dict[str, dict] = {}
        for pid in sorted(set(good) | set(fails)):
            cat = good.get(pid)
            age = (now - cat.discovered_at) if cat is not None and cat.discovered_at is not None else None
            if cat is None:
                state = STATUS_UNAVAILABLE
            elif age is not None and age < self._ttl:
                state = STATUS_FRESH
            elif age is not None and age <= self._stale_ttl:
                state = STATUS_STALE
            else:
                state = STATUS_UNAVAILABLE
            out[pid] = {
                "cached_status": state,
                "age_seconds": round(age, 1) if age is not None else None,
                "fields": len(cat.fields) if cat is not None else 0,
                "concrete_indices_seen": list(cat.concrete_indices_seen) if cat is not None else [],
                "mapping_variation": cat.mapping_variation if cat is not None else None,
                "last_failure_seconds_ago": round(now - fails[pid], 1) if pid in fails else None,
                "last_failure_reason": reasons.get(pid),
            }
        return out

    def get(self, entry: PatternEntry) -> FieldCatalog:
        pid = entry.pattern_id
        now = self._clock()
        with self._lock:
            cached = self._good.get(pid)
            age = self._age(cached, now)
            if age < self._ttl:
                return cached
            if self._in_backoff(pid, now):
                return self._degraded(pid, cached, now, self._last_failure_reason[pid])
            refresh = self._refreshes.get(pid)
            starts_refresh = refresh is None
            if starts_refresh:
                refresh = self._refreshes[pid] = _Refresh()

        if age <= self._stale_ttl:
            if starts_refresh:
                self._start_background(entry, refresh, now)
            return replace(cached, discovery_status=STATUS_STALE, reason=REASON_REFRESHING)

        # Cold or expired: wait for a confirmed view and fail closed on failure.
        if starts_refresh:
            self._refresh(entry, refresh, now)
        else:
            refresh.finished.wait()
        return refresh.catalog or FieldCatalog(
            pattern_id=pid, discovery_status=STATUS_UNAVAILABLE, reason=REASON_REFRESH_INVALIDATED)

    def prewarm(self, entries: Iterable[PatternEntry]) -> None:
        """Start discovery for enabled patterns without delaying service startup."""
        for entry in entries:
            self._prewarm_pattern(entry)

    def _prewarm_pattern(self, entry: PatternEntry) -> None:
        pid, now = entry.pattern_id, self._clock()
        with self._lock:
            if self._age(self._good.get(pid), now) < self._ttl or self._in_backoff(pid, now):
                return
            pending = self._refreshes.get(pid)
            if pending is not None:
                if pending.invalidated:
                    pending.warm_after = entry
                return
            refresh = self._refreshes[pid] = _Refresh()
        self._start_background(entry, refresh, now)

    # -- internals --

    @staticmethod
    def _age(cached: FieldCatalog | None, now: float) -> float:
        if cached is None or cached.discovered_at is None:
            return float("inf")
        return now - cached.discovered_at

    def _in_backoff(self, pid: str, now: float) -> bool:
        return pid in self._last_failure and now - self._last_failure[pid] < self._backoff

    def _start_background(self, entry: PatternEntry, refresh: _Refresh, now: float) -> None:
        try:
            threading.Thread(target=self._refresh, args=(entry, refresh, now), daemon=True,
                             name=f"opensearch-mapping-{entry.pattern_id}").start()
        except RuntimeError:
            logger.exception("Could not start OpenSearch field discovery for %r", entry.pattern_id)
            self._publish(entry.pattern_id, refresh, now, None, REASON_FETCH_FAILED)

    def _refresh(self, entry: PatternEntry, refresh: _Refresh, now: float) -> None:
        pid = entry.pattern_id
        mapping = self._safe_fetch(entry.index_pattern)
        catalog = None
        if mapping is not None:
            try:
                catalog = build_field_catalog(pid, mapping, max_indices=self._max_indices, discovered_at=now)
            except Exception:
                logger.exception("Could not build the OpenSearch field catalog for %r", pid)
        reason = (catalog.reason or REASON_NO_MATCHING_INDICES) if catalog is not None else REASON_FETCH_FAILED
        self._publish(pid, refresh, now, catalog, reason)

    def _publish(self, pid: str, refresh: _Refresh, now: float, catalog: FieldCatalog | None, reason: str) -> None:
        failed = False
        followup = None
        with self._lock:
            if not refresh.invalidated:
                if catalog is not None and catalog.discovery_status == STATUS_FRESH:
                    self._good[pid] = catalog
                    self._last_failure.pop(pid, None)
                    self._last_failure_reason.pop(pid, None)
                    refresh.catalog = catalog
                else:
                    self._last_failure[pid] = now
                    self._last_failure_reason[pid] = reason
                    refresh.catalog = self._degraded(pid, self._good.get(pid), self._clock(), reason)
                    failed = True
            if refresh.warm_after is None:
                del self._refreshes[pid]
            else:
                followup = self._refreshes[pid] = _Refresh()
            refresh.finished.set()
        if failed:
            logger.warning("OpenSearch field discovery failed for %r (%s)", pid, reason)
        if followup is not None:
            self._start_background(refresh.warm_after, followup, self._clock())

    def _safe_fetch(self, index_pattern: str) -> dict | None:
        try:
            result = self._fetch(index_pattern)
        except Exception:  # a fetcher bug or transport surprise must not break the request
            logger.exception("OpenSearch mapping fetch raised for pattern %r", index_pattern)
            return None
        if result is not None and not isinstance(result, dict):
            logger.warning("OpenSearch mapping fetch for %r returned a non-object; treating as failure",
                           index_pattern)
            return None
        return result

    def _degraded(self, pid: str, cached: FieldCatalog | None, now: float, reason: str) -> FieldCatalog:
        if cached is not None and cached.discovered_at is not None \
                and now - cached.discovered_at <= self._stale_ttl:
            return replace(cached, discovery_status=STATUS_STALE, reason=REASON_REFRESH_FAILED)
        return FieldCatalog(pattern_id=pid, discovery_status=STATUS_UNAVAILABLE, reason=reason)


# ---- registry x catalog view ---------------------------------------------------------


@dataclass(frozen=True)
class PatternFieldView:
    """What a generated query may actually use for one pattern right now: the
    registry's permission intersected with the live catalog's confirmation.
    This is the object the pipeline will put in the Generator prompt and hand
    to the validator.

    `usable` is False (callers fail closed) when discovery is unavailable or
    the registry's time field is not a confirmed date field."""
    pattern_id: str
    discovery_status: str
    reason: str | None
    mapping_variation: str
    time_field: str
    time_field_confirmed: bool
    allowed_fields: dict[str, FieldInfo]       # registry-queryable (and their confirmed sub-fields) that exist live
    missing_fields: tuple[str, ...]            # registry-queryable but absent from the live mapping
    roles_confirmed: dict[str, tuple[str, ...]]
    dropped_role_bindings: tuple[tuple[str, str], ...]   # (role, path) bound in the registry but not live
    group_by_fields: dict[str, str]            # groupable path -> effective path to group on
    warnings: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return (self.discovery_status in (STATUS_FRESH, STATUS_STALE)
                and self.time_field_confirmed and bool(self.allowed_fields))


def build_field_view(entry: PatternEntry, catalog: FieldCatalog) -> PatternFieldView:
    """Intersect registry permission with live confirmation. Discovery widens
    knowledge, never permission: a live field outside `queryable_fields` is
    not in the view. A registry binding absent from the live mapping is
    dropped WITH a warning, never used silently."""
    warnings: list[str] = []
    allowed: dict[str, FieldInfo] = {}
    missing: list[str] = []
    for path in sorted(entry.queryable_fields):
        info = catalog.get(path)
        if info is None:
            missing.append(path)
            continue
        allowed[path] = info
        if info.keyword_subfield:
            sub = catalog.get(info.keyword_subfield)
            if sub is not None:
                allowed[info.keyword_subfield] = sub
        if info.conflict:
            warnings.append(f"{path}: mapping type differs between indices; no operations allowed")
    if missing:
        warnings.append("registry fields not in the live mapping: " + ", ".join(missing))

    roles_confirmed: dict[str, tuple[str, ...]] = {}
    dropped: list[tuple[str, str]] = []
    for role, paths in entry.roles.items():
        live = tuple(p for p in paths if p in allowed)
        dropped.extend((role, p) for p in paths if p not in allowed)
        if live:
            roles_confirmed[role] = live
    for role, path in dropped:
        warnings.append(f"role {role!r} binding {path!r} dropped: not in the live mapping")

    group_by: dict[str, str] = {}
    for path in sorted(entry.groupable_fields - entry.never_group):
        info = allowed.get(path)
        if info is not None and OP_GROUP_BY in info.ops:
            group_by[path] = info.ops[OP_GROUP_BY]

    time_ok = catalog.time_field_confirmed(entry.time_field)
    if catalog.discovery_status != STATUS_UNAVAILABLE and not time_ok:
        warnings.append(f"time field {entry.time_field!r} is not a confirmed date field")
    if catalog.discovery_status == STATUS_STALE:
        detail = "refresh in progress" if catalog.reason == REASON_REFRESHING else "the last refresh failed"
        warnings.append(f"field catalog is stale: {detail}")

    return PatternFieldView(
        pattern_id=entry.pattern_id, discovery_status=catalog.discovery_status, reason=catalog.reason,
        mapping_variation=catalog.mapping_variation, time_field=entry.time_field,
        time_field_confirmed=time_ok, allowed_fields=allowed, missing_fields=tuple(missing),
        roles_confirmed=roles_confirmed, dropped_role_bindings=tuple(dropped),
        group_by_fields=group_by, warnings=tuple(warnings),
    )
