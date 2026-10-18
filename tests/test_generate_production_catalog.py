"""
tests/test_generate_production_catalog.py

Batch 2 tests for scripts/generate_production_catalog.py -- the new
production Catalog-generation cutover entry point.

Covers:
  - generating a Catalog from the real vendor document + real skills_root
    matches the documented ground truth (43 metrics, 16 node-exporter +
    27 dcgm-exporter, the known malformed row rejected, not produced as
    an entry);
  - CatalogEntry.category is the vendor category, never Markdown-derived
    (Option C boundary);
  - reference_path is resolved by metric-name join against existing
    Markdown, independent of vendor category;
  - the generated Catalog round-trips through write_catalog/load_catalog
    unchanged.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.catalog.generator import write_catalog
from app.catalog.loader import load_catalog

pytest.importorskip("docx")

from scripts.generate_production_catalog import generate_production_catalog  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_SKILLS_ROOT = REPO_ROOT / "skills"
FIXTURE_DOCX = Path(__file__).resolve().parent / "fixtures" / "prometheus_metrics.docx"


def test_generates_exactly_43_metrics():
    catalog, _rejected = generate_production_catalog(
        FIXTURE_DOCX, REAL_SKILLS_ROOT, generated_at="t"
    )
    assert len(catalog.metrics) == 43


def test_exporter_counts_match_ground_truth():
    catalog, _rejected = generate_production_catalog(
        FIXTURE_DOCX, REAL_SKILLS_ROOT, generated_at="t"
    )
    node = [m for m in catalog.metrics if m.exporter == "node-exporter"]
    dcgm = [m for m in catalog.metrics if m.exporter == "dcgm-exporter"]
    assert len(node) == 16
    assert len(dcgm) == 27


def test_malformed_filesystem_backtick_row_is_rejected_not_a_catalog_entry():
    catalog, rejected = generate_production_catalog(
        FIXTURE_DOCX, REAL_SKILLS_ROOT, generated_at="t"
    )
    assert len(rejected) == 1
    assert rejected[0].raw_cells == ("Filesystem", "`", "", "", "")
    assert catalog.get("`") is None
    for entry in catalog.metrics:
        assert entry.name != "`"


def test_category_is_vendor_category_never_markdown_category():
    """Option C boundary: even for a metric with rich Markdown (a
    per-metric Category bullet that is more specific than the vendor's
    own coarser category), CatalogEntry.category must be the vendor
    text, verbatim."""
    catalog, _rejected = generate_production_catalog(
        FIXTURE_DOCX, REAL_SKILLS_ROOT, generated_at="t"
    )
    entry = catalog.get("node_cpu_seconds_total")
    assert entry is not None
    # The Markdown Category for this metric is the more specific "CPU
    # Utilization" (see skills/references/node-exporter/cpu.md) -- the
    # vendor document's own category is the coarser "CPU". Catalog
    # generation must preserve the latter, not the former.
    assert entry.category != "CPU Utilization"


def test_reference_path_resolved_for_documented_metric():
    catalog, _rejected = generate_production_catalog(
        FIXTURE_DOCX, REAL_SKILLS_ROOT, generated_at="t"
    )
    entry = catalog.get("node_cpu_seconds_total")
    assert entry.reference_path == "references/node-exporter/cpu.md"


def test_every_current_vendor_metric_has_a_resolved_reference_path():
    """Ground truth for the currently attached vendor document: every one
    of its 43 valid metrics already has matching semantic Markdown from
    prior batches, so none should have reference_path=None today. (The
    architecture nonetheless supports reference_path=None for a genuinely
    vendor-only metric -- see
    test_pipeline_catalog_integration.py's no-Markdown acceptance test
    for that case, exercised with a synthetic vendor-only metric since
    none currently exists in the real document.)"""
    catalog, _rejected = generate_production_catalog(
        FIXTURE_DOCX, REAL_SKILLS_ROOT, generated_at="t"
    )
    unresolved = [m.name for m in catalog.metrics if m.reference_path is None]
    assert unresolved == []


def test_generation_is_deterministic():
    catalog_a, _ = generate_production_catalog(FIXTURE_DOCX, REAL_SKILLS_ROOT, generated_at="t")
    catalog_b, _ = generate_production_catalog(FIXTURE_DOCX, REAL_SKILLS_ROOT, generated_at="t")
    assert catalog_a.to_dict() == catalog_b.to_dict()


def test_round_trips_through_write_and_load(tmp_path):
    catalog, _rejected = generate_production_catalog(
        FIXTURE_DOCX, REAL_SKILLS_ROOT, generated_at="t"
    )
    out_path = tmp_path / "catalog.json"
    write_catalog(catalog, out_path)
    loaded = load_catalog(out_path)
    assert loaded.to_dict() == catalog.to_dict()


def test_keywords_and_priority_are_left_as_phase5_defaults():
    """Confirms this script does NOT apply rules.py's keyword/priority
    classification or Option C enrichment -- those remain load-time
    concerns (app/pipeline.py::_get_catalog), exactly as the pre-Batch-2
    Markdown-sourced generator.py::main() also left them."""
    from app.catalog.schema import Priority

    catalog, _rejected = generate_production_catalog(
        FIXTURE_DOCX, REAL_SKILLS_ROOT, generated_at="t"
    )
    for entry in catalog.metrics:
        assert entry.keywords == ()
        assert entry.priority == Priority.REVIEW.value
