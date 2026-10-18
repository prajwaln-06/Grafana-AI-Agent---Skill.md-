"""
tests/test_search.py

Phase 6 tests: deterministic scoring/ranking behavior, status filtering
(rejected excluded by default, never a category/priority-based filter),
catalog-miss behavior, and a handful of representative real questions
against the real 43-metric catalog (Layer 2 of the reference doc's
testing strategy: "correct metric retained; candidate count; ranking;
ambiguous questions; catalog miss").
"""
from __future__ import annotations

from pathlib import Path

from app.catalog.generator import generate_catalog
from app.catalog.rules import apply_rules
from app.catalog.schema import Catalog, CatalogEntry, CatalogStatus, Priority
from app.catalog.search import DEFAULT_STATUSES, search

REAL_SKILLS_ROOT = Path(__file__).resolve().parent.parent / "skills"


def _entry(name, **kw):
    return CatalogEntry(
        name=name,
        type=kw.pop("type", "gauge"),
        category=kw.pop("category", "Test"),
        priority=kw.pop("priority", Priority.REVIEW.value),
        exporter=kw.pop("exporter", "node-exporter"),
        status=kw.pop("status", CatalogStatus.APPROVED.value),
        help=kw.pop("help", ""),
        keywords=kw.pop("keywords", ()),
        **kw,
    )


# ---- basic scoring/ordering -------------------------------------------


def test_search_returns_empty_list_for_no_match_is_a_catalog_miss():
    catalog = Catalog(catalog_version="1.0", generated_at="t",
                       metrics=(_entry("node_load1", help="1 minute load average"),))
    results = search(catalog, "completely unrelated query xyz")
    assert results == []


def test_search_finds_exact_name_token_match():
    catalog = Catalog(
        catalog_version="1.0", generated_at="t",
        metrics=(
            _entry("node_load1", help="1 minute load average"),
            _entry("node_memory_MemFree_bytes", help="free memory"),
        ),
    )
    results = search(catalog, "load average")
    assert results
    assert results[0].entry.name == "node_load1"


def test_search_orders_by_descending_score():
    catalog = Catalog(
        catalog_version="1.0", generated_at="t",
        metrics=(
            _entry("a_metric", category="Load", help="system load average"),
            _entry("b_metric", category="Other", help="mentions load once"),
        ),
    )
    results = search(catalog, "load")
    assert [r.score for r in results] == sorted((r.score for r in results), reverse=True)


def test_search_is_deterministic_and_ties_broken_alphabetically():
    catalog = Catalog(
        catalog_version="1.0", generated_at="t",
        metrics=(
            _entry("zzz_metric", help="widget widget"),
            _entry("aaa_metric", help="widget widget"),
        ),
    )
    r1 = search(catalog, "widget")
    r2 = search(catalog, "widget")
    assert [r.entry.name for r in r1] == [r.entry.name for r in r2]
    assert [r.entry.name for r in r1] == ["aaa_metric", "zzz_metric"]


def test_search_respects_top_n():
    catalog = Catalog(
        catalog_version="1.0", generated_at="t",
        metrics=tuple(_entry(f"metric_{i}", help="widget") for i in range(10)),
    )
    results = search(catalog, "widget", top_n=3)
    assert len(results) == 3


# ---- status filtering: rejected excluded, others included by default ---


def test_rejected_metrics_excluded_by_default():
    catalog = Catalog(
        catalog_version="1.0", generated_at="t",
        metrics=(_entry("node_load1", help="load average", status=CatalogStatus.REJECTED.value),),
    )
    assert search(catalog, "load average") == []


def test_approved_unavailable_included_by_default_pending_review_excluded():
    """Normal (query-generation-facing) retrieval defaults to
    approved + approved_unavailable only -- an unreviewed runtime-only
    metric must not become automatically eligible for query generation
    just by showing up in the catalog as discovered_pending_review."""
    catalog = Catalog(
        catalog_version="1.0", generated_at="t",
        metrics=(
            _entry("pending_metric", help="widget widget",
                   status=CatalogStatus.DISCOVERED_PENDING_REVIEW.value),
            _entry("unavailable_metric", help="widget widget",
                   status=CatalogStatus.APPROVED_UNAVAILABLE.value),
        ),
    )
    results = search(catalog, "widget")
    names = {r.entry.name for r in results}
    assert names == {"unavailable_metric"}


