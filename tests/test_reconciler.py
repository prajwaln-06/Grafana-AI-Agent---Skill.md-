"""
tests/test_reconciler.py

Phase 4 tests: runtime discovery HTTP behavior (mocked, same convention as
tests/test_prometheus_client.py) and the reconcile() status-transition
logic (pure, no HTTP -- runtime_names/runtime_metadata are passed in
directly).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from app.catalog import reconciler as rc
from app.catalog.schema import Catalog, CatalogEntry, CatalogStatus, Priority, UNCATEGORIZED


def _fake_response(status_code=200, json_body=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body or {}
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(f"{status_code}")
    return resp


def _entry(name, status=CatalogStatus.APPROVED.value, type_="gauge", **kw):
    return CatalogEntry(
        name=name,
        type=type_,
        category=kw.pop("category", "Test Category"),
        priority=kw.pop("priority", Priority.REVIEW.value),
        exporter=kw.pop("exporter", "node-exporter"),
        status=status,
        help=kw.pop("help", "help text"),
        **kw,
    )


# ---- discover_runtime_metric_names --------------------------------------


def test_discover_runtime_metric_names_success():
    fake = _fake_response(200, {"status": "success", "data": ["node_load1", "node_load5"]})
    with patch.object(rc, "_get_session") as mock_sess:
        mock_sess.return_value.get.return_value = fake
        names = rc.discover_runtime_metric_names("http://localhost:9090")
    assert names == {"node_load1", "node_load5"}


def test_discover_runtime_metric_names_connection_error_returns_none():
    with patch.object(rc, "_get_session") as mock_sess:
        mock_sess.return_value.get.side_effect = requests.exceptions.ConnectionError("refused")
        assert rc.discover_runtime_metric_names("http://localhost:9090") is None


def test_discover_runtime_metric_names_bad_status_returns_none():
    fake = _fake_response(200, {"status": "error"})
    with patch.object(rc, "_get_session") as mock_sess:
        mock_sess.return_value.get.return_value = fake
        assert rc.discover_runtime_metric_names("http://localhost:9090") is None


def test_discover_runtime_metric_names_non_json_returns_none():
    fake = MagicMock()
    fake.status_code = 200
    fake.raise_for_status = MagicMock()
    fake.json.side_effect = ValueError("bad json")
    with patch.object(rc, "_get_session") as mock_sess:
        mock_sess.return_value.get.return_value = fake
        assert rc.discover_runtime_metric_names("http://localhost:9090") is None


# ---- discover_runtime_metadata -------------------------------------------


def test_discover_runtime_metadata_success():
    fake = _fake_response(200, {
        "status": "success",
        "data": {
            "node_load1": [{"type": "gauge", "help": "1m load average"}],
            "acme_new_metric": [{"type": "counter", "help": "New thing"}],
        },
    })
    with patch.object(rc, "_get_session") as mock_sess:
        mock_sess.return_value.get.return_value = fake
        meta = rc.discover_runtime_metadata("http://localhost:9090")
    assert meta["node_load1"] == rc.RuntimeMetricInfo(type="gauge", help="1m load average")
    assert meta["acme_new_metric"].type == "counter"


def test_discover_runtime_metadata_failure_returns_none():
    with patch.object(rc, "_get_session") as mock_sess:
        mock_sess.return_value.get.side_effect = requests.exceptions.Timeout()
        assert rc.discover_runtime_metadata("http://localhost:9090") is None


# ---- reconcile(): fail-closed on discovery failure -----------------------


def test_reconcile_raises_when_runtime_names_is_none():
    vendor = Catalog(catalog_version="1.0", generated_at="t", metrics=(_entry("node_load1"),))
    with pytest.raises(rc.ReconciliationSkipped):
        rc.reconcile(vendor, runtime_names=None)


# ---- reconcile(): vendor-side status transitions -------------------------


def test_vendor_metric_present_at_runtime_becomes_approved():
    vendor = Catalog(
        catalog_version="1.0", generated_at="t",
        metrics=(_entry("node_load1", status=CatalogStatus.APPROVED.value),),
    )
    report = rc.reconcile(vendor, runtime_names={"node_load1"})
    assert report.catalog.get("node_load1").status == CatalogStatus.APPROVED.value
    assert report.became_unavailable == ()


def test_vendor_metric_absent_at_runtime_becomes_approved_unavailable():
    vendor = Catalog(
        catalog_version="1.0", generated_at="t",
        metrics=(_entry("node_load1", status=CatalogStatus.APPROVED.value),),
    )
    report = rc.reconcile(vendor, runtime_names=set())
    assert report.catalog.get("node_load1").status == CatalogStatus.APPROVED_UNAVAILABLE.value
    assert report.became_unavailable == ("node_load1",)


def test_previously_unavailable_metric_reappearing_is_flagged_available_again():
    vendor = Catalog(
        catalog_version="1.0", generated_at="t",
        metrics=(_entry("node_load1", status=CatalogStatus.APPROVED.value),),
    )
    previous = Catalog(
        catalog_version="1.0", generated_at="t0",
        metrics=(_entry("node_load1", status=CatalogStatus.APPROVED_UNAVAILABLE.value),),
    )
    report = rc.reconcile(vendor, runtime_names={"node_load1"}, previous_catalog=previous)
    assert report.catalog.get("node_load1").status == CatalogStatus.APPROVED.value
    assert report.became_available_again == ("node_load1",)


def test_rejected_vendor_metric_stays_rejected_even_if_present_at_runtime():
    vendor = Catalog(
        catalog_version="1.0", generated_at="t",
        metrics=(_entry("node_load1", status=CatalogStatus.APPROVED.value),),
    )
    previous = Catalog(
        catalog_version="1.0", generated_at="t0",
        metrics=(_entry("node_load1", status=CatalogStatus.REJECTED.value),),
    )
    report = rc.reconcile(vendor, runtime_names={"node_load1"}, previous_catalog=previous)
    assert report.catalog.get("node_load1").status == CatalogStatus.REJECTED.value
    assert "node_load1" in report.kept_rejected


def test_rejected_vendor_metric_stays_rejected_even_if_absent_at_runtime():
    vendor = Catalog(
        catalog_version="1.0", generated_at="t",
        metrics=(_entry("node_load1", status=CatalogStatus.APPROVED.value),),
    )
    previous = Catalog(
        catalog_version="1.0", generated_at="t0",
        metrics=(_entry("node_load1", status=CatalogStatus.REJECTED.value),),
    )
    report = rc.reconcile(vendor, runtime_names=set(), previous_catalog=previous)
    assert report.catalog.get("node_load1").status == CatalogStatus.REJECTED.value


# ---- reconcile(): runtime-only discovery (path 2) ------------------------


def test_runtime_only_metric_with_valid_type_becomes_discovered_pending_review():
    vendor = Catalog(catalog_version="1.0", generated_at="t", metrics=())
    meta = {"acme_new_metric": rc.RuntimeMetricInfo(type="counter", help="A new thing")}
    report = rc.reconcile(vendor, runtime_names={"acme_new_metric"}, runtime_metadata=meta)
    entry = report.catalog.get("acme_new_metric")
    assert entry is not None
    assert entry.status == CatalogStatus.DISCOVERED_PENDING_REVIEW.value
    assert entry.type == "counter"
    assert entry.help == "A new thing"
    assert entry.category == UNCATEGORIZED
    assert entry.priority == Priority.REVIEW.value
    assert entry.exporter == "unknown"
    assert "acme_new_metric" in report.newly_discovered


def test_runtime_only_metric_never_auto_promoted_to_approved():
    """The core Phase 4 invariant: a metric Prometheus exposes but that
    was never in the vendor-approved catalog must NEVER come out of
    reconcile() with status 'approved'."""
    vendor = Catalog(catalog_version="1.0", generated_at="t", metrics=())
    meta = {"acme_new_metric": rc.RuntimeMetricInfo(type="gauge", help="x")}
    report = rc.reconcile(vendor, runtime_names={"acme_new_metric"}, runtime_metadata=meta)
    assert report.catalog.get("acme_new_metric").status != CatalogStatus.APPROVED.value


def test_runtime_only_metric_without_resolvable_type_is_skipped_not_guessed():
    vendor = Catalog(catalog_version="1.0", generated_at="t", metrics=())
    # No metadata at all for this metric name.
    report = rc.reconcile(vendor, runtime_names={"acme_mystery_metric"})
    assert report.catalog.get("acme_mystery_metric") is None
    assert "acme_mystery_metric" in report.undetermined_type_skipped


def test_runtime_only_metric_with_unrecognized_type_string_is_skipped():
    vendor = Catalog(catalog_version="1.0", generated_at="t", metrics=())
    meta = {"acme_untyped_metric": rc.RuntimeMetricInfo(type="untyped", help="x")}
    report = rc.reconcile(vendor, runtime_names={"acme_untyped_metric"}, runtime_metadata=meta)
    assert report.catalog.get("acme_untyped_metric") is None
    assert "acme_untyped_metric" in report.undetermined_type_skipped


def test_previously_rejected_runtime_only_metric_stays_rejected_not_pending_review():
    vendor = Catalog(catalog_version="1.0", generated_at="t", metrics=())
    previous = Catalog(
        catalog_version="1.0", generated_at="t0",
        metrics=(_entry("acme_new_metric", status=CatalogStatus.REJECTED.value, exporter="unknown", category=UNCATEGORIZED),),
    )
    meta = {"acme_new_metric": rc.RuntimeMetricInfo(type="counter", help="x")}
    report = rc.reconcile(
        vendor, runtime_names={"acme_new_metric"}, runtime_metadata=meta, previous_catalog=previous,
    )
    entry = report.catalog.get("acme_new_metric")
    assert entry.status == CatalogStatus.REJECTED.value
    assert "acme_new_metric" not in report.newly_discovered


# ---- reconcile(): overall result is a valid Catalog ----------------------


def test_reconciled_result_has_no_duplicate_names_and_is_a_valid_catalog():
    vendor = Catalog(
        catalog_version="1.0", generated_at="t",
        metrics=(_entry("node_load1"), _entry("node_load5")),
    )
    meta = {"acme_new_metric": rc.RuntimeMetricInfo(type="gauge", help="x")}
    report = rc.reconcile(
        vendor,
        runtime_names={"node_load1", "acme_new_metric"},
        runtime_metadata=meta,
    )
    names = [m.name for m in report.catalog.metrics]
    assert len(names) == len(set(names))
    assert set(names) == {"node_load1", "node_load5", "acme_new_metric"}
