"""
tests/test_catalog_generator.py

Phase 2 tests: does app/catalog/generator.py correctly derive a 43-metric
catalog from the real skills/ package on disk, and does it behave
correctly (fail loudly, stay deterministic) against small synthetic
fixtures that exercise edge cases the real skill package doesn't happen
to hit (a missing required bullet, an unparsable Metric Directory row, a
Metric Directory row pointing at a domain file that doesn't exist).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.catalog.generator import (
    CatalogGenerationError,
    generate_catalog,
    write_catalog,
)
from app.catalog.loader import load_catalog
from app.catalog.schema import CatalogSchemaError

REAL_SKILLS_ROOT = Path(__file__).resolve().parent.parent / "skills"


# ---- against the real skill package -----------------------------------


def test_generates_all_43_metrics_from_real_skill_package():
    catalog = generate_catalog(REAL_SKILLS_ROOT, generated_at="2026-08-27T00:00:00Z")
    assert len(catalog.metrics) == 43


def test_generates_16_node_exporter_and_27_dcgm_exporter_metrics():
    catalog = generate_catalog(REAL_SKILLS_ROOT, generated_at="2026-08-27T00:00:00Z")
    by_exporter: dict[str, int] = {}
    for entry in catalog.metrics:
        by_exporter[entry.exporter] = by_exporter.get(entry.exporter, 0) + 1
    assert by_exporter == {"node-exporter": 16, "dcgm-exporter": 27}


def test_no_duplicate_metric_names():
    # Catalog.__post_init__ already raises on a duplicate name; generating
    # successfully at all is itself the assertion, but spell it out too.
    catalog = generate_catalog(REAL_SKILLS_ROOT, generated_at="2026-08-27T00:00:00Z")
    names = [entry.name for entry in catalog.metrics]
    assert len(names) == len(set(names))


def test_every_entry_has_required_source_derived_fields_populated():
    catalog = generate_catalog(REAL_SKILLS_ROOT, generated_at="2026-08-27T00:00:00Z")
    for entry in catalog.metrics:
        assert entry.name.strip()
        assert entry.type in ("counter", "gauge", "histogram", "summary")
        assert entry.category.strip()
        assert entry.help.strip(), f"{entry.name} has no help/Purpose text"
        assert entry.exporter.strip()
        assert entry.reference_path is not None
        assert entry.reference_path.startswith(f"references/{entry.exporter}/")


def test_phase_2_defaults_are_explicit_not_guessed():
    """Priority, keywords, and dimensions are explicitly out of scope for
    Phase 2 (Phase 5's job) -- every entry should carry the schema's own
    "not yet classified" values, never an invented guess."""
    catalog = generate_catalog(REAL_SKILLS_ROOT, generated_at="2026-08-27T00:00:00Z")
    for entry in catalog.metrics:
        assert entry.priority == "Review"
        assert entry.keywords == ()
        assert entry.dimensions == ()
        assert entry.status == "approved"


def test_unit_field_preserved_verbatim_including_caveats():
    catalog = generate_catalog(REAL_SKILLS_ROOT, generated_at="2026-08-27T00:00:00Z")
    node_load1 = catalog.get("node_load1")
    assert node_load1 is not None
    assert node_load1.unit is not None
    assert "Not stated in the authoritative document" in node_load1.unit

    power_violation = catalog.get("DCGM_FI_DEV_POWER_VIOLATION")
    assert power_violation is not None
    assert "exact exposed unit should be verified" in power_violation.unit


def test_multiline_purpose_bullet_joined_into_one_string():
    catalog = generate_catalog(REAL_SKILLS_ROOT, generated_at="2026-08-27T00:00:00Z")
    entry = catalog.get("node_cpu_seconds_total")
    assert entry is not None
    assert entry.help == (
        "Measures CPU time spent in different modes, including user, "
        "system, idle, iowait, and other CPU modes."
    )


def test_category_is_per_metric_level2_category_not_metric_directory_domain():
    """category comes from the domain file's own "- **Category:**" bullet
    (fine-grained), not the Metric Directory's coarser "Domain" column
    (e.g. "cpu"/"memory") -- see generator.py's module docstring."""
    catalog = generate_catalog(REAL_SKILLS_ROOT, generated_at="2026-08-27T00:00:00Z")
    assert catalog.get("node_cpu_seconds_total").category == "CPU Utilization"
    assert catalog.get("node_load1").category == "System Load"
    assert catalog.get("node_context_switches_total").category == "CPU Scheduling Activity"


# ---- determinism / idempotency -----------------------------------------


def test_generation_is_deterministic_given_fixed_generated_at():
    c1 = generate_catalog(REAL_SKILLS_ROOT, generated_at="fixed")
    c2 = generate_catalog(REAL_SKILLS_ROOT, generated_at="fixed")
    assert c1.to_dict() == c2.to_dict()


def test_generation_order_is_stable_across_runs():
    c1 = generate_catalog(REAL_SKILLS_ROOT, generated_at="fixed")
    c2 = generate_catalog(REAL_SKILLS_ROOT, generated_at="fixed")
    assert [e.name for e in c1.metrics] == [e.name for e in c2.metrics]


def test_write_then_load_round_trips_through_real_loader(tmp_path):
    catalog = generate_catalog(REAL_SKILLS_ROOT, generated_at="2026-08-27T00:00:00Z")
    out = tmp_path / "catalog.json"
    write_catalog(catalog, out)
    reloaded = load_catalog(out)
    assert reloaded.to_dict() == catalog.to_dict()


# ---- synthetic fixtures: fail-loudly behavior ---------------------------


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_missing_required_bullet_raises_generation_error(tmp_path):
    references = tmp_path / "references"
    _write(
        references / "acme-exporter" / "overview.md",
        (
            "## Metric Directory\n\n"
            "| Domain | Intent / Measurement | Metric | Detail File |\n"
            "|---|---|---|---|\n"
            "| widget | Widget count | `acme_widget_count` | `widget.md` |\n"
        ),
    )
    _write(
        references / "acme-exporter" / "widget.md",
        (
            "### `acme_widget_count`\n\n"
            "- **Category:** Widgets\n"
            "- **Type:** `Gauge`\n"
            # Purpose intentionally omitted.
        ),
    )
    with pytest.raises(CatalogGenerationError, match="Purpose"):
        generate_catalog(tmp_path)


def test_metric_directory_row_pointing_at_missing_file_raises(tmp_path):
    references = tmp_path / "references"
    _write(
        references / "acme-exporter" / "overview.md",
        (
            "## Metric Directory\n\n"
            "| Domain | Intent / Measurement | Metric | Detail File |\n"
            "|---|---|---|---|\n"
            "| widget | Widget count | `acme_widget_count` | `missing.md` |\n"
        ),
    )
    with pytest.raises(CatalogGenerationError, match="does not exist"):
        generate_catalog(tmp_path)


def test_unparsable_metric_directory_row_raises(tmp_path):
    references = tmp_path / "references"
    _write(
        references / "acme-exporter" / "overview.md",
        (
            "## Metric Directory\n\n"
            "| Domain | Intent / Measurement | Metric | Detail File |\n"
            "|---|---|---|---|\n"
            "| widget | Widget count | not-backticked | widget.md |\n"
        ),
    )
    with pytest.raises(CatalogGenerationError, match="could not parse"):
        generate_catalog(tmp_path)


def test_no_metric_directory_table_raises(tmp_path):
    references = tmp_path / "references"
    _write(
        references / "acme-exporter" / "overview.md",
        "## Metric Directory\n\nNothing here.\n",
    )
    with pytest.raises(CatalogGenerationError, match="no Metric Directory rows"):
        generate_catalog(tmp_path)


def test_missing_references_dir_raises(tmp_path):
    with pytest.raises(CatalogGenerationError, match="does not exist"):
        generate_catalog(tmp_path)


def test_undocumented_type_value_raises_catalog_schema_error(tmp_path):
    references = tmp_path / "references"
    _write(
        references / "acme-exporter" / "overview.md",
        (
            "## Metric Directory\n\n"
            "| Domain | Intent / Measurement | Metric | Detail File |\n"
            "|---|---|---|---|\n"
            "| widget | Widget count | `acme_widget_count` | `widget.md` |\n"
        ),
    )
    _write(
        references / "acme-exporter" / "widget.md",
        (
            "### `acme_widget_count`\n\n"
            "- **Category:** Widgets\n"
            "- **Purpose:** Counts widgets.\n"
            "- **Type:** `Frobnicator`\n"
        ),
    )
    with pytest.raises(CatalogSchemaError):
        generate_catalog(tmp_path)