def test_caller_can_narrow_statuses_explicitly():
    catalog = Catalog(
        catalog_version="1.0", generated_at="t",
        metrics=(
            _entry("approved_metric", help="widget widget", status=CatalogStatus.APPROVED.value),
            _entry("pending_metric", help="widget widget",
                   status=CatalogStatus.DISCOVERED_PENDING_REVIEW.value),
        ),
    )
    results = search(catalog, "widget", statuses={CatalogStatus.APPROVED.value})
    assert [r.entry.name for r in results] == ["approved_metric"]


def test_caller_can_explicitly_widen_to_include_pending_review_for_review_purposes():
    """discovered_pending_review entries stay IN the catalog for review/
    discovery tooling -- a reviewer opts in explicitly via `statuses`."""
    catalog = Catalog(
        catalog_version="1.0", generated_at="t",
        metrics=(_entry("pending_metric", help="widget widget",
                         status=CatalogStatus.DISCOVERED_PENDING_REVIEW.value),),
    )
    results = search(catalog, "widget",
                      statuses={CatalogStatus.DISCOVERED_PENDING_REVIEW.value})
    assert [r.entry.name for r in results] == ["pending_metric"]


def test_default_statuses_constant_excludes_rejected_and_pending_review():
    assert CatalogStatus.REJECTED.value not in DEFAULT_STATUSES
    assert CatalogStatus.DISCOVERED_PENDING_REVIEW.value not in DEFAULT_STATUSES
    assert CatalogStatus.APPROVED.value in DEFAULT_STATUSES
    assert CatalogStatus.APPROVED_UNAVAILABLE.value in DEFAULT_STATUSES


# ---- priority is a tie-break only, never a filter or an override of relevance --


def test_high_priority_zero_relevance_entry_never_appears():
    catalog = Catalog(
        catalog_version="1.0", generated_at="t",
        metrics=(
            _entry("irrelevant_but_high_priority", help="nothing related",
                   priority=Priority.HIGH.value),
        ),
    )
    assert search(catalog, "gpu temperature") == []


def test_priority_breaks_ties_between_equally_relevant_entries():
    catalog = Catalog(
        catalog_version="1.0", generated_at="t",
        metrics=(
            _entry("low_pri", help="widget widget widget", priority=Priority.REVIEW.value),
            _entry("high_pri", help="widget widget widget", priority=Priority.HIGH.value),
        ),
    )
    results = search(catalog, "widget")
    assert results[0].entry.name == "high_pri"


# ---- against the real 43-metric catalog ---------------------------------


def _real_catalog() -> Catalog:
    catalog = generate_catalog(REAL_SKILLS_ROOT, generated_at="t")
    return apply_rules(catalog)


def test_real_catalog_load_average_question_retrieves_load_metrics():
    catalog = _real_catalog()
    results = search(catalog, "is the system overloaded")
    names = {r.entry.name for r in results}
    assert names & {"node_load1", "node_load5", "node_load15"}


def test_real_catalog_gpu_temperature_question_retrieves_gpu_temp():
    catalog = _real_catalog()
    results = search(catalog, "gpu temperature")
    assert results
    assert results[0].entry.name == "DCGM_FI_DEV_GPU_TEMP"


def test_real_catalog_nvlink_errors_question_retrieves_nvlink_health_metrics():
    catalog = _real_catalog()
    results = search(catalog, "nvlink errors")
    names = {r.entry.name for r in results}
    assert names & {
        "DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL",
        "DCGM_FI_DEV_NVLINK_RECOVERY_ERROR_COUNT_TOTAL",
    }


def test_real_catalog_unrelated_query_is_a_catalog_miss():
    catalog = _real_catalog()
    assert search(catalog, "quarterly sales revenue forecast") == []
