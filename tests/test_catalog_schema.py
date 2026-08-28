import pytest

from app.catalog.schema import (
    Catalog,
    CatalogEntry,
    CatalogSchemaError,
    CatalogStatus,
    MetricType,
    Priority,
    UNCATEGORIZED,
)


def _valid_entry_dict(**overrides):
    data = {
        "name": "node_cpu_seconds_total",
        "type": "counter",
        "help": "Seconds the CPUs spent in each mode.",
        "unit": "seconds",
        "keywords": ["cpu", "usage"],
        "category": "node_hardware",
        "priority": "High",
        "exporter": "node-exporter",
        "status": "approved",
        "reference_path": "references/node-exporter/cpu.md",
        "dimensions": ["cpu", "mode"],
    }
    data.update(overrides)
    return data


# ---- CatalogEntry: valid construction -------------------------------------


def test_entry_from_dict_round_trips_to_dict():
    entry = CatalogEntry.from_dict(_valid_entry_dict())
    assert entry.name == "node_cpu_seconds_total"
    assert entry.type == MetricType.COUNTER.value
    assert entry.keywords == ("cpu", "usage")
    assert entry.dimensions == ("cpu", "mode")
    assert entry.to_dict() == {
        "name": "node_cpu_seconds_total",
        "type": "counter",
        "help": "Seconds the CPUs spent in each mode.",
        "unit": "seconds",
        "keywords": ["cpu", "usage"],
        "category": "node_hardware",
        "priority": "High",
        "exporter": "node-exporter",
        "status": "approved",
        "reference_path": "references/node-exporter/cpu.md",
        "dimensions": ["cpu", "mode"],
    }


def test_entry_optional_fields_default_sensibly():
    minimal = {
        "name": "node_load1",
        "type": "gauge",
        "category": "node_hardware",
        "priority": "Medium",
        "exporter": "node-exporter",
        "status": "approved",
    }
    entry = CatalogEntry.from_dict(minimal)
    assert entry.help == ""
    assert entry.unit is None
    assert entry.keywords == ()
    assert entry.reference_path is None
    assert entry.dimensions == ()


def test_entry_reference_path_may_be_null():
    entry = CatalogEntry.from_dict(_valid_entry_dict(reference_path=None))
    assert entry.reference_path is None


def test_entry_accepts_uncategorized_fallback_category():
    entry = CatalogEntry.from_dict(_valid_entry_dict(category=UNCATEGORIZED))
    assert entry.category == "uncategorized"


def test_entry_accepts_review_priority():
    entry = CatalogEntry.from_dict(_valid_entry_dict(priority=Priority.REVIEW.value))
    assert entry.priority == "Review"


def test_entry_accepts_all_catalog_statuses():
    for status in CatalogStatus:
        entry = CatalogEntry.from_dict(_valid_entry_dict(status=status.value))
        assert entry.status == status.value


# ---- CatalogEntry: validation failures ------------------------------------


def test_entry_missing_name_raises():
    data = _valid_entry_dict()
    del data["name"]
    with pytest.raises(CatalogSchemaError):
        CatalogEntry.from_dict(data)


def test_entry_empty_name_raises():
    with pytest.raises(CatalogSchemaError):
        CatalogEntry.from_dict(_valid_entry_dict(name="   "))


def test_entry_invalid_type_raises():
    with pytest.raises(CatalogSchemaError):
        CatalogEntry.from_dict(_valid_entry_dict(type="not_a_real_type"))


def test_entry_invalid_priority_raises():
    with pytest.raises(CatalogSchemaError):
        CatalogEntry.from_dict(_valid_entry_dict(priority="Urgent"))


def test_entry_invalid_status_raises():
    with pytest.raises(CatalogSchemaError):
        CatalogEntry.from_dict(_valid_entry_dict(status="totally_approved"))


def test_entry_blank_category_raises():
    with pytest.raises(CatalogSchemaError):
        CatalogEntry.from_dict(_valid_entry_dict(category=""))


def test_entry_blank_exporter_raises():
    with pytest.raises(CatalogSchemaError):
        CatalogEntry.from_dict(_valid_entry_dict(exporter="  "))


def test_entry_empty_string_reference_path_raises():
    """None is a valid 'no reference' sentinel; an empty string is not --
    it almost certainly indicates a generation bug rather than a genuine
    'no Markdown reference exists' case."""
    with pytest.raises(CatalogSchemaError):
        CatalogEntry.from_dict(_valid_entry_dict(reference_path=""))


def test_entry_is_frozen():
    entry = CatalogEntry.from_dict(_valid_entry_dict())
    with pytest.raises(Exception):
        entry.name = "something_else"  # type: ignore[misc]


# ---- Catalog: valid construction ------------------------------------------


