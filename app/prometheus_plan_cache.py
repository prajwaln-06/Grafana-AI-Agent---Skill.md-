"""
prometheus_plan_cache.py

The Prometheus QUERY-PLAN cache: a process-wide, in-memory, LRU-bounded store of VALIDATED PromQL
plans (the contract entry the pipeline validated and executed successfully), so an identical or
equivalent question can skip the Router/Generator LLM calls while the plan is still re-validated and
EXECUTED AGAIN against Prometheus for fresh data. It is the Prometheus counterpart of
`opensearch_catalog.plan_cache` and deliberately reuses its machinery rather than a second matcher:

  * the cache class (`QueryPlanCache`: bounded LRU, exact -> semantic lookup, lost-race handling,
    exact statistics) is subclassed, not copied;
  * question matching is `opensearch_catalog.query_cache.build_signature` / `equivalent` (Level 1
    identical canonical wording; Level 2 equal in every facet, deterministic, no embeddings). The only
    Prometheus-specific part is VOCABULARY: `prometheus_concepts` hands the shared matcher its phrase table
    (cpu usage / cpu utilization / processor usage -> `cpu_utilization`, ...) so ordinary synonyms need no
    declared alias. A concept only makes a reworded question ELIGIBLE; the six-facet equality, the plan's
    metric check, catalog ownership and revalidation below all still apply (docs/PROMETHEUS_CONCEPTS.md).

What is Prometheus-specific (an OpenSearch plan depends on registry + live mappings; a PromQL plan on
other things) is everything below that:

Safety model (every doubt is a miss)
------------------------------------
1. WHAT is stored: the contract entry of one successful, NON-EMPTY Prometheus execution that is a
   plain `status: "ok"` single entry with a relative time window. Never results, never the execution
   block, never credentials. Multi / compound / dependency-resolved / correlated responses,
   `panic_mode_best_effort`, alert proposals, clarification turns, errors and empty results are not
   stored (see `plan_eligibility`). Relative time ("now-1h") is kept verbatim, so a hit evaluates the
   window at hit time, not at generation time.
2. WHEN it may be reused: Level 1 identical canonical wording; Level 2 a reworded question equal in
   every signature facet AND confirmed by the catalog (`reference_ambiguity`): the catalog's own
   deterministic ranking of the new wording must still put the plan's reference on top, uniquely, and
   still contain every metric the plan uses. Level 2 is offered only for entries whose own question was
   cross-checked against the query that really ran (`semantic_consistency`: window, literals, and that
   every built-in concept in the question is backed by a plan metric). Same scope only.
3. Reuse re-proves the plan (`revalidate_entry`), cheapest check first: schema version; configuration
   fingerprint; skill-text fingerprint (SKILL.md + the reference + its overview + prometheus
   fundamentals); catalog fingerprint (the full catalog entry of every metric the plan uses -- type,
   help, unit, STATUS, reference, keywords); the catalog's ROUTING view of the question (every metric
   and reference the pipeline's own uncapped narrowing search offers for it, so a new or changed
   COMPETING entry invalidates exact hits too -- the catalog is republished in the background and
   nothing clears this cache when that happens); live label-key fingerprint of those metrics; and
   finally the REAL `validator.validate_contract` is run on the stored plan against the current metric
   directory, label keys/values and catalog statuses. The Prometheus executor has no guard of its own,
   so this validator run is the thing that stands between a stale plan and a query. Any failure evicts
   the entry and the normal pipeline runs.
4. Bounded: at most `max_entries`; the least recently USED entry is evicted first. Validity is decided
   by revalidation, not by a TTL, so there is deliberately no maximum age.
5. Any internal failure is a miss / not-stored, never a failed request.

Not cached here, by design: Prometheus DATA. Every hit queries Prometheus again.

The catalog a plan is judged against is the one it was GENERATED under: on a miss the agent reads the
published catalog snapshot once, before the pipeline starts, and passes that same snapshot to the
pipeline (`CatalogBaseline`). After execution the plan is stored only if the catalog now in force still
agrees with it (routing view of the question and the full entry of every plan metric, compared by content
so an identical republish is harmless); otherwise the answer is returned normally and not cached. The
fingerprints themselves are computed from the generation snapshot. A missing catalog fails closed
everywhere it is needed: nothing is stored without one, and a stored plan is not reused while it is
unavailable (routing and status validation could not be proven, and "no status map" must never make the
validator skip its status check).

A stored plan must also be the plan that was checked: `check_consistency` compares the stored query,
reference, metrics, query type, family AND the time expressions (from/to/step, or the instant) with the
facts captured at insertion, so a plan whose stored range was altered is rejected even if the altered
range would itself be valid.

Coupling note: `PrometheusPlanCache` relies on the base class's `_lookup`, `_hit`, `_bump`,
`_evict_locked` and the attribute names of its entries (see `PromPlanEntry`); tests pin this so a change
to the OpenSearch cache cannot silently alter Prometheus behaviour.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import re
import threading
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Mapping

from app import label_discovery, pipeline, prometheus_concepts as concepts, time_utils, validator
from app.catalog import search as catalog_search
from app.opensearch_catalog import plan_cache as _os_plan
from app.opensearch_catalog import query_cache as _qc

logger = logging.getLogger(__name__)

PLAN_SCHEMA_VERSION = 1
MAX_PLAN_BYTES = 200_000
MAX_PLAN_METRICS = 16

class CatalogUnavailable(Exception):
    """The catalog is required to prove a plan safe (routing view, per-metric status/metadata) and is
    not available. Always handled as "do not store" / "do not reuse"."""


PlanHit = _os_plan.PlanHit
PlanLookup = _os_plan.PlanLookup

_RANGE_SELECTOR_RE = re.compile(r"\[\s*(\d+)\s*(ms|s|m|h|d|w)\s*(?::\s*\d+\s*(?:ms|s|m|h|d|w)\s*)?\]")
_UNIT_SECONDS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def _digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


# =====================================================================================================
# 1. what a plan is, and whether it may be stored
# =====================================================================================================


@dataclass(frozen=True)
class PlanFacts:
    """The machine-checkable facts of one validated plan (what revalidation and matching need)."""
    reference_used: str
    family: str                       # exporter directory ("node-exporter"): the alias family
    metrics: tuple[str, ...]          # every metric the plan names (measurement_used.name + source_metrics)
    query: str
    query_type: str                   # "instant" | "range"
    window_seconds: float | None      # span of a range query; None for an instant query
    time_expr: tuple                  # the plan's time EXPRESSIONS (see time_expr_of), not resolved instants


@dataclass(frozen=True)
class Assessment:
    cacheable: bool
    reason: str = ""
    facts: PlanFacts | None = None
    semantic_ok: bool = False
    semantic_reason: str = ""


def family_of(reference_path: str) -> str:
    """The exporter directory a reference lives in (the same rule skill_index.metric_directory uses)."""
    parts = (reference_path or "").split("/")
    return parts[-2] if len(parts) >= 2 else ""


def plan_metrics(contract: Mapping) -> tuple[str, ...]:
    m = contract.get("measurement_used")
    if not isinstance(m, Mapping):
        return ()
    names = [m.get("name"), *(m.get("source_metrics") or [])]
    return tuple(sorted({n for n in names if isinstance(n, str) and n}))


def plan_shell(executed: dict) -> dict:
    """The validated contract entry without anything produced by executing it."""
    return {k: v for k, v in executed.items() if k != "execution"}


def time_expr_of(contract: Mapping) -> tuple:
    """The time expressions a plan executes with, exactly as stored: ("range", from, to, step) or
    ("instant", time). Expressions, not resolved instants: a relative window ("now-1h") resolves
    differently on every execution, which is the point of caching it verbatim. Used to prove that the
    stored plan's time range is the one that was checked when the plan was inserted."""
    tr = contract.get("time_range")
    if contract.get("query_type", "range") == "instant":
        return ("instant", tr.get("time") if isinstance(tr, Mapping) else contract.get("time"))
    if not isinstance(tr, Mapping):
        return ("range", None, None, None)
    return ("range", tr.get("from"), tr.get("to"), tr.get("step"))


