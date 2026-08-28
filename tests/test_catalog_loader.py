import json

import pytest

from app.catalog.loader import CatalogIndex, CatalogLoadError, load_catalog


def _write_catalog(path, metrics=None, catalog_version="1.0", generated_at="2026-08-27T00:00:00Z"):
    doc = {
        "catalog_version": catalog_version,
        "generated_at": generated_at,
        "metrics": metrics if metrics is not None else [],
    }
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _sample_metric(**overrides):
    data = {
        "name": "node_load1",
        "type": "gauge",
        "help": "1m load average.",
        "unit": None,
        "keywords": ["load"],
        "category": "node_hardware",
        "priority": "High",
        "exporter": "node-exporter",
        "status": "approved",
        "reference_path": None,
        "dimensions": [],
    }
    data.update(overrides)
    return data


# ---- happy path -------------------------------------------------------


def test_load_catalog_reads_valid_file(tmp_path):
    catalog_path = tmp_path / "catalog.json"
    _write_catalog(catalog_path, metrics=[_sample_metric()])

    catalog = load_catalog(catalog_path)

    assert catalog.catalog_version == "1.0"
    assert len(catalog.metrics) == 1
    assert catalog.metrics[0].name == "node_load1"


def test_load_catalog_with_empty_metrics_list_is_valid(tmp_path):
    catalog_path = tmp_path / "catalog.json"
    _write_catalog(catalog_path, metrics=[])

    catalog = load_catalog(catalog_path)

    assert catalog.metrics == ()


# ---- failure modes: file-level --------------------------------------------


def test_load_catalog_missing_file_raises(tmp_path):
    missing_path = tmp_path / "does_not_exist.json"
    with pytest.raises(CatalogLoadError):
        load_catalog(missing_path)


def test_load_catalog_invalid_json_raises(tmp_path):
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(CatalogLoadError):
        load_catalog(catalog_path)


def test_load_catalog_top_level_not_an_object_raises(tmp_path):
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(json.dumps(["a", "list", "not", "a", "dict"]), encoding="utf-8")
    with pytest.raises(CatalogLoadError):
        load_catalog(catalog_path)


# ---- failure modes: schema-level, surfaced as CatalogLoadError -----------


def test_load_catalog_missing_required_top_level_field_raises(tmp_path):
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(
        json.dumps({"catalog_version": "1.0", "metrics": []}), encoding="utf-8"
    )
    with pytest.raises(CatalogLoadError):
        load_catalog(catalog_path)


def test_load_catalog_duplicate_metric_name_raises(tmp_path):
    catalog_path = tmp_path / "catalog.json"
    _write_catalog(catalog_path, metrics=[_sample_metric(), _sample_metric()])
    with pytest.raises(CatalogLoadError):
        load_catalog(catalog_path)


def test_load_catalog_invalid_entry_enum_value_raises(tmp_path):
    catalog_path = tmp_path / "catalog.json"
    _write_catalog(catalog_path, metrics=[_sample_metric(status="totally_approved")])
    with pytest.raises(CatalogLoadError):
        load_catalog(catalog_path)


# ---- all 4 required-but-invalid-status states parse correctly when valid -


@pytest.mark.parametrize(
    "status",
    ["approved", "approved_unavailable", "discovered_pending_review", "rejected"],
)
def test_load_catalog_accepts_every_valid_status(tmp_path, status):
    catalog_path = tmp_path / "catalog.json"
    _write_catalog(catalog_path, metrics=[_sample_metric(status=status)])
    catalog = load_catalog(catalog_path)
    assert catalog.metrics[0].status == status


# ---- CatalogIndex -----------------------------------------------------


def test_catalog_index_load_exposes_catalog_and_path(tmp_path):
    catalog_path = tmp_path / "catalog.json"
    _write_catalog(catalog_path, metrics=[_sample_metric()])

    index = CatalogIndex.load(catalog_path)

    assert index.path == catalog_path
    assert len(index.catalog.metrics) == 1


def test_catalog_index_load_missing_file_raises(tmp_path):
    with pytest.raises(CatalogLoadError):
        CatalogIndex.load(tmp_path / "missing.json")


def test_catalog_index_reload_picks_up_changes(tmp_path):
    catalog_path = tmp_path / "catalog.json"
    _write_catalog(catalog_path, metrics=[_sample_metric()])
    index = CatalogIndex.load(catalog_path)
    assert len(index.catalog.metrics) == 1

    _write_catalog(
        catalog_path,
        metrics=[_sample_metric(), _sample_metric(name="node_cpu_seconds_total", type="counter")],
    )
    index.reload()

    assert len(index.catalog.metrics) == 2


def test_catalog_index_reload_does_not_pick_up_changes_without_calling_reload(tmp_path):
    """Mirrors test_adding_a_new_routing_row_requires_reload_not_picked_up_live
    in tests/test_skill_index.py: a loaded index is a point-in-time snapshot,
    not a live filesystem view, until reload() is explicitly called."""
    catalog_path = tmp_path / "catalog.json"
    _write_catalog(catalog_path, metrics=[_sample_metric()])
    index = CatalogIndex.load(catalog_path)

    _write_catalog(catalog_path, metrics=[_sample_metric(), _sample_metric(name="node_load5")])

    assert len(index.catalog.metrics) == 1  # unchanged until reload()


def test_catalog_index_reload_with_broken_file_raises_and_keeps_old_catalog(tmp_path):
    """A failed reload must not tear down previously-good, in-memory state --
    same fail-closed-without-losing-working-state principle CatalogIndex.reload's
    docstring describes."""
    catalog_path = tmp_path / "catalog.json"
    _write_catalog(catalog_path, metrics=[_sample_metric()])
    index = CatalogIndex.load(catalog_path)

    catalog_path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(CatalogLoadError):
        index.reload()

    # old, valid catalog is still in place
    assert len(index.catalog.metrics) == 1
    assert index.catalog.metrics[0].name == "node_load1"
