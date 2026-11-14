"""
plan_cache.py

The OpenSearch QUERY-PLAN cache: a process-wide, in-memory, LRU-bounded store of VALIDATED query
plans (the guard-approved contract entry), so an identical or equivalent question can skip the
Router/Generator LLM calls while the query is still re-validated and EXECUTED AGAIN for fresh data.

It is NOT the result cache (query_cache.ResultCache), which stores completed answers and skips
execution. The two share the matching/fingerprint/guard utilities of query_cache.py and nothing else:
separate instances, separate keys, separate metadata (`query_plan_cache` vs `result_cache`).

Safety model (every doubt is a miss)
------------------------------------
1. WHAT is stored: the contract entry of one complete, successful, non-partial OpenSearch execution
   (decided by query_cache.assess_executed, so the completeness rules are the result cache's own),
   restricted further to a SINGLE entry (no multi/compound/correlated or dependency-resolved
   responses) whose window is RELATIVE ("now-15m"). No result rows, no credentials, no execution block
   are stored; the DSL keeps its relative time expressions verbatim, so a hit queries the clock at
   hit time, not at generation time.
2. WHEN it may be reused: Level 1 identical canonical wording; Level 2 reworded and signature-equal
   in every facet (query_cache.equivalent) -- offered only for entries query_cache judged
   semantic-safe (absence / recurrence analyses). Same scope only.
3. Reuse re-proves the plan: schema version, registry pattern still enabled, registry / live field
   view / configuration / skill fingerprints unchanged, and the stored canonical query still passes
   guard.check_entry unchanged (query_cache.revalidate_entry). The executor then runs the full guard a
   second time before anything is sent. Any failure evicts the entry and the normal pipeline runs.
4. Bounded: at most `max_entries`; the least recently USED entry is evicted first. Validity is decided
   by revalidation, not by a TTL, so there is deliberately no maximum age.
5. Any internal failure is a miss / not-stored, never a failed request.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from app.opensearch_catalog import query_cache
from app.opensearch_catalog.query_cache import Assessment, Fingerprints, PartFacts

logger = logging.getLogger(__name__)

PLAN_SCHEMA_VERSION = 1
MAX_PLAN_BYTES = 200_000

Revalidator = Callable[["PlanEntry"], "str | None"]


def plan_eligibility(assessment: Assessment, executed: Any) -> str:
    """"" if this executed result may seed a plan, else why not. Stricter than the result cache."""
    if not assessment.cacheable:
        return assessment.reason or "result is not cacheable"
    if assessment.mode != "single" or len(assessment.parts) != 1 or not isinstance(executed, dict):
        return "only a single OpenSearch entry is stored as a plan (multi/compound responses are not)"
    if "correlation" in executed:
        return "correlated responses are not stored as a plan"
    if not assessment.parts[0].window_relative:
        return "the plan has an absolute time window; only relative windows are stored"
    return ""


def plan_shell(executed: dict) -> dict:
    """The validated contract entry without anything produced by executing it."""
    return {k: v for k, v in executed.items() if k != "execution"}


def check_consistency(entry: PlanEntry) -> str | None:
    """None if the contract that WILL be executed is the very plan that was fingerprinted and
    guard-checked: same pattern, same canonical DSL, same analysis, an `ok` OpenSearch entry with no
    execution block. (query_cache.revalidate_entry guards `entry.parts`; the executor runs
    `entry.contract_json`; this proves they are one and the same.)"""
    try:
        contract = json.loads(entry.contract_json)
        part = entry.parts[0]
        if len(entry.parts) != 1 or not isinstance(contract, dict):
            return "stored plan is malformed"
        if contract.get("status") != "ok" or (contract.get("data_source") or "").strip().lower() != "opensearch" \
                or "execution" in contract:
            return "stored plan is not a validated OpenSearch entry"
        if contract.get("pattern_id") != part.pattern_id or entry.pattern_id != part.pattern_id:
            return "stored plan names a different pattern than the one that was checked"
        if contract.get("query") != json.loads(part.query_json):
            return "stored plan differs from the query that was checked"
        analysis = json.loads(part.analysis_json) if part.analysis_json else None
        if contract.get("analysis") != analysis:
            return "stored plan differs from the analysis that was checked"
        return None
    except (ValueError, TypeError, IndexError):
        return "stored plan is malformed"


@dataclass(frozen=True)
class PlanEntry:
    entry_id: int
    scope: str
    schema_version: int
    question: str
    canonical_text: str
    semantic_ok: bool
    pattern_id: str
    analysis_type: str | None
    parts: tuple[PartFacts, ...]          # exactly one; shaped for query_cache.revalidate_entry
    contract_json: str                    # validated entry, relative time kept verbatim
    fingerprints: Fingerprints
    created_wall: float
    created_at: float                     # monotonic


@dataclass(frozen=True)
class PlanHit:
    contract: dict                        # fresh deserialisation: callers cannot mutate the cache
    match: str                            # "exact" | "semantic"
    entry_id: int
    age_seconds: float


@dataclass(frozen=True)
class PlanLookup:
    hit: PlanHit | None = None
    invalidation_reason: str | None = None


class QueryPlanCache:
    def __init__(self, *, max_entries: int = 500, semantic_enabled: bool = True,
                 aliases: Mapping[str, Mapping[str, str]] | None = None,
                 clock: Callable[[], float] = time.monotonic, wall_clock: Callable[[], float] = time.time):
        self.max_entries = int(max_entries)
        self.semantic_enabled = bool(semantic_enabled)
        self.aliases = {k: dict(v) for k, v in (aliases or {}).items()}
        self._clock, self._wall = clock, wall_clock
        self._lock = threading.RLock()
        self._entries: OrderedDict[int, PlanEntry] = OrderedDict()     # order = least -> most recently used
        self._by_key: dict[tuple[str, str], int] = {}
        self._next_id = 1
        self._stats: Counter = Counter()

    # ---- bookkeeping ----

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._by_key.clear()

    def snapshot(self) -> dict:
        """Counts only: never a question, a plan or a scope."""
        with self._lock:
            return {"enabled": True, "entries": len(self._entries), "max_entries": self.max_entries,
                    "semantic_enabled": self.semantic_enabled, **dict(self._stats)}

    def _evict_locked(self, entry_id: int) -> None:
        e = self._entries.pop(entry_id, None)
        if e is not None and self._by_key.get((e.scope, e.canonical_text)) == entry_id:
            del self._by_key[(e.scope, e.canonical_text)]

    # ---- store ----

    def put(self, question: str, scope: str, executed: dict, assessment: Assessment,
            fingerprints: Fingerprints | None) -> tuple[bool, str]:
        """(stored, reason). Never raises. A live duplicate (same scope + canonical wording) is kept,
        so concurrent identical misses cannot churn the cache."""
        try:
            why = plan_eligibility(assessment, executed)
            if why:
                self._stats["not_stored"] += 1
                return False, why
            if fingerprints is None or not isinstance(scope, str) or not scope:
                self._stats["not_stored"] += 1
                return False, "no fingerprints or scope"
            try:
                contract_json = json.dumps(plan_shell(executed), ensure_ascii=False, allow_nan=False,
                                           separators=(",", ":"))
            except (TypeError, ValueError):
                self._stats["not_stored"] += 1
                return False, "plan is not JSON-serialisable"
            if len(contract_json.encode("utf-8")) > MAX_PLAN_BYTES:
                self._stats["not_stored"] += 1
                return False, "plan too large to cache"
            part = assessment.parts[0]
            canonical_text = query_cache.build_signature(question).canonical_text
            with self._lock:
                if (scope, canonical_text) in self._by_key:
                    self._stats["duplicate_not_stored"] += 1
                    return False, "a plan for this question already exists"
                while len(self._entries) >= self.max_entries:
                    oldest = next(iter(self._entries))
                    self._evict_locked(oldest)
                    self._stats["evicted_capacity"] += 1
                eid = self._next_id
                self._next_id += 1
                self._entries[eid] = PlanEntry(
                    entry_id=eid, scope=scope, schema_version=PLAN_SCHEMA_VERSION, question=question,
                    canonical_text=canonical_text, semantic_ok=assessment.semantic_ok, pattern_id=part.pattern_id,
                    analysis_type=part.analysis_type, parts=(part,), contract_json=contract_json,
                    fingerprints=fingerprints, created_wall=self._wall(), created_at=self._clock())
                self._by_key[(scope, canonical_text)] = eid
                self._stats["stored"] += 1
            return True, ""
        except Exception:  # noqa: BLE001 -- caching must never fail a request
            logger.exception("query plan cache store failed")
            self._stats["errors"] += 1
            return False, "internal error"

    # ---- lookup ----

    def lookup(self, question: str, scope: str, revalidate: Revalidator) -> PlanLookup:
        """Never raises. `invalidation_reason` is set when a matching entry was found but failed
        revalidation (and was evicted)."""
        reasons: list[str] = []
        try:
            hit = self._lookup(question, scope, revalidate, reasons)
        except Exception:  # noqa: BLE001
            logger.exception("query plan cache lookup failed")
            self._stats["errors"] += 1
            return PlanLookup()
        self._stats["hits_" + hit.match if hit else "misses"] += 1
        return PlanLookup(hit, reasons[-1] if (reasons and hit is None) else None)

    def _lookup(self, question: str, scope: str, revalidate: Revalidator, reasons: list[str]) -> PlanHit | None:
        if not isinstance(question, str) or not question.strip() or not isinstance(scope, str) or not scope:
            return None
        sig = query_cache.build_signature(question)
        with self._lock:
            candidates = [e for e in self._entries.values() if e.scope == scope]

        exact = sorted((e for e in candidates if e.canonical_text == sig.canonical_text), key=lambda e: -e.created_at)
        for e in exact:
            if self._accept(e, revalidate, reasons):
                return self._hit(e, "exact")

        if not (self.semantic_enabled and sig.understood):
            return None
        tried = {e.entry_id for e in exact}
        matches: list[PlanEntry] = []
        for e in candidates:
            if e.entry_id in tried or not e.semantic_ok:
                continue
            try:
                al = query_cache.alias_items_for(self.aliases, [e.pattern_id])
                equal = query_cache.equivalent(query_cache.build_signature(question, al),
                                               query_cache.build_signature(e.question, al))
            except Exception:  # noqa: BLE001 -- a matching failure is a miss for this candidate
                logger.exception("plan matching failed; treating as a miss")
                self._stats["match_errors"] += 1
                continue
            if equal:
                matches.append(e)
        for e in sorted(matches, key=lambda e: -e.created_at):
            if self._accept(e, revalidate, reasons):
                return self._hit(e, "semantic")
        return None

    def _accept(self, entry: PlanEntry, revalidate: Revalidator, reasons: list[str]) -> bool:
        if entry.schema_version != PLAN_SCHEMA_VERSION:
            problem: str | None = f"unsupported plan schema version {entry.schema_version}"
        else:
            try:
                problem = check_consistency(entry) or revalidate(entry)    # revalidate: outside the lock
            except Exception as exc:  # noqa: BLE001
                problem = f"revalidation error: {type(exc).__name__}"
        if problem:
            logger.info("cached plan %s not reused: %s", entry.entry_id, problem)
            self.invalidate(entry.entry_id)
            self._stats["revalidation_failed"] += 1
            reasons.append(problem)
            return False
        return True

    def invalidate(self, entry_id: int) -> None:
        with self._lock:
            self._evict_locked(entry_id)

    def _hit(self, entry: PlanEntry, match: str) -> PlanHit:
        with self._lock:
            if entry.entry_id in self._entries:
                self._entries.move_to_end(entry.entry_id)          # LRU: most recently used
        return PlanHit(contract=json.loads(entry.contract_json), match=match, entry_id=entry.entry_id,
                       age_seconds=round(self._clock() - entry.created_at, 3))


def annotate(result: Any, info: dict) -> None:
    """Adds `execution.metadata.query_plan_cache` (never touches `result_cache`)."""
    if isinstance(result, dict) and isinstance(result.get("execution"), dict):
        ex = result["execution"]
        if not isinstance(ex.get("metadata"), dict):
            ex["metadata"] = {}
        ex["metadata"]["query_plan_cache"] = dict(info)