def test_catalog_from_dict_round_trips_to_dict():
    doc = {
        "catalog_version": "1.0",
        "generated_at": "2026-08-27T00:00:00Z",
        "metrics": [_valid_entry_dict(), _valid_entry_dict(name="node_load1", type="gauge")],
    }
    catalog = Catalog.from_dict(doc)
    assert catalog.catalog_version == "1.0"
    assert len(catalog.metrics) == 2
    assert catalog.to_dict()["catalog_version"] == "1.0"
    assert len(catalog.to_dict()["metrics"]) == 2


def test_catalog_may_have_zero_metrics():
    doc = {"catalog_version": "1.0", "generated_at": "2026-08-27T00:00:00Z", "metrics": []}
    catalog = Catalog.from_dict(doc)
    assert catalog.metrics == ()


def test_catalog_metrics_field_optional_defaults_empty():
    doc = {"catalog_version": "1.0", "generated_at": "2026-08-27T00:00:00Z"}
    catalog = Catalog.from_dict(doc)
    assert catalog.metrics == ()


# ---- Catalog: validation failures -----------------------------------------


def test_catalog_missing_catalog_version_raises():
    doc = {"generated_at": "2026-08-27T00:00:00Z", "metrics": []}
    with pytest.raises(CatalogSchemaError):
        Catalog.from_dict(doc)


def test_catalog_missing_generated_at_raises():
    doc = {"catalog_version": "1.0", "metrics": []}
    with pytest.raises(CatalogSchemaError):
        Catalog.from_dict(doc)


def test_catalog_metrics_not_a_list_raises():
    doc = {"catalog_version": "1.0", "generated_at": "x", "metrics": {"not": "a list"}}
    with pytest.raises(CatalogSchemaError):
        Catalog.from_dict(doc)


def test_catalog_duplicate_metric_name_raises():
    """Mandatory duplicate-detection requirement (Layer 1 test list)."""
    doc = {
        "catalog_version": "1.0",
        "generated_at": "2026-08-27T00:00:00Z",
        "metrics": [_valid_entry_dict(), _valid_entry_dict()],
    }
    with pytest.raises(CatalogSchemaError):
        Catalog.from_dict(doc)


def test_catalog_one_invalid_entry_fails_the_whole_load():
    """A single malformed entry should fail loudly at load time rather than
    silently drop that one metric and continue -- this is the fail-closed
    behavior the frozen architecture requires."""
    doc = {
        "catalog_version": "1.0",
        "generated_at": "2026-08-27T00:00:00Z",
        "metrics": [_valid_entry_dict(), _valid_entry_dict(name="bad_one", type="bogus")],
    }
    with pytest.raises(CatalogSchemaError):
        Catalog.from_dict(doc)


# ---- Catalog: lookup helpers -----------------------------------------------


def test_catalog_get_returns_entry_by_name():
    doc = {
        "catalog_version": "1.0",
        "generated_at": "x",
        "metrics": [_valid_entry_dict(), _valid_entry_dict(name="node_load1")],
    }
    catalog = Catalog.from_dict(doc)
    found = catalog.get("node_load1")
    assert found is not None
    assert found.name == "node_load1"


def test_catalog_get_returns_none_for_unknown_name():
    doc = {"catalog_version": "1.0", "generated_at": "x", "metrics": [_valid_entry_dict()]}
    catalog = Catalog.from_dict(doc)
    assert catalog.get("does_not_exist") is None


def test_catalog_by_status_filters_correctly():
    doc = {
        "catalog_version": "1.0",
        "generated_at": "x",
        "metrics": [
            _valid_entry_dict(name="a", status="approved"),
            _valid_entry_dict(name="b", status="discovered_pending_review"),
            _valid_entry_dict(name="c", status="approved"),
        ],
    }
    catalog = Catalog.from_dict(doc)
    approved = catalog.by_status("approved")
    assert {e.name for e in approved} == {"a", "c"}


def test_catalog_by_category_filters_correctly():
    doc = {
        "catalog_version": "1.0",
        "generated_at": "x",
        "metrics": [
            _valid_entry_dict(name="a", category="node_hardware"),
            _valid_entry_dict(name="b", category="gpu_ai"),
        ],
    }
    catalog = Catalog.from_dict(doc)
    assert {e.name for e in catalog.by_category("gpu_ai")} == {"b"}


def test_catalog_by_exporter_filters_correctly():
    doc = {
        "catalog_version": "1.0",
        "generated_at": "x",
        "metrics": [
            _valid_entry_dict(name="a", exporter="node-exporter"),
            _valid_entry_dict(name="b", exporter="dcgm-exporter"),
        ],
    }
    catalog = Catalog.from_dict(doc)
    assert {e.name for e in catalog.by_exporter("dcgm-exporter")} == {"b"}
