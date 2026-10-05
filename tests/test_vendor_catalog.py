"""
tests/test_vendor_catalog.py

Batch 1 tests for:
  - app/catalog/vendor.py::build_catalog_from_vendor_records (vendor
    record -> CatalogEntry mapping, including reference_path resolution)
  - app/catalog/generator.py::build_markdown_reference_index (the
    factored-out "existing Markdown metric-discovery logic" the vendor
    path reuses)
  - a full real-vendor-document -> candidate-Catalog generation, diffed
    against the currently committed app/catalog/catalog.json, per the
    frozen migration architecture's "generate the candidate Catalog
    first and inspect/diff it before production cutover" requirement.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.catalog.generator import build_markdown_reference_index
from app.catalog.loader import load_catalog
from app.catalog.schema import CatalogStatus, Priority
from app.catalog.vendor import VendorMetricRecord, build_catalog_from_vendor_records
from app.catalog.vendor_docx import parse_vendor_docx

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_SKILLS_ROOT = REPO_ROOT / "skills"
REAL_CATALOG_PATH = REPO_ROOT / "app" / "catalog" / "catalog.json"
FIXTURE_DOCX = Path(__file__).resolve().parent / "fixtures" / "prometheus_metrics.docx"


# ---- build_markdown_reference_index ------------------------------------


def test_markdown_reference_index_covers_all_43_documented_metrics():
    index = build_markdown_reference_index(REAL_SKILLS_ROOT)
    assert len(index) == 43
    assert index["node_cpu_seconds_total"] == "references/node-exporter/cpu.md"
    assert index["DCGM_FI_DEV_GPU_UTIL"] == "references/dcgm-exporter/compute.md"


def test_markdown_reference_index_empty_for_skills_root_with_no_references(tmp_path):
    index = build_markdown_reference_index(tmp_path)
    assert index == {}


# ---- build_catalog_from_vendor_records: field mapping -------------------


def test_maps_name_type_category_help_from_vendor_record():
    record = VendorMetricRecord(
        name="node_cpu_seconds_total",
        type="Counter",
        category="CPU",
        help="CPU time spent in different modes.",
        exporter="node-exporter",
    )
    catalog = build_catalog_from_vendor_records(
        (record,), markdown_reference_index={}, generated_at="2026-08-30T00:00:00Z"
    )
    entry = catalog.get("node_cpu_seconds_total")
    assert entry is not None
    assert entry.type == "counter"  # normalized lowercase, matches MetricType
    assert entry.category == "CPU"
    assert entry.help == "CPU time spent in different modes."
    assert entry.exporter == "node-exporter"


def test_unit_defaults_to_none_when_record_has_no_unit():
    """A VendorMetricRecord built without an explicit `unit` (the default)
    carries through as CatalogEntry.unit=None -- build_catalog_from_vendor_records
    never invents one itself, it only passes through what the record has."""
    record = VendorMetricRecord(
        name="node_load1", type="Gauge", category="Load", help="1-minute load average.",
        exporter="node-exporter",
    )
    catalog = build_catalog_from_vendor_records((record,), markdown_reference_index={})
    assert catalog.get("node_load1").unit is None


def test_unit_is_carried_through_verbatim_when_record_has_one():
    """When the vendor parser DID extract a unit onto the record,
    build_catalog_from_vendor_records passes it through unchanged."""
    record = VendorMetricRecord(
        name="DCGM_FI_DEV_GPU_TEMP", type="Gauge", category="Thermal",
        help="GPU core temperature (°C)", exporter="dcgm-exporter", unit="°C",
    )
    catalog = build_catalog_from_vendor_records((record,), markdown_reference_index={})
    assert catalog.get("DCGM_FI_DEV_GPU_TEMP").unit == "°C"


def test_default_status_priority_keywords_dimensions():
    record = VendorMetricRecord(
        name="node_load1", type="Gauge", category="Load", help="1-minute load average.",
        exporter="node-exporter",
    )
    catalog = build_catalog_from_vendor_records((record,), markdown_reference_index={})
    entry = catalog.get("node_load1")
    assert entry.status == CatalogStatus.APPROVED.value
    assert entry.priority == Priority.REVIEW.value
    assert entry.keywords == ()
    assert entry.dimensions == ()


# ---- reference_path resolution: metric-name join, not vendor category ---


def test_metric_present_in_markdown_gets_existing_reference_path():
    record = VendorMetricRecord(
        name="node_cpu_seconds_total", type="Counter", category="CPU",
        help="CPU time.", exporter="node-exporter",
    )
    markdown_index = {"node_cpu_seconds_total": "references/node-exporter/cpu.md"}
    catalog = build_catalog_from_vendor_records((record,), markdown_index)
    assert catalog.get("node_cpu_seconds_total").reference_path == "references/node-exporter/cpu.md"


def test_metric_absent_from_markdown_gets_reference_path_none():
    record = VendorMetricRecord(
        name="node_brand_new_metric", type="Gauge", category="CPU",
        help="Something new.", exporter="node-exporter",
    )
    catalog = build_catalog_from_vendor_records((record,), markdown_reference_index={})
    entry = catalog.get("node_brand_new_metric")
    assert entry is not None
    assert entry.reference_path is None


def test_vendor_category_never_used_as_reference_path():
    """A record whose vendor `category` happens to collide with a real
    domain-file name (e.g. category='cpu') must NOT get reference_path
    derived from that category -- only a metric-NAME match in
    markdown_reference_index may populate reference_path."""
    record = VendorMetricRecord(
        name="node_totally_unrelated_metric",
        type="Gauge",
        category="cpu",  # deliberately collides with a real domain filename
        help="Not actually CPU-related in Markdown.",
        exporter="node-exporter",
    )
    markdown_index = {"node_cpu_seconds_total": "references/node-exporter/cpu.md"}
    catalog = build_catalog_from_vendor_records((record,), markdown_index)
    assert catalog.get("node_totally_unrelated_metric").reference_path is None


# ---- end-to-end candidate generation + diff against committed catalog ---


@pytest.fixture(scope="module")
def candidate_catalog():
    result = parse_vendor_docx(FIXTURE_DOCX)
    markdown_index = build_markdown_reference_index(REAL_SKILLS_ROOT)
    return build_catalog_from_vendor_records(
        result.records, markdown_index, generated_at="2026-08-30T00:00:00Z"
    )


@pytest.fixture(scope="module")
def committed_catalog():
    return load_catalog(REAL_CATALOG_PATH)


def test_candidate_catalog_has_same_43_metric_names_as_committed(
    candidate_catalog, committed_catalog
):
    candidate_names = {m.name for m in candidate_catalog.metrics}
    committed_names = {m.name for m in committed_catalog.metrics}
    assert candidate_names == committed_names


def test_candidate_catalog_preserves_type_for_every_metric(candidate_catalog, committed_catalog):
    committed_by_name = {m.name: m for m in committed_catalog.metrics}
    mismatches = [
        (m.name, m.type, committed_by_name[m.name].type)
        for m in candidate_catalog.metrics
        if m.type != committed_by_name[m.name].type
    ]
    assert mismatches == []


def test_candidate_catalog_preserves_reference_path_for_documented_metrics(
    candidate_catalog, committed_catalog
):
    """Every metric that has Markdown documentation today must keep the
    exact same reference_path in the vendor-sourced candidate -- proving
    the metric-name join reproduces the existing routing structure
    exactly, not merely "some" reference path."""
    committed_by_name = {m.name: m for m in committed_catalog.metrics}
    mismatches = [
        (m.name, m.reference_path, committed_by_name[m.name].reference_path)
        for m in candidate_catalog.metrics
        if m.reference_path != committed_by_name[m.name].reference_path
    ]
    assert mismatches == []


def test_candidate_catalog_category_matches_committed_for_dcgm_exporter(
    candidate_catalog, committed_catalog
):
    """dcgm-exporter's committed per-metric Category bullets were authored
    verbatim from the vendor Category column, so all 27 must match
    exactly -- a mismatch here WOULD mean the vendor mapping picked the
    wrong column."""
    committed_by_name = {m.name: m for m in committed_catalog.metrics}
    mismatches = [
        (m.name, m.category, committed_by_name[m.name].category)
        for m in candidate_catalog.metrics
        if m.exporter == "dcgm-exporter" and m.category != committed_by_name[m.name].category
    ]
    assert mismatches == []


# Batch 2 corrective patch: prior to the production vendor-source cutover,
# `committed_catalog` above still held the LEGACY Markdown-sourced
# category values, while `candidate_catalog` (built from the vendor
# document directly, exactly like scripts/generate_production_catalog.py)
# already held vendor categories -- so the two diverged for these 14
# node-exporter metrics, where node-exporter's per-metric Category bullets
# happen to be a finer-grained curation than the vendor document's own
# coarser Category column (see generator.py's own module docstring).
#
# Now that the production cutover has actually happened (`committed_catalog`
# loads the real, vendor-source-generated app/catalog/catalog.json -- see
# scripts/generate_production_catalog.py and Batch 2's report), BOTH
# catalogs derive `category` from the same vendor source, so they no
# longer diverge at all: the expected set is empty. This is a genuine
# behavior change (the cutover happened), not a loosened assertion --
# Option C's own invariant (CatalogEntry.category = vendor category,
# Markdown Category -> keywords only, never a category override) is
# exactly why they now agree. See
# tests/test_pipeline_catalog_integration.py::
# test_existing_markdown_backed_metric_gets_category_enrichment_keywords
# for where the retired 14-metric Markdown-category information now
# actually gets used (as keyword enrichment, not a category value).
_EXPECTED_NODE_EXPORTER_CATEGORY_DIVERGENCE: dict[str, tuple[str, str]] = {}


def test_candidate_catalog_node_exporter_category_divergence_is_exactly_the_known_set(
    candidate_catalog, committed_catalog
):
    committed_by_name = {m.name: m for m in committed_catalog.metrics}
    actual_divergence = {
        m.name: (m.category, committed_by_name[m.name].category)
        for m in candidate_catalog.metrics
        if m.exporter == "node-exporter" and m.category != committed_by_name[m.name].category
    }
    assert actual_divergence == _EXPECTED_NODE_EXPORTER_CATEGORY_DIVERGENCE


def test_candidate_catalog_no_metric_loses_its_reference_path(candidate_catalog):
    """None of the 43 currently-documented metrics should regress to
    reference_path=None in the candidate -- every one of them must
    resolve via the metric-name join against real, existing Markdown."""
    missing = [m.name for m in candidate_catalog.metrics if m.reference_path is None]
    assert missing == []


def test_candidate_catalog_has_exactly_14_metrics_with_a_unit(candidate_catalog):
    """End-to-end: parse_vendor_docx -> build_catalog_from_vendor_records
    on the real fixture document must produce exactly the 14 vendor-stated
    units, nothing more (never inferred) and nothing less (never dropped)."""
    with_unit = {m.name: m.unit for m in candidate_catalog.metrics if m.unit is not None}
    assert len(with_unit) == 14
    assert with_unit["DCGM_FI_DEV_FB_USED"] == "bytes"
    assert candidate_catalog.get("node_cpu_seconds_total").unit is None
