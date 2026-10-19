"""
app/catalog/runtime.py

Runtime glue between the structured Prometheus metric catalog
(`settings.catalog_path`, default app/catalog/catalog.json) and the request
pipeline (app/pipeline.py).

This module is deliberately separate from two other things that have
"catalog" in their name and must not be confused with it:

  - app/catalog.json            the dashboard UI catalog served by
                                app/api/routes_query.py (/api/catalog)
  - app/grafana_tools/concept_catalog.py
                                the dashboard concept -> metric mapping

Neither is read, written or cached here, and their loading code is
unchanged.

What the structured catalog is for here (and what it is not):

  * It improves ROUTING and METRIC CANDIDATE RETRIEVAL: given the user's
    question, a deterministic ranked search (app/catalog/search.py)
    (1) narrows which SKILL.md Section 4 routing rows the Router sees
    (`maybe_narrow_section4`) and (2) suggests which metrics the Generator
    should choose from (`build_level1_outcome`). The pipeline backs the
    Generator restriction with a Level 1 -> Level 2 -> Level 3 fallback
    cascade (app/pipeline.py) so a catalog ranking miss is recoverable.
  * It is NOT a source of metric semantics. The matched Markdown references
    stay authoritative for what a metric means and how to query it.
  * It is NOT a source of label keys. Live Prometheus label discovery
    (app/label_discovery.py) stays authoritative for which labels exist.
    Catalog `dimensions` are never treated as proof a label exists.
  * It never widens scope. Candidates are always intersected with the
    metrics of the reference(s) the Router actually matched for the current
    request/stage, so a catalog hit can never smuggle in a metric from an
    unrelated reference or data source.
  * A catalog miss, a low-confidence hit, or a missing/malformed
    catalog.json all degrade to the exact pre-catalog behavior. "Not in the
    catalog" never means "unsupported".

Lifecycle: the catalog is loaded lazily on first use and cached for the
process (`get_structured_catalog`). A load failure is cached too, so a
broken or absent file is logged once and never re-read on every request.
`reset_structured_catalog_cache` / `reload_structured_catalog` are the
explicit reload hooks (the admin reload endpoint calls the latter).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from app.catalog import rules as catalog_rules
from app.catalog import search as catalog_search
from app.catalog.generator import build_markdown_category_index
from app.catalog.loader import CatalogLoadError, load_catalog
from app.catalog.schema import Catalog, CatalogEntry, CatalogSchemaError
from app.skill_index import ROUTING_ROW_RE

if TYPE_CHECKING:  # pragma: no cover - typing only; avoids importing config at module load
    from app.config import Settings

logger = logging.getLogger(__name__)

# Narrowing always searches uncapped (bounded only by catalog size, never by
# rank) and relies on a confidence floor on the TOP score instead of a rank
# cutoff to decide whether the result is trustworthy. A small rank cap would
# silently drop the right metric whenever many metrics share a topic.
NARROW_SEARCH_TOP_N = 1_000_000


# ---- cached, failure-tolerant loading -------------------------------------------

_cache_lock = threading.Lock()
# Keyed by (catalog_path, skills_root). A value of None records a failed load
# so the failure is logged once and not retried on every request.
_cache: dict[tuple[str, str], Catalog | None] = {}


def _cache_key(settings: "Settings") -> tuple[str, str]:
    return (str(settings.catalog_path), str(settings.skills_root))


def _load_from_disk(settings: "Settings") -> Catalog:
    """Reads, validates and enriches the catalog. Raises on any problem.

    The on-disk catalog.json is pure generator output; keywords/priority
    (app/catalog/rules.py) and Markdown-category keyword enrichment are
    applied in memory at load time, as app/catalog/generator.py intends.
    """
    loaded = load_catalog(Path(settings.catalog_path))
    category_index = build_markdown_category_index(Path(settings.skills_root))
    return catalog_rules.apply_rules(loaded, markdown_category_index=category_index)


def get_structured_catalog(settings: "Settings") -> Catalog | None:
    """Returns the cached structured catalog, loading it on first use.

    Returns None (never raises) if the file is missing, unreadable, not valid
    JSON, or fails schema validation. Every catalog-dependent pipeline
    behavior treats None as "no catalog": no candidate block in the Generator
    prompt and no status validation, i.e. exactly the pre-catalog behavior.
    A broken catalog can therefore never crash a request or unrelated
    dashboard/chat features.
    """
    key = _cache_key(settings)
    with _cache_lock:
        if key in _cache:
            return _cache[key]
        try:
            catalog = _load_from_disk(settings)
        except (CatalogLoadError, CatalogSchemaError, OSError) as e:
            logger.warning(
                "Structured metric catalog disabled: failed to load %s: %s. "
                "Catalog candidates and status validation are off; the pipeline "
                "continues with its normal reference-based behavior.",
                settings.catalog_path, e,
            )
            _cache[key] = None
            return None
        except Exception:  # noqa: BLE001 - catalog is additive; never take a request down
            logger.exception(
                "Unexpected error loading structured metric catalog %s; treating it as unavailable.",
                settings.catalog_path,
            )
            _cache[key] = None
            return None
        _cache[key] = catalog
        return catalog


def reset_structured_catalog_cache() -> None:
    """Drops every cached catalog (and cached load failure). The next
    `get_structured_catalog` call re-reads the file from disk."""
    with _cache_lock:
        _cache.clear()


def reload_structured_catalog(settings: "Settings") -> Catalog | None:
    """Explicit reload from disk. Returns the catalog now in effect.

    Same fail-safe convention as the skill reload: if the new file cannot be
    loaded, a previously loaded good catalog keeps serving (a bad edit never
    switches the catalog off); with no previous good catalog, the failure is
    cached and the catalog stays unavailable (None)."""
    key = _cache_key(settings)
    with _cache_lock:
        try:
            catalog = _load_from_disk(settings)
        except Exception as e:  # noqa: BLE001 - reload must never raise into an admin request
            previous = _cache.get(key)
            if previous is not None:
                logger.warning(
                    "Structured metric catalog reload failed (%s); keeping the previously loaded "
                    "catalog (%d metrics).", e, len(previous.metrics),
                )
                return previous
            logger.warning("Structured metric catalog reload failed (%s); catalog remains unavailable.", e)
            _cache[key] = None
            return None
        _cache[key] = catalog
        return catalog


# ---- retrieval decisions (pure functions) ---------------------------------------


@dataclass(frozen=True)
class NarrowingDecision:
    """Reference-level narrowing result: which reference_paths the catalog
    suggests. Pure data, independent of SkillIndex/Section 4 text, so
    scripts/evaluate_catalog_retrieval.py and tests can exercise the real
    decision directly; the Router narrowing in `maybe_narrow_section4` acts on
    exactly this decision."""
    confident: bool
    suggested_paths: frozenset[str]
    top_score: float | None


def catalog_narrowing_candidates(catalog: Catalog, question: str,
                                 min_confident_score: float) -> NarrowingDecision:
    """Uncapped-by-rank catalog search gated by a confidence floor on the TOP
    candidate's score. `confident=False` covers both a true catalog miss
    (nothing scored above zero) and a low-confidence hit (best score below
    `min_confident_score`); callers treat both identically as "no opinion".

    Uses search.py's default status filter, so an unreviewed
    (discovered_pending_review) or rejected metric never influences
    retrieval."""
    results = catalog_search.search(catalog, question, top_n=NARROW_SEARCH_TOP_N)
    if not results:
        return NarrowingDecision(confident=False, suggested_paths=frozenset(), top_score=None)

    top_score = results[0].score
    if top_score < min_confident_score:
        return NarrowingDecision(confident=False, suggested_paths=frozenset(), top_score=top_score)

    suggested = frozenset(r.entry.reference_path for r in results if r.entry.reference_path)
    return NarrowingDecision(confident=True, suggested_paths=suggested, top_score=top_score)


@dataclass(frozen=True)
class MetricCandidateDecision:
    """Metric-level counterpart of NarrowingDecision: carries the ranked
    CatalogEntry objects themselves."""
    confident: bool
    candidates: tuple[CatalogEntry, ...] = ()
    top_score: float | None = None


def catalog_metric_candidates(catalog: Catalog, question: str,
                              min_confident_score: float) -> MetricCandidateDecision:
    """Ranked metric candidates for `question`, or `confident=False` (empty
    candidates) on a miss or a top score below `min_confident_score`."""
    results = catalog_search.search(catalog, question, top_n=NARROW_SEARCH_TOP_N)
    if not results:
        return MetricCandidateDecision(confident=False, candidates=(), top_score=None)

    top_score = results[0].score
    if top_score < min_confident_score:
        return MetricCandidateDecision(confident=False, candidates=(), top_score=top_score)

    return MetricCandidateDecision(confident=True, candidates=tuple(r.entry for r in results),
                                   top_score=top_score)


def metric_metadata_dict(entry: CatalogEntry) -> dict[str, str | None]:
    """Projects a CatalogEntry down to the four identity fields the Generator
    may see. Built field by field so keywords/priority/status/exporter/
    reference_path/dimensions can never reach the prompt by accident."""
    return {"name": entry.name, "type": entry.type, "help": entry.help, "unit": entry.unit}


@dataclass(frozen=True)
class Level1Outcome:
    """Result of the Level-1 (catalog-narrowed Generator candidates) lookup.

    An empty `metric_metadata` has two importantly different causes:

      * a catalog miss / low-confidence hit / non-Prometheus request: Level 1
        simply had no information. `reference_universe_mismatch=False`; the
        request behaves exactly as if no catalog existed.
      * a CONFIDENT catalog hit whose candidates share ZERO overlap with the
        metrics of the references the Router matched: the catalog has an
        opinion and it contradicts the routed reference universe.
        `reference_universe_mismatch=True`. Broadening candidates inside that
        same universe (Level 2) can never recover this, so the pipeline goes
        straight to Level 3 (a fresh, catalog-unnarrowed routing pass).
    """
    metric_metadata: tuple[dict[str, str | None], ...] = ()
    reference_universe_mismatch: bool = False


def build_level1_outcome(catalog: Catalog, question: str, known_prometheus_metrics: set[str],
                         min_confident_score: float) -> Level1Outcome:
    """Ranked candidate metadata for ONE request/stage.

    Candidates are intersected with `known_prometheus_metrics` -- the metrics
    of the reference(s) this specific request or dependency stage actually
    opened. That intersection is the scope boundary: a catalog hit for a
    metric outside those references is never shown to the Generator.
    """
    if not known_prometheus_metrics:
        return Level1Outcome()
    decision = catalog_metric_candidates(catalog, question, min_confident_score)
    if not decision.confident:
        logger.info(
            "catalog_candidates: no confident candidates for question %r (top_score=%s, min=%s); "
            "Generator prompt gets no candidate block.",
            question, decision.top_score, min_confident_score,
        )
        return Level1Outcome()
    in_scope = tuple(e for e in decision.candidates if e.name in known_prometheus_metrics)
    if not in_scope:
        logger.info(
            "catalog_candidates: confident candidates for question %r were entirely outside this "
            "request's matched references (top_score=%s) -- reference-universe mismatch.",
            question, decision.top_score,
        )
        return Level1Outcome(reference_universe_mismatch=True)
    return Level1Outcome(metric_metadata=tuple(metric_metadata_dict(e) for e in in_scope))


def build_metric_metadata(catalog: Catalog, question: str, known_prometheus_metrics: set[str],
                          min_confident_score: float) -> tuple[dict[str, str | None], ...]:
    """Candidate metadata only (see `build_level1_outcome` for the full result)."""
    return build_level1_outcome(catalog, question, known_prometheus_metrics,
                                min_confident_score).metric_metadata


def build_level2_metric_metadata(catalog: Catalog | None, matched: list[dict],
                                 known_prometheus_metrics: set[str]) -> tuple[dict[str, str | None], ...]:
    """Level-2 candidates: every catalog metric belonging to the matched
    reference(s), restricted to `known_prometheus_metrics` as a consistency
    cross-check (a metric absent from the Metric Directory this request
    opened is never offered, even if catalog.json lists it under that
    reference). Returns () if there is no catalog or nothing matches -- i.e.
    behaviourally identical to no catalog for the retry, never an error."""
    if catalog is None:
        return ()
    by_name: dict[str, dict[str, str | None]] = {}
    for m in matched:
        reference_path = m.get("reference_path")
        if not reference_path:
            continue
        for entry in catalog.by_reference_path(reference_path):
            if entry.name in known_prometheus_metrics and entry.name not in by_name:
                by_name[entry.name] = metric_metadata_dict(entry)
    return tuple(by_name.values())


def format_metric_metadata_for_prompt(metric_metadata: tuple[dict[str, str | None], ...]) -> str:
    """Renders candidate metadata as a clearly scoped Generator prompt block.

    Identity/description only, layered ON TOP of the Markdown references
    (never a replacement for them and never authoritative for query
    semantics). Wording follows `main`: the list is the set of metrics
    currently eligible for this attempt, and if none fits the Generator
    answers `unsupported_metric` -- which the pipeline treats as the signal
    to retry once with every metric of the matched reference(s) (Level 2),
    so a narrow candidate list never makes a supported metric unsupported."""
    lines = [
        "Catalog-identified candidate metrics for this question (structured basic "
        "identity metadata only -- name, Prometheus type, HELP text, and unit). "
        "These are the metrics currently eligible for selection for this question. "
        "This is descriptive/identity information ONLY -- it is not a substitute for "
        "the matched reference Markdown above, which remains authoritative for "
        "Use-when guidance, confusable-metric distinctions, query construction, and "
        "all other semantic interpretation. If, after consulting the Markdown above, "
        "none of these candidates is actually the right metric for this question, "
        "respond with status 'unsupported_metric' rather than selecting a metric "
        "outside this list:",
    ]
    for m in metric_metadata:
        unit = m["unit"] if m["unit"] else "n/a"
        lines.append(f"- {m['name']} (type: {m['type']}, unit: {unit}) -- {m['help']}")
    return "\n".join(lines)


def catalog_status_by_metric(catalog: Catalog) -> dict[str, str]:
    """{metric_name: catalog_status}: the minimal interface the validator
    needs, instead of threading the whole Catalog through validation."""
    return {m.name: m.status for m in catalog.metrics}


# ---- Router narrowing of SKILL.md Section 4 -------------------------------------


def catalog_covered_reference_paths(catalog: Catalog) -> set[str]:
    """Every reference_path the catalog has at least one entry for. Section 4
    rows outside this set (overview.md, *-fundamentals.md, execution-contract,
    OpenSearch/log rows, any future domain the catalog does not cover yet) are
    rows the catalog has no opinion about, so they are always kept."""
    return {m.reference_path for m in catalog.metrics if m.reference_path}


def narrow_section4_text(section4_text: str, allowed_reference_paths: set[str]) -> str:
    """Filters SKILL.md's Section 4 table down to the data rows whose linked
    reference_path is in `allowed_reference_paths`. Every non-data-row line
    (headers, intro paragraph, separator row, trailing guidance) is preserved
    byte-for-byte, and kept rows are the ORIGINAL markdown lines (matched with
    skill_index.ROUTING_ROW_RE, the regex SkillIndex itself parses Section 4
    with), so every kept row is identical to what the Router always saw."""
    kept_lines: list[str] = []
    for line in section4_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            kept_lines.append(line)
            continue
        if stripped.startswith("|---") or stripped.startswith("| ---"):
            kept_lines.append(line)
            continue
        if stripped.lower().startswith("| route when"):
            kept_lines.append(line)
            continue
        m = ROUTING_ROW_RE.match(stripped)
        if not m:
            # Not a recognizable routing row: keep rather than risk silently
            # dropping content this function does not understand.
            kept_lines.append(line)
            continue
        if m.group("link_path").strip() in allowed_reference_paths:
            kept_lines.append(line)
        # else: a catalog-covered domain row the catalog did not suggest.
    return "\n".join(kept_lines)


def maybe_narrow_section4(section4_text: str, skill_index, catalog: Catalog, question: str,
                          min_confident_score: float) -> str:
    """Narrows Section 4 to catalog-suggested rows for this question, with
    three independent safety nets, any one of which alone guarantees "never
    narrow away something that might matter":

      1. Only rows whose reference_path the catalog covers can be dropped.
         Every other row is always kept (the catalog has no opinion on them).
      2. A catalog MISS returns `section4_text` unchanged. A miss is never
         converted into "unsupported".
      3. A LOW-CONFIDENCE hit (best score below `min_confident_score`) is
         treated exactly like a miss.

    The Router still decides; the catalog only trims what it is shown.
    """
    decision = catalog_narrowing_candidates(catalog, question, min_confident_score)
    if not decision.confident:
        logger.info(
            "catalog_routing: no confident candidates for question %r (top_score=%s, min=%s); "
            "using the full, unnarrowed Section 4.",
            question, decision.top_score, min_confident_score,
        )
        return section4_text
    uncovered = set(skill_index.reference_paths()) - catalog_covered_reference_paths(catalog)
    allowed = set(decision.suggested_paths) | uncovered
    narrowed = narrow_section4_text(section4_text, allowed)
    logger.info(
        "catalog_routing: narrowed Section 4 for question %r -- catalog-suggested=%s, "
        "always-kept (uncovered)=%s, top_score=%s",
        question, sorted(decision.suggested_paths), sorted(uncovered), decision.top_score,
    )
    return narrowed