def _is_relative(expr: Any) -> bool:
    if not isinstance(expr, str):
        return False
    try:
        time_utils.resolve_relative_time(expr)
        return True
    except time_utils.TimeParseError:
        return False


def plan_eligibility(executed: Any) -> str:
    """"" if this executed result may seed a plan, else why not. Correctness first, coverage second."""
    if not isinstance(executed, dict):
        return "not a contract entry"
    if executed.get("mode") != "single" or "results" in executed:
        return "only a single Prometheus entry is stored (multi/compound responses are not)"
    if "correlation" in executed:
        return "correlated responses are not stored as a plan"
    if executed.get("status") != "ok":
        return f"status {executed.get('status')!r} is not stored (only plain 'ok' plans; panic-mode, " \
               f"alert and every non-executable status are excluded)"
    if (executed.get("data_source") or "").strip().lower() != "prometheus":
        return "not a Prometheus entry"
    ex = executed.get("execution")
    if not isinstance(ex, dict) or ex.get("execution_status") != "success":
        return "only a successful, non-empty execution seeds a plan (errors and empty results do not)"
    if not isinstance(executed.get("query"), str) or not executed["query"].strip():
        return "the plan has no PromQL query"
    if not isinstance(executed.get("reference_used"), str) or not executed["reference_used"]:
        return "the plan names no reference"
    if not plan_metrics(executed):
        return "the plan names no metric"
    if len(plan_metrics(executed)) > MAX_PLAN_METRICS:
        return "the plan names too many metrics"
    qtype = executed.get("query_type", "range")
    tr = executed.get("time_range")
    if qtype == "instant":
        t = tr.get("time") if isinstance(tr, dict) else executed.get("time")
        if t is not None and not _is_relative(t):
            return "the plan has an absolute instant; only relative times are stored"
    elif qtype == "range":
        if not isinstance(tr, dict) or not (_is_relative(tr.get("from")) and _is_relative(tr.get("to"))):
            return "the plan has no relative time window; only relative windows are stored"
    else:
        return f"query_type {qtype!r} is not stored"
    return ""


