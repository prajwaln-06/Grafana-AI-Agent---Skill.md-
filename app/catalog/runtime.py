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

  * It improves METRIC CANDIDATE RETRIEVAL: given the user's question, a
    deterministic ranked search (app/catalog/search.py) suggests which
    metrics are most relevant, and the pipeline shows that short list to the
    Generator.
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
    decision directly. The fullstack pipeline does not use this to alter the
    Router; it exists for retrieval measurement."""
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


def build_metric_metadata(catalog: Catalog, question: str, known_prometheus_metrics: set[str],
                          min_confident_score: float) -> tuple[dict[str, str | None], ...]:
    """Ranked candidate metadata for ONE request/stage.

    Candidates are intersected with `known_prometheus_metrics` -- the metrics
    of the reference(s) this specific request or dependency stage actually
    opened. That intersection is the scope boundary: a catalog hit for a
    metric outside those references (another exporter, another stage's
    domain) is dropped, never shown. Returns () on a catalog miss, a
    low-confidence hit, or no in-scope overlap; the caller then keeps its
    existing, unnarrowed behavior.
    """
    if not known_prometheus_metrics:
        return ()
    decision = catalog_metric_candidates(catalog, question, min_confident_score)
    if not decision.confident:
        logger.info(
            "Catalog: no confident candidates for question %r (top_score=%s, min=%s); "
            "Generator prompt gets no catalog block.",
            question, decision.top_score, min_confident_score,
        )
        return ()
    in_scope = tuple(e for e in decision.candidates if e.name in known_prometheus_metrics)
    if not in_scope:
        logger.info(
            "Catalog: confident candidates for question %r were all outside this request's "
            "matched references (top_score=%s); ignoring them.", question, decision.top_score,
        )
        return ()
    return tuple(metric_metadata_dict(e) for e in in_scope)


def format_metric_metadata_for_prompt(metric_metadata: tuple[dict[str, str | None], ...]) -> str:
    """Renders candidate metadata as a clearly scoped, advisory Generator
    prompt block. Advisory on purpose: the list is a retrieval aid, so a
    metric that is documented in the opened references but absent from the
    list is still valid -- a catalog ranking miss must not turn a supported
    metric into `unsupported_metric`."""
    lines = [
        "Catalog-ranked candidate metrics for this question, most relevant first "
        "(structured identity metadata only: name, Prometheus type, unit, HELP text). "
        "These were already limited to metrics documented in the reference(s) opened "
        "above. This list is a retrieval aid, not a restriction and not a source of "
        "semantics: the matched reference Markdown remains authoritative for what each "
        "metric means, Use-when guidance, confusable-metric distinctions, query "
        "construction and label keys. Prefer a listed candidate when the Markdown "
        "confirms it fits the question; if the Markdown shows another metric documented "
        "in the opened references is the better fit, follow the Markdown. Never use a "
        "metric that is not documented in the opened references:",
    ]
    for m in metric_metadata:
        unit = m["unit"] if m["unit"] else "n/a"
        lines.append(f"- {m['name']} (type: {m['type']}, unit: {unit}) -- {m['help']}")
    return "\n".join(lines)


def catalog_status_by_metric(catalog: Catalog) -> dict[str, str]:
    """{metric_name: catalog_status}: the minimal interface the validator
    needs, instead of threading the whole Catalog through validation."""
    return {m.name: m.status for m in catalog.metrics}
