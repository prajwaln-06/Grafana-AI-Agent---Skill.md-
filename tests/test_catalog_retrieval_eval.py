"""
tests/test_catalog_retrieval_eval.py

Phase 7: exercises scripts/evaluate_catalog_retrieval.py against the real,
generated 43-metric catalog. Deliberately asserts STRUCTURE, not a
minimum recall number -- per the explicit Batch 2 review guidance, weight
tuning is out of scope for this batch; a hard-coded recall floor here
would either fail immediately (today's untuned weights) or have to be set
so low it catches nothing, and would need to be hand-adjusted the moment
search.py's weights are legitimately improved later. The measured numbers
themselves are surfaced in the Batch 3 report, not gated on here.
"""
from __future__ import annotations

from pathlib import Path

from app.catalog.generator import generate_catalog
from app.catalog.rules import apply_rules
from scripts.evaluate_catalog_retrieval import (
    MULTI_METRIC_QUESTION_SET,
    QUESTION_SET,
    evaluate,
    evaluate_multi_metric,
    evaluate_reference_level,
    format_multi_metric_report,
    format_reference_level_report,
    format_report,
)

REAL_SKILLS_ROOT = Path(__file__).resolve().parent.parent / "skills"


def _real_catalog():
    return apply_rules(generate_catalog(REAL_SKILLS_ROOT, generated_at="t"))


def test_question_set_covers_all_43_metrics():
    """The question set is meant to be representative of the whole
    catalog -- every one of the 43 metrics should appear as an expected
    answer for at least one question. Some metrics have more than one
    listed intent in the source table (e.g. node_cpu_seconds_total covers
    "CPU utilization, CPU busy, CPU idle"), so a metric may legitimately
    appear more than once -- that reflects the source documentation, not
    duplication error."""
    catalog = _real_catalog()
    all_names = {m.name for m in catalog.metrics}
    expected_names = {expected for _, expected in QUESTION_SET}
    assert expected_names == all_names


def test_question_set_questions_are_sourced_not_duplicated():
    questions = [q for q, _ in QUESTION_SET]
    assert len(questions) == len(set(questions))


def test_evaluate_produces_one_result_per_question():
    catalog = _real_catalog()
    report = evaluate(catalog, top_n=5)
    assert report.total == len(QUESTION_SET)
    assert len(report.results) == len(QUESTION_SET)


def test_evaluate_rank_is_consistent_with_candidate_position():
    catalog = _real_catalog()
    report = evaluate(catalog, top_n=5)
    for r in report.results:
        if r.rank is not None:
            assert r.candidate_names[r.rank - 1] == r.expected_metric
        else:
            assert r.expected_metric not in r.candidate_names


def test_evaluate_recall_metrics_are_internally_consistent():
    catalog = _real_catalog()
    report = evaluate(catalog, top_n=5)
    assert report.hit_at_1 <= report.hit_at_top_n <= report.total
    assert 0.0 <= report.recall_at_1 <= report.recall_at_top_n <= 1.0
    assert 0.0 <= report.mean_reciprocal_rank <= 1.0
    assert len(report.misses) == report.total - report.hit_at_top_n


def test_format_report_is_human_readable_and_runs_without_error():
    catalog = _real_catalog()
    report = evaluate(catalog, top_n=5)
    text = format_report(report)
    assert "recall@1" in text
    assert "recall@5" in text


def test_evaluate_is_deterministic():
    catalog = _real_catalog()
    r1 = evaluate(catalog, top_n=5)
    r2 = evaluate(catalog, top_n=5)
    assert [r.rank for r in r1.results] == [r.rank for r in r2.results]


# ---- Batch 4 (Phase 10): reference-level recall / false-positive / multi-metric --
#
# Same "measure, don't gate on a hard-coded number" philosophy as the
# metric-level tests above: these assert the new report types are
# internally consistent and runnable against the real catalog, not that
# they clear some chosen safety percentage -- that number belongs in the
# Batch 4 report, not frozen into a test that would need hand-adjusting
# every time search.py's weights legitimately change.


def test_reference_level_outcomes_partition_the_question_set():
    catalog = _real_catalog()
    report = evaluate_reference_level(catalog)
    assert report.total == len(QUESTION_SET)
    outcomes = [r.outcome for r in report.results]
    # Every question lands in exactly one bucket; none of the 43 approved
    # metrics used as expected answers should ever be missing a
    # reference_path (that would be a catalog-authoring bug, not a
    # retrieval-quality finding).
    assert "no_reference" not in outcomes
    assert set(outcomes) <= {"safe_hit", "unsafe_narrow", "true_miss", "low_confidence_fallback"}


def test_reference_level_safety_counters_are_internally_consistent():
    catalog = _real_catalog()
    report = evaluate_reference_level(catalog)
    assert report.safe_hits + report.unsafe_narrows + report.true_misses + report.low_confidence_fallbacks == report.total
    assert 0.0 <= report.reference_visible_rate <= 1.0
    assert 0.0 <= report.unsafe_narrow_rate <= 1.0
    assert len(report.unsafe_narrow_cases) == report.unsafe_narrows


def test_reference_level_visible_rate_is_never_worse_than_metric_level_recall_at_5():
    """The whole point of searching uncapped-by-rank for narrowing (rather
    than reusing search.py's user-facing top_n=5) is that it should never
    be LESS safe than the metric-level top-5 view -- it can only do as
    well or better, since every top-5 hit is trivially still a candidate
    once the rank cap is lifted."""
    catalog = _real_catalog()
    metric_level = evaluate(catalog, top_n=5)
    reference_level = evaluate_reference_level(catalog)
    assert reference_level.reference_visible_rate >= metric_level.recall_at_top_n


def test_format_reference_level_report_is_human_readable_and_runs_without_error():
    catalog = _real_catalog()
    report = evaluate_reference_level(catalog)
    text = format_reference_level_report(report)
    assert "reference visible rate" in text
    assert "unsafe narrows" in text


def test_multi_metric_question_set_uses_real_catalog_metrics():
    catalog = _real_catalog()
    all_names = {m.name for m in catalog.metrics}
    for _, expected_metrics in MULTI_METRIC_QUESTION_SET:
        assert len(expected_metrics) >= 2, "a multi-metric question must expect 2+ metrics"
        for metric in expected_metrics:
            assert metric in all_names


def test_multi_metric_coverage_is_internally_consistent():
    catalog = _real_catalog()
    report = evaluate_multi_metric(catalog)
    assert report.total == len(MULTI_METRIC_QUESTION_SET)
    assert report.fully_covered == report.total - len(report.incomplete_cases)
    assert 0.0 <= report.coverage_rate <= 1.0
    for r in report.incomplete_cases:
        assert len(r.missing_reference_paths) > 0


def test_format_multi_metric_report_is_human_readable_and_runs_without_error():
    catalog = _real_catalog()
    report = evaluate_multi_metric(catalog)
    text = format_multi_metric_report(report)
    assert "fully covered" in text