def _window_seconds(executed: dict) -> float | None:
    if executed.get("query_type", "range") == "instant":
        return None
    resolved = time_utils.resolve_time_range(executed["time_range"])
    return (resolved["end"] - resolved["start"]).total_seconds()


def _promql_windows(query: str) -> set[float]:
    return {int(n) * _UNIT_SECONDS[u] for n, u in _RANGE_SELECTOR_RE.findall(query)}


def semantic_consistency(question: str, facts: PlanFacts, aliases: Mapping[str, Mapping[str, str]],
                         *, concepts_enabled: bool = True) -> tuple[bool, str]:
    """May this entry be matched by DIFFERENTLY WORDED questions? Only if its own question agrees
    with the plan that really ran, in everything that can be checked mechanically."""
    sig = concepts.signature(question, _qc.alias_items_for(aliases, [facts.family]), enabled=concepts_enabled)
    if not sig.understood:
        return False, "question not fully understood: " + ", ".join(sig.reasons)
    unbacked = concepts.unsupported_concept(sig, facts.metrics)
    if unbacked:
        return False, unbacked
    if concepts_enabled:
        # A domain acronym ("CPU") is a concept WORD under the vocabulary, not a case-sensitive literal, so
        # the literal check below no longer sees it. When no concept phrase consumed it (it is still a
        # leftover word: "CPU temperature") it must still appear in the executed query, exactly as it had to
        # before; when a concept consumed it, the metric check above is what covers it.
        for acronym in sorted(concepts.WORD_ACRONYMS):
            if acronym.lower() in sig.residual and acronym.lower() not in facts.query.lower():
                return False, f"literal {acronym!r} is not in the executed query"
    if sig.window_seconds is not None:
        if facts.window_seconds is not None:
            if abs(facts.window_seconds - sig.window_seconds) > 1:
                return False, "the question's time window is not the executed window"
        elif sig.window_seconds not in _promql_windows(facts.query):
            return False, "the question's time window is not a range selector of the executed query"
    for lit in sig.literals:
        needle = lit[2:] if lit.startswith("q:") else lit
        # an alphabetic acronym ("CPU", "GPU") is a concept word, not data: metric names are lower case
        haystack, probe = (facts.query.lower(), needle.lower()) if needle.isalpha() else (facts.query, needle)
        if probe not in haystack:
            return False, f"literal {needle!r} is not in the executed query"
    return True, ""


def assess_executed(question: str, executed: Any, *, aliases: Mapping[str, Mapping[str, str]] | None = None,
                    concepts_enabled: bool = True) -> Assessment:
    """May this executed result be stored, for exact wording and (separately) for reworded questions?"""
    why = plan_eligibility(executed)
    if why:
        return Assessment(False, why)
    try:
        facts = PlanFacts(
            reference_used=executed["reference_used"], family=family_of(executed["reference_used"]),
            metrics=plan_metrics(executed), query=executed["query"],
            query_type=executed.get("query_type", "range"), window_seconds=_window_seconds(executed),
            time_expr=time_expr_of(executed))
    except (time_utils.TimeParseError, KeyError, TypeError, ValueError) as exc:
        return Assessment(False, f"the plan's time window could not be resolved ({type(exc).__name__})")
    ok, sem_why = semantic_consistency(question, facts, aliases or {}, concepts_enabled=concepts_enabled)
    return Assessment(True, "", facts, ok, sem_why)


# =====================================================================================================
# 2. fingerprints (what a plan's validity depends on)
# =====================================================================================================


@dataclass(frozen=True)
class PromFingerprints:
    config: str
    skill: str
    catalog: tuple[tuple[str, str], ...]      # (metric, digest of its full catalog entry)
    routing: str                              # digest of the catalog's candidate view of the question
    labels: tuple[tuple[str, str], ...]       # (metric, digest of its live label KEYS)


def fingerprint_config(settings) -> str:
    """Settings that change whether a stored plan is still valid or how a question is routed.
    Output-shaping limits (max points/series) are NOT here: they apply at execution time on every hit."""
    return _digest({
        "url": settings.prometheus_url,
        "lookback_hours": settings.label_discovery_lookback_hours,
        "narrow_min_score": settings.catalog_narrow_min_score,
        "status_validation": pipeline._status_validation_active(settings),
        "alerts": settings.alert_rule_creation_enabled,
        "dependent": settings.dependent_query_resolution_enabled,
        "model": settings.gemini_model,
    })


def fingerprint_skill(skill_index, facts: PlanFacts) -> str:
    """SKILL.md plus the reference, its overview and prometheus-fundamentals: they decide how a question
    becomes a query."""
    texts: dict[str, str] = {"skill": getattr(skill_index, "raw_text", "") or ""}
    overview = skill_index.overview_path_for(facts.reference_used)
    fundamentals = skill_index.fundamentals_reference_for("prometheus")
    for key, path in (("reference", facts.reference_used), ("overview", overview), ("fundamentals", fundamentals)):
        texts[key] = skill_index.read_reference(path) if path else ""
    return _digest(texts)


