"""
tests/test_rules.py

Phase 5 tests: keyword generation, priority classification, and
apply_rules()'s default "don't clobber existing curation" behavior.
"""
from __future__ import annotations

from app.catalog.rules import apply_rules, classify_priority, generate_keywords
from app.catalog.schema import UNCATEGORIZED, Catalog, CatalogEntry, Priority


def _entry(name="node_cpu_seconds_total", category="CPU Utilization",
           help_="Measures CPU time spent in different modes, including user, system, idle.",
           **kw):
    return CatalogEntry(
        name=name,
        type=kw.pop("type", "counter"),
        category=category,
        priority=kw.pop("priority", Priority.REVIEW.value),
        exporter=kw.pop("exporter", "node-exporter"),
        status=kw.pop("status", "approved"),
        help=help_,
        **kw,
    )


# ---- generate_keywords ----------------------------------------------------


def test_generate_keywords_tokenizes_name_category_and_help():
    entry = _entry()
    keywords = generate_keywords(entry)
    assert "cpu" in keywords
    assert "seconds" in keywords
    assert "idle" in keywords


def test_generate_keywords_drops_stopwords_and_short_tokens():
    entry = _entry(help_="This is a value of the system, used for it.")
    keywords = generate_keywords(entry)
    for stop in ("this", "is", "a", "of", "the", "for", "it", "used"):
        assert stop not in keywords


def test_generate_keywords_deduplicates_preserving_first_occurrence():
    entry = _entry(name="node_cpu_seconds_total", category="CPU Utilization",
                    help_="CPU time in CPU modes.")
    keywords = generate_keywords(entry)
    assert keywords.count("cpu") == 1


def test_generate_keywords_is_capped():
    long_help = " ".join(f"wordnumber{i}" for i in range(50))
    entry = _entry(help_=long_help)
    keywords = generate_keywords(entry)
    assert len(keywords) <= 12


def test_generate_keywords_is_deterministic():
    entry = _entry()
    assert generate_keywords(entry) == generate_keywords(entry)


# ---- classify_priority -----------------------------------------------------


def test_classify_priority_high_for_reliability_and_ecc_and_temperature():
    assert classify_priority(_entry(category="Reliability")) == Priority.HIGH.value
    assert classify_priority(_entry(category="ECC")) == Priority.HIGH.value
    assert classify_priority(_entry(category="Temperature")) == Priority.HIGH.value


def test_classify_priority_medium_for_utilization_and_memory():
    assert classify_priority(_entry(category="GPU Utilization")) == Priority.MEDIUM.value
    assert classify_priority(_entry(category="Physical Memory")) == Priority.MEDIUM.value


def test_classify_priority_case_insensitive():
    assert classify_priority(_entry(category="temperature")) == Priority.HIGH.value
    assert classify_priority(_entry(category="TEMPERATURE")) == Priority.HIGH.value


def test_classify_priority_review_for_unmatched_category():
    assert classify_priority(_entry(category="Something Entirely Novel")) == Priority.REVIEW.value


def test_classify_priority_review_for_uncategorized():
    assert classify_priority(_entry(category=UNCATEGORIZED)) == Priority.REVIEW.value


def test_classify_priority_never_returns_a_value_outside_the_enum():
    from app.catalog.schema import Priority as P
    valid = {p.value for p in P}
    for category in ("Reliability", "GPU Utilization", "Something Novel", UNCATEGORIZED):
        assert classify_priority(_entry(category=category)) in valid


# ---- apply_rules ------------------------------------------------------------


def test_apply_rules_fills_review_priority_and_empty_keywords():
    catalog = Catalog(
        catalog_version="1.0", generated_at="t",
        metrics=(_entry(category="Reliability", priority=Priority.REVIEW.value, keywords=()),),
    )
    result = apply_rules(catalog)
    entry = result.get("node_cpu_seconds_total")
    assert entry.priority == Priority.HIGH.value
    assert entry.keywords != ()


def test_apply_rules_does_not_overwrite_existing_curated_keywords_by_default():
    catalog = Catalog(
        catalog_version="1.0", generated_at="t",
        metrics=(_entry(keywords=("manually", "curated")),),
    )
    result = apply_rules(catalog)
    assert result.get("node_cpu_seconds_total").keywords == ("manually", "curated")


def test_apply_rules_does_not_overwrite_non_review_priority_by_default():
    catalog = Catalog(
        catalog_version="1.0", generated_at="t",
        metrics=(_entry(category="Something Novel", priority=Priority.HIGH.value),),
    )
    result = apply_rules(catalog)
    assert result.get("node_cpu_seconds_total").priority == Priority.HIGH.value


def test_apply_rules_overwrite_existing_true_forces_reclassification():
    catalog = Catalog(
        catalog_version="1.0", generated_at="t",
        metrics=(_entry(category="Reliability", priority=Priority.MEDIUM.value,
                         keywords=("stale",)),),
    )
    result = apply_rules(catalog, overwrite_existing=True)
    entry = result.get("node_cpu_seconds_total")
    assert entry.priority == Priority.HIGH.value
    assert entry.keywords != ("stale",)


def test_apply_rules_never_touches_category_or_status_or_name():
    catalog = Catalog(
        catalog_version="1.0", generated_at="t",
        metrics=(_entry(category="Reliability", status="approved_unavailable"),),
    )
    result = apply_rules(catalog)
    entry = result.get("node_cpu_seconds_total")
    assert entry.category == "Reliability"
    assert entry.status == "approved_unavailable"
    assert entry.name == "node_cpu_seconds_total"


def test_apply_rules_produces_a_valid_catalog_for_the_real_43_metrics():
    from pathlib import Path
    from app.catalog.generator import generate_catalog

    real_skills_root = Path(__file__).resolve().parent.parent / "skills"
    catalog = generate_catalog(real_skills_root, generated_at="t")
    result = apply_rules(catalog)
    assert len(result.metrics) == 43
    for entry in result.metrics:
        assert entry.priority in {p.value for p in Priority}