def fingerprint_catalog(catalog, metrics: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    """One digest per plan metric over its WHOLE catalog entry (type, help, unit, status, exporter,
    reference, keywords, ...). An unavailable catalog and a metric absent from it are distinct states."""
    by_name = {m.name: m for m in catalog.metrics} if catalog is not None else {}
    return tuple((name, _digest({"catalog": catalog is not None,
                                 "entry": dataclasses.asdict(by_name[name]) if name in by_name else None}))
                 for name in metrics)


def fingerprint_routing(catalog, question: str) -> str:
    """The catalog's CURRENT view of who could own `question`: every (metric, reference, score) the
    pipeline's own uncapped narrowing search returns for it. That list is exactly what the Router (via
    reference candidates) and the Generator (via metric candidates) are shown, so if it is unchanged the
    pipeline would be offered the same choices it was when the plan was generated.

    Why this exists: fingerprint_catalog() only covers the plan's OWN metrics, so a NEW or CHANGED
    competing catalog entry (the catalog is republished in the background and by admin reload) could
    change which reference/metric should own the question while every one of the plan's own entries is
    untouched. Scores are per entry (no corpus-wide weighting), so an unrelated entry does not move this
    digest; only an entry that scores for the question's words does. Over-rejection costs two LLM calls;
    under-rejection would run a plan for a question the catalog now routes elsewhere."""
    if catalog is None:
        return _digest({"catalog": None})
    results = catalog_search.search(catalog, question, top_n=pipeline._NARROW_SEARCH_TOP_N)
    return _digest([(r.entry.name, r.entry.reference_path, round(r.score, 6)) for r in results])


def fingerprint_labels(details: Mapping[str, Any], metrics: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    """Label KEYS only. Values churn constantly (hosts come and go); the validator already enforces them
    for every exact-match selector, so they are re-checked on each hit rather than fingerprinted."""
    out = []
    for name in metrics:
        d = details.get(name)
        out.append((name, _digest(None if d is None else sorted(d.keys))))
    return tuple(out)


# =====================================================================================================
# 3. the validation context, rebuilt from current state
# =====================================================================================================


def known_universe(facts: PlanFacts, skill_index) -> tuple[set[str], set[str]]:
    """(known_metrics, known_references) exactly as pipeline._build_generator_context derives them for a
    request routed to `facts.reference_used`. Cheap: no network."""
    known_references = {facts.reference_used}
    known_metrics: set[str] = set()
    skill_index.read_reference(facts.reference_used)          # raises if the reference is gone
    overview = skill_index.overview_path_for(facts.reference_used)
    if overview:
        known_references.add(overview)
        known_metrics.update(skill_index.metric_directory(overview).keys())
    return known_metrics, known_references


def label_context(facts: PlanFacts, settings) -> dict[str, Any]:
    """Live label discovery for just the plan's metrics (TTL-cached by label_discovery)."""
    label_discovery.configure(settings.label_discovery_cache_ttl_seconds, settings.label_discovery_max_workers)
    return label_discovery.discover_label_details_for_metrics(
        settings.prometheus_url, list(facts.metrics),
        lookback=timedelta(hours=settings.label_discovery_lookback_hours))


def validator_kwargs(facts: PlanFacts, *, settings, catalog, known_metrics: set[str], known_references: set[str],
                     details: Mapping[str, Any]) -> dict:
    """What validator.validate_contract is given. Same fields the pipeline passes, with no candidate
    scope: a stored plan is judged against the whole Router-matched reference universe (Level-2
    equivalent), never a narrower scope than the pipeline itself could have accepted."""
    status = None
    if pipeline._status_validation_active(settings):
        if catalog is None:
            # fail closed: without the catalog the status check cannot run, and "no status map" would
            # make the validator skip it silently -- exactly the bypass this cache must never allow
            raise CatalogUnavailable("catalog status validation is active but no catalog is available")
        status = pipeline._catalog_status_by_metric(catalog)
    return dict(
        known_metrics=known_metrics,
        labels_by_metric={m: (d.keys if d is not None else None) for m, d in details.items()},
        label_values_by_metric={m: (d.values if d is not None else None) for m, d in details.items()},
        known_references=known_references, known_datasources={"prometheus"},
        catalog_status_by_metric=status, candidate_metric_scope=None, opensearch_context=None)


def compute_fingerprints(facts: PlanFacts, contract: dict, *, settings, skill_index, catalog, question: str
                         ) -> tuple[PromFingerprints | None, str]:
    """(fingerprints, "") or (None, reason). Fails when the plan cannot be PROVEN valid right now:
    its metrics are not in the metric directory, label discovery is unavailable for any of them, or the
    validator rejects it under the rebuilt context. Such a plan is neither stored nor reused."""
    if catalog is None:
        return None, "catalog unavailable: routing and status checks cannot be proven"
    known_metrics, known_references = known_universe(facts, skill_index)
    if not known_metrics:
        return None, "no metric directory to validate the plan against"
    unknown = [m for m in facts.metrics if m not in known_metrics]
    if unknown:
        return None, f"metric(s) {unknown} are not in the metric directory"
    details = label_context(facts, settings)
    unconfirmed = [m for m in facts.metrics if details.get(m) is None]
    if unconfirmed:
        return None, f"label discovery is unavailable for {unconfirmed}"
    result = validator.validate_contract(contract, **validator_kwargs(
        facts, settings=settings, catalog=catalog, known_metrics=known_metrics,
        known_references=known_references, details=details))
    if not result.passed:
        return None, f"the plan does not validate under the revalidation context: {result.reason}"
    return PromFingerprints(
        config=fingerprint_config(settings), skill=fingerprint_skill(skill_index, facts),
        catalog=fingerprint_catalog(catalog, facts.metrics), routing=fingerprint_routing(catalog, question),
        labels=fingerprint_labels(details, facts.metrics)), ""


# =====================================================================================================
# 4. the cache
# =====================================================================================================


@dataclass(frozen=True)
class PromPlanEntry:
    """Attribute names are the contract with QueryPlanCache's lookup (entry_id, scope, schema_version,
    question, canonical_text, semantic_ok, pattern_id, contract_json, created_at); `pattern_id` is the
    exporter family, which is what aliases are keyed by."""
    entry_id: int
    scope: str
    schema_version: int
    question: str
    canonical_text: str
    semantic_ok: bool
    pattern_id: str
    analysis_type: str | None
    facts: PlanFacts
    contract_json: str
    fingerprints: PromFingerprints
    created_wall: float
    created_at: float


@dataclass(frozen=True)
class StoreOutcome:
    stored: bool
    reason: str = ""
    entry_id: int | None = None
    evicted: bool = False


def check_consistency(entry: PromPlanEntry) -> str | None:
    """None if the contract that WILL be executed is the very plan whose facts were fingerprinted and
    validated: an `ok` Prometheus entry with no execution block, the same query, reference and metrics."""
    try:
        contract = json.loads(entry.contract_json)
        f = entry.facts
        if not isinstance(contract, dict):
            return "stored plan is malformed"
        if contract.get("status") != "ok" or (contract.get("data_source") or "").strip().lower() != "prometheus" \
                or "execution" in contract:
            return "stored plan is not a validated Prometheus entry"
        if contract.get("query") != f.query or contract.get("reference_used") != f.reference_used \
                or plan_metrics(contract) != f.metrics or contract.get("query_type", "range") != f.query_type:
            return "stored plan differs from the plan that was checked"
        if time_expr_of(contract) != f.time_expr:
            return "stored plan's time range differs from the plan that was checked"
        if entry.pattern_id != f.family:
            return "stored plan names a different family than the one that was checked"
        return None
    except (ValueError, TypeError, AttributeError):
        return "stored plan is malformed"


class PrometheusPlanCache(_os_plan.QueryPlanCache):
    """The OpenSearch plan cache's LRU / lookup machinery with Prometheus entries, eligibility and
    consistency check. Its own instance and statistics: it can never see an OpenSearch entry."""

    def __init__(self, *, concepts_enabled: bool = True, **kwargs):
        super().__init__(**kwargs)
        self.concepts_enabled = bool(concepts_enabled)

    def snapshot(self) -> dict:
        return {**super().snapshot(), "datasource": "prometheus", "concepts_enabled": self.concepts_enabled}

    def _signatures_equal(self, question: str, entry) -> bool:
        """Same six-facet equality as every other plan cache; only the VOCABULARY differs: the built-in
        Prometheus concepts (prometheus_concepts) on top of the family's declared aliases."""
        al = _qc.alias_items_for(self.aliases, [entry.pattern_id])
        return _qc.equivalent(concepts.signature(question, al, enabled=self.concepts_enabled),
                              concepts.signature(entry.question, al, enabled=self.concepts_enabled))

    def count(self, key: str) -> None:
        self._bump(key)

    def store(self, question: str, scope: str, assessment: Assessment, contract: dict,
              fingerprints: PromFingerprints | None) -> StoreOutcome:
        """Never raises. A live duplicate (same scope + canonical wording) is kept, so concurrent
        identical misses cannot churn the cache."""
        try:
            if not assessment.cacheable or assessment.facts is None:
                self._bump("not_stored")
                return StoreOutcome(False, assessment.reason or "not cacheable")
            if fingerprints is None or not isinstance(scope, str) or not scope:
                self._bump("not_stored")
                return StoreOutcome(False, "no fingerprints or scope")
            try:
                contract_json = json.dumps(contract, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            except (TypeError, ValueError):
                self._bump("not_stored")
                return StoreOutcome(False, "plan is not JSON-serialisable")
            if len(contract_json.encode("utf-8")) > MAX_PLAN_BYTES:
                self._bump("not_stored")
                return StoreOutcome(False, "plan too large to cache")
            canonical_text = _qc.build_signature(question).canonical_text
            evicted = False
            with self._lock:
                if (scope, canonical_text) in self._by_key:
                    self._bump("duplicate_not_stored")
                    return StoreOutcome(False, "a plan for this question already exists")
                while len(self._entries) >= self.max_entries:
                    self._evict_locked(next(iter(self._entries)))
                    self._bump("evicted_capacity")
                    evicted = True
                eid = self._next_id
                self._next_id += 1
                self._entries[eid] = PromPlanEntry(
                    entry_id=eid, scope=scope, schema_version=PLAN_SCHEMA_VERSION, question=question,
                    canonical_text=canonical_text, semantic_ok=assessment.semantic_ok,
                    pattern_id=assessment.facts.family, analysis_type=None, facts=assessment.facts,
                    contract_json=contract_json, fingerprints=fingerprints, created_wall=self._wall(),
                    created_at=self._clock())
                self._by_key[(scope, canonical_text)] = eid
                self._bump("stored")
            return StoreOutcome(True, "", eid, evicted)
        except Exception:  # noqa: BLE001 -- caching must never fail a request
            logger.exception("prometheus plan cache store failed")
            self._bump("errors")
            return StoreOutcome(False, "internal error")

    def _accept(self, entry, revalidate, reasons: list[str]) -> bool:
        if entry.schema_version != PLAN_SCHEMA_VERSION:
            problem: str | None = f"unsupported plan schema version {entry.schema_version}"
        else:
            try:
                problem = check_consistency(entry) or revalidate(entry)       # revalidate: outside the lock
            except Exception as exc:  # noqa: BLE001
                problem = f"revalidation error: {type(exc).__name__}"
        if problem:
            logger.info("cached prometheus plan %s not reused: %s", entry.entry_id, problem)
            self.invalidate(entry.entry_id)
            self._bump("revalidation_failed")
            reasons.append(problem)
            return False
        return True


# =====================================================================================================
# 5. proof that a stored plan is still valid
# =====================================================================================================


def _changed(stored: tuple[tuple[str, str], ...], current: tuple[tuple[str, str], ...]) -> list[str]:
    now = dict(current)
    return [name for name, digest in stored if now.get(name) != digest]


def revalidate_entry(entry: PromPlanEntry, *, settings, skill_index, catalog) -> str | None:
    """None if the stored plan may still run, else why not. No LLM and no query: fingerprints are
    compared (cheapest first), live labels are re-read from label discovery and the stored plan is run
    through the real validator again."""
    stored, facts = entry.fingerprints, entry.facts
    if fingerprint_config(settings) != stored.config:
        return "configuration changed"
    if catalog is None:
        return "catalog unavailable: routing and status checks cannot be proven"
    try:
        skill_now = fingerprint_skill(skill_index, facts)
    except Exception as exc:  # noqa: BLE001 -- a vanished reference is a reason, not a crash
        return f"skill text is unavailable ({type(exc).__name__})"
    if skill_now != stored.skill:
        return "skill text changed"
    changed = _changed(stored.catalog, fingerprint_catalog(catalog, facts.metrics))
    if changed:
        return f"catalog entry for {changed} changed"
    if fingerprint_routing(catalog, entry.question) != stored.routing:
        return "catalog routing for the question changed (a competing or changed catalog entry)"
    known_metrics, known_references = known_universe(facts, skill_index)
    if not known_metrics:
        return "no metric directory to validate the plan against"
    details = label_context(facts, settings)
    unconfirmed = [m for m in facts.metrics if details.get(m) is None]
    if unconfirmed:
        return f"label discovery is unavailable for {unconfirmed}"
    changed = _changed(stored.labels, fingerprint_labels(details, facts.metrics))
    if changed:
        return f"label keys of {changed} changed"
    result = validator.validate_contract(json.loads(entry.contract_json), **validator_kwargs(
        facts, settings=settings, catalog=catalog, known_metrics=known_metrics,
        known_references=known_references, details=details))
    if not result.passed:
        return f"the validator now rejects the stored plan: {result.reason}"
    return None


def _top_references(catalog, question: str, min_score: float
                    ) -> tuple[bool, set[str], dict[str, set[str]]]:
    """(confident, references tied for the top score, metric names the catalog offers for the question
    GROUPED BY THE REFERENCE THAT OWNS THEM). Grouping matters: a metric offered by some other reference
    says nothing about whether the plan's own reference still offers it."""
    results = catalog_search.search(catalog, question, top_n=pipeline._NARROW_SEARCH_TOP_N)
    if not results or results[0].score < min_score:
        return False, set(), {}
    top = results[0].score
    refs = {r.entry.reference_path for r in results if r.score == top and r.entry.reference_path}
    by_ref: dict[str, set[str]] = {}
    for r in results:
        if r.entry.reference_path:
            by_ref.setdefault(r.entry.reference_path, set()).add(r.entry.name)
    return True, refs, by_ref


def reference_ambiguity(question: str, entry: PromPlanEntry, *, settings, catalog) -> str | None:
    """None only if the catalog confirms that the plan's reference is the one (and only one) that owns a
    REWORDED question; else why not. Level-2 equivalence compares wording only and picking the reference
    is the Router's (LLM) job, which must not be repeated at lookup time -- so ownership is judged
    deterministically and one-sidedly: any doubt is a miss, never a reuse. This is the cross-pattern
    safeguard (a reworded question must not borrow a plan built for another exporter/domain/metric)."""
    if catalog is None:
        return "no catalog is available to confirm which reference owns the reworded question"
    confident, refs, by_ref = _top_references(catalog, question, settings.catalog_narrow_min_score)
    if not confident:
        return "the catalog is not confident which reference owns the reworded question"
    if refs != {entry.facts.reference_used}:
        return "the catalog does not put the plan's reference alone on top for the reworded question"
    offered = by_ref.get(entry.facts.reference_used, set())
    missing = [m for m in entry.facts.metrics if m not in offered]
    if missing:
        return f"the plan's own reference no longer offers {missing} for the reworded question"
    return None


def concept_ownership(question: str, entry: PromPlanEntry, *, cache: "PrometheusPlanCache", settings, catalog
                      ) -> str | None:
    """The ownership check for a semantic candidate, with the built-in concepts taken into account.
    `reference_ambiguity` itself is unchanged (reference alone on top, confident, every plan metric still
    offered); it is asked about the question plus the canonical wording of its recognised concepts, and the
    plan's metrics must back every one of those concepts. Any doubt is a miss."""
    al = _qc.alias_items_for(cache.aliases, [entry.pattern_id])
    if cache.concepts_enabled:
        unbacked = concepts.unsupported_concept(concepts.signature(question, al), entry.facts.metrics)
        if unbacked:
            return unbacked
    return reference_ambiguity(concepts.ownership_text(question, al, enabled=cache.concepts_enabled), entry,
                               settings=settings, catalog=catalog)


# =====================================================================================================
# 6. process-wide wiring (settings -> the one cache) and the agent-facing operations
# =====================================================================================================

_lock = threading.Lock()
_caches: dict[tuple, PrometheusPlanCache] = {}


def enabled(settings) -> bool:
    return bool(settings.prometheus_query_plan_cache_enabled)


def get_cache(settings) -> PrometheusPlanCache | None:
    """The one process-wide cache for these settings, or None when disabled. Reads only its own
    PROMETHEUS_QUERY_PLAN_CACHE_* settings: nothing is shared with the OpenSearch caches."""
    if not enabled(settings):
        return None
    key = (settings.prometheus_query_plan_cache_max_entries, settings.prometheus_query_plan_cache_semantic_enabled,
           json.dumps(settings.prometheus_query_plan_cache_aliases, sort_keys=True),
           settings.prometheus_query_plan_cache_concepts_enabled)
    with _lock:
        cache = _caches.get(key)
        if cache is None:
            cache = PrometheusPlanCache(
                max_entries=settings.prometheus_query_plan_cache_max_entries,
                semantic_enabled=settings.prometheus_query_plan_cache_semantic_enabled,
                aliases=settings.prometheus_query_plan_cache_aliases,
                concepts_enabled=settings.prometheus_query_plan_cache_concepts_enabled)
            _caches[key] = cache
        return cache


def reset() -> None:
    """Empties every Prometheus plan cache and forgets the instances. Used by the admin reload and tests."""
    with _lock:
        caches = list(_caches.values())
        _caches.clear()
    for cache in caches:
        cache.clear()


def snapshot(settings) -> dict:
    cache = get_cache(settings)
    return {"enabled": False} if cache is None else cache.snapshot()


class _LazyCatalog:
    """The published catalog snapshot, read at most once per operation and only if something needs it."""

    def __init__(self, settings):
        self._settings, self._loaded, self._value = settings, False, None

    def get(self):
        if not self._loaded:
            self._value, self._loaded = pipeline._get_catalog(self._settings), True
        return self._value


def lookup(question: str, scope: str, settings, skill_index) -> PlanLookup:
    """A re-validated plan for an identical or equivalent question (hit), or why a matching plan was
    dropped. Never raises. A hit has passed the schema check and every revalidation step; the caller
    must still EXECUTE it (Prometheus is queried again -- nothing here is data)."""
    cache = get_cache(settings)
    if cache is None:
        return PlanLookup()
    catalog = _LazyCatalog(settings)

    def revalidate(entry):
        return revalidate_entry(entry, settings=settings, skill_index=skill_index, catalog=catalog.get())

    def pattern_check(entry, q):
        return concept_ownership(q, entry, cache=cache, settings=settings, catalog=catalog.get())

    try:
        return cache.lookup(question, scope, revalidate, pattern_check)
    except Exception:  # noqa: BLE001 -- caching must never fail a request
        logger.exception("prometheus plan cache lookup failed")
        cache.count("errors")
        return PlanLookup()


def invalidate(settings, entry_id: int) -> None:
    try:
        cache = get_cache(settings)
        if cache is not None:
            cache.invalidate(entry_id)
    except Exception:  # noqa: BLE001 -- never fail a request because of the cache
        logger.exception("invalidating the cached prometheus plan failed")


def _prometheus_entries(executed: Any) -> list[dict]:
    if not isinstance(executed, dict):
        return []
    entries = executed.get("results") if executed.get("mode") == "multi" else [executed]
    return [e for e in (entries or []) if isinstance(e, dict)
            and (e.get("data_source") or "").strip().lower() == "prometheus" and e.get("status") in
            ("ok", "panic_mode_best_effort")]


@dataclass(frozen=True)
class CatalogBaseline:
    """The catalog snapshot in force when the pipeline STARTED for a question (an immutable snapshot
    object; the runtime republishes by replacing it, never by mutating it). The agent hands this very
    object to the pipeline, so the Router, the Generator candidates and the status validation all work
    from it, and the plan is later fingerprinted against it -- never against whatever is current when the
    answer is stored. `catalog` is None when no catalog was available (then nothing may be stored)."""
    catalog: Any


def capture_baseline(settings) -> CatalogBaseline | None:
    """Read the published catalog snapshot once, before the pipeline runs. None = cache disabled."""
    if not enabled(settings):
        return None
    try:
        return CatalogBaseline(pipeline._get_catalog(settings))
    except Exception:  # noqa: BLE001 -- never fail a request because of the cache
        logger.exception("capturing the catalog baseline failed")
        return CatalogBaseline(None)


def catalog_changed_since(baseline_catalog, current_catalog, question: str, metrics: tuple[str, ...]) -> str | None:
    """None if the catalog the plan was generated under and the catalog now in force agree on everything
    a plan's validity depends on that the catalog decides: the routing view of the question and the full
    entry (status, type, help, unit, reference, ...) of every metric the plan uses. Compared by CONTENT,
    not identity: a refresh that republishes identical data must not make plans uncacheable."""
    if baseline_catalog is None:
        return "catalog unavailable when the pipeline started: routing and status checks cannot be proven"
    if current_catalog is None:
        return "catalog unavailable at insertion: routing and status checks cannot be proven"
    if baseline_catalog is current_catalog:
        return None
    if fingerprint_routing(baseline_catalog, question) != fingerprint_routing(current_catalog, question):
        return "catalog routing for the question changed between generation and insertion"
    if fingerprint_catalog(baseline_catalog, metrics) != fingerprint_catalog(current_catalog, metrics):
        return "catalog entry of a plan metric changed between generation and insertion"
    return None


def store_validated_plan(question: str, scope: str, executed: Any, settings, skill_index,
                         baseline: CatalogBaseline | None = None) -> StoreOutcome:
    """Stores the plan behind `executed` if (and only if) it is eligible. Never raises. Responses that
    contain no executed Prometheus entry are not this cache's business and are not counted.

    `baseline` is the catalog snapshot the pipeline ran under (see CatalogBaseline). The plan is stored
    only if the catalog now in force still agrees with it; otherwise the answer is returned normally and
    simply not cached (the next identical question generates and stores a plan under the new catalog).
    No baseline = nothing proves which catalog generated the plan, so nothing is stored (fail closed)."""
    try:
        cache = get_cache(settings)
        if cache is None:
            return StoreOutcome(False, "plan cache disabled")
        if not _prometheus_entries(executed):
            return StoreOutcome(False, "not a Prometheus execution")
        assessment = assess_executed(question, executed, aliases=cache.aliases,
                                     concepts_enabled=cache.concepts_enabled)
        if not assessment.cacheable:
            return cache.store(question, scope, assessment, {}, None)           # counted, not stored
        if baseline is None:
            cache.count("not_stored")
            return StoreOutcome(False, "no catalog baseline: which catalog generated the plan is unknown")
        changed = catalog_changed_since(baseline.catalog, pipeline._get_catalog(settings), question,
                                        assessment.facts.metrics)
        if changed:
            cache.count("not_stored")
            cache.count("catalog_changed_not_stored")
            return StoreOutcome(False, changed)
        contract = plan_shell(executed)
        prints, why = compute_fingerprints(assessment.facts, contract, settings=settings, skill_index=skill_index,
                                           catalog=baseline.catalog, question=question)
        if prints is None:
            cache.count("not_stored")
            return StoreOutcome(False, why)
        return cache.store(question, scope, assessment, contract, prints)
    except Exception:  # noqa: BLE001 -- never fail a request because of the cache
        logger.exception("storing the Prometheus plan failed")
        return StoreOutcome(False, "internal error")


# ---- response metadata ----------------------------------------------------------------------------------


def annotate(result: Any, info: dict) -> None:
    """Adds `execution.metadata.query_plan_cache` (the same key the OpenSearch plan cache uses; `cache`
    says which one). Only for a single executed entry; never touches anything else."""
    _os_plan.annotate(result, {"cache": "prometheus", **info})


def hit_info(hit: PlanHit) -> dict:
    return {"hit": True, "match": hit.match, "revalidated": True, "age_seconds": hit.age_seconds,
            "cache_entry_id": hit.entry_id}


def miss_info(outcome: StoreOutcome | None, invalidation_reason: str | None) -> dict:
    info: dict[str, Any] = {"hit": False}
    if invalidation_reason:
        info["invalidation_reason"] = invalidation_reason
    if outcome is not None:
        info["stored"] = outcome.stored
        if outcome.stored:
            info["cache_entry_id"] = outcome.entry_id
            info["evicted"] = outcome.evicted
        else:
            info["not_stored_reason"] = outcome.reason
    return info


def record_miss(question: str, scope: str, executed: Any, settings, skill_index,
                invalidation_reason: str | None, baseline: CatalogBaseline | None = None) -> None:
    """After a normal (cache-miss) execution: remember the plan if eligible and say what happened in
    the response. A no-op when the cache is disabled, so responses are then exactly as before. Never raises."""
    try:
        if not enabled(settings):
            return
        outcome = store_validated_plan(question, scope, executed, settings, skill_index, baseline)
        if isinstance(executed, dict) and executed.get("mode") != "multi" and _prometheus_entries(executed):
            annotate(executed, miss_info(outcome, invalidation_reason))
    except Exception:  # noqa: BLE001 -- never fail a request because of the cache
        logger.exception("recording the Prometheus plan cache outcome failed")
