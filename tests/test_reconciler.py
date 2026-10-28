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


# ---- discover_runtime_metadata: conflicting types ------------------------


def test_metadata_with_conflicting_types_across_targets_is_undetermined_not_first_wins():
    fake = _fake_response(200, {
        "status": "success",
        "data": {
            "acme_ambiguous": [
                {"type": "counter", "help": "from target A"},
                {"type": "gauge", "help": "from target B"},
            ],
            # Differing HELP with the SAME type is harmless and still resolves.
            "acme_help_differs": [
                {"type": "counter", "help": "one"},
                {"type": "counter", "help": "two"},
            ],
        },
    })
    with patch.object(rc, "_get_session") as mock_sess:
        mock_sess.return_value.get.return_value = fake
        meta = rc.discover_runtime_metadata("http://localhost:9090")
    assert meta["acme_ambiguous"].type is None
    assert meta["acme_help_differs"] == rc.RuntimeMetricInfo(type="counter", help="one")

    vendor = Catalog(catalog_version="1.0", generated_at="t", metrics=())
    report = rc.reconcile(vendor, {"acme_ambiguous"}, runtime_metadata=meta)
    assert report.catalog.get("acme_ambiguous") is None
    assert "acme_ambiguous" in report.undetermined_type_skipped


# ---- histogram / summary: pin what the reconciler does, no suffix magic ---
# Prometheus's HTTP API documentation does not say whether /api/v1/metadata
# keys histogram/summary metadata by the base family name or by the
# _bucket/_sum/_count series names, so this module makes no assumption: it
# neither strips suffixes nor synthesizes a base-family entry. These tests pin
# exactly that.


def test_histogram_series_names_without_metadata_under_their_own_name_are_skipped():
    vendor = Catalog(catalog_version="1.0", generated_at="t", metrics=())
    # Metadata is keyed by the BASE name only; the runtime names are the
    # suffixed series. Nothing is inferred across the two.
    meta = {"acme_latency_seconds": rc.RuntimeMetricInfo(type="histogram", help="latency")}
    names = {"acme_latency_seconds_bucket", "acme_latency_seconds_sum", "acme_latency_seconds_count"}
    report = rc.reconcile(vendor, names, runtime_metadata=meta)
    assert report.catalog.metrics == ()
    assert set(report.undetermined_type_skipped) == names
    assert report.newly_discovered == ()


def test_histogram_and_summary_types_are_accepted_when_reported_under_the_exact_name():
    vendor = Catalog(catalog_version="1.0", generated_at="t", metrics=())
    meta = {
        "acme_hist": rc.RuntimeMetricInfo(type="histogram", help="h"),
        "acme_summ": rc.RuntimeMetricInfo(type="summary", help="s"),
    }
    report = rc.reconcile(vendor, {"acme_hist", "acme_summ"}, runtime_metadata=meta)
    assert report.catalog.get("acme_hist").type == "histogram"
    assert report.catalog.get("acme_summ").type == "summary"
    assert all(
        e.status == CatalogStatus.DISCOVERED_PENDING_REVIEW.value for e in report.catalog.metrics
    )


# ---- DiscoveryScope ------------------------------------------------------


def test_default_scope_allows_everything():
    assert rc.DiscoveryScope().allows("go_goroutines")


def test_exclude_pattern_removes_matches_and_wins_over_include():
    scope = rc.DiscoveryScope(include=("go_*", "node_*"), exclude=("go_*",))
    assert not scope.allows("go_goroutines")
    assert scope.allows("node_load1")
    assert not scope.allows("acme_other")  # include is non-empty and nothing matched


def test_scope_patterns_are_case_sensitive_and_blank_patterns_rejected():
    assert not rc.DiscoveryScope(include=("DCGM_*",)).allows("dcgm_temp")
    with pytest.raises(ValueError):
        rc.DiscoveryScope(exclude=("  ",))


def test_scope_filters_only_the_pending_queue_never_vendor_or_rejected_outcomes():
    vendor = Catalog(
        catalog_version="1.0", generated_at="t",
        metrics=(_entry("go_vendor_metric"),),  # matches the exclude glob below
    )
    previous = Catalog(
        catalog_version="1.0", generated_at="t0",
        metrics=(_entry("go_rejected", status=CatalogStatus.REJECTED.value, exporter="unknown", category=UNCATEGORIZED),),
    )
    meta = {
        "go_goroutines": rc.RuntimeMetricInfo(type="gauge", help="g"),
        "go_rejected": rc.RuntimeMetricInfo(type="gauge", help="g"),
        "acme_new": rc.RuntimeMetricInfo(type="gauge", help="n"),
    }
    report = rc.reconcile(
        vendor,
        {"go_vendor_metric", "go_goroutines", "go_rejected", "acme_new"},
        runtime_metadata=meta,
        previous_catalog=previous,
        discovery_scope=rc.DiscoveryScope(exclude=("go_*",)),
    )
    assert report.catalog.get("go_vendor_metric").status == CatalogStatus.APPROVED.value
    assert report.catalog.get("go_rejected").status == CatalogStatus.REJECTED.value
    assert report.catalog.get("go_goroutines") is None
    assert report.out_of_scope_skipped == ("go_goroutines",)
    assert report.newly_discovered == ("acme_new",)


def test_out_of_scope_names_do_not_appear_in_undetermined_list():
    vendor = Catalog(catalog_version="1.0", generated_at="t", metrics=())
    report = rc.reconcile(
        vendor, {"go_untyped"}, runtime_metadata={},
        discovery_scope=rc.DiscoveryScope(exclude=("go_*",)),
    )
    assert report.out_of_scope_skipped == ("go_untyped",)
    assert report.undetermined_type_skipped == ()


# ---- run_reconciliation: orchestration and failure handling ---------------


def _routes_session(names_resp, metadata_resp):
    """Session double that answers by URL suffix; a response that is an
    Exception instance is raised instead of returned."""
    def _get(url, timeout=None):
        resp = names_resp if url.endswith("/api/v1/label/__name__/values") else metadata_resp
        if isinstance(resp, Exception):
            raise resp
        return resp
    session = MagicMock()
    session.get.side_effect = _get
    return session


def _ok(data):
    return _fake_response(200, {"status": "success", "data": data})


def test_run_reconciliation_success_combines_both_discovery_calls():
    vendor = Catalog(catalog_version="1.0", generated_at="t", metrics=(_entry("node_load1"),))
    session = _routes_session(
        _ok(["node_load1", "acme_new"]),
        _ok({"acme_new": [{"type": "gauge", "help": "x"}]}),
    )
    with patch.object(rc, "_get_session", return_value=session):
        report = rc.run_reconciliation(vendor, "http://prom:9090", timeout=3)
    assert report.runtime_name_count == 2
    assert report.catalog.get("node_load1").status == CatalogStatus.APPROVED.value
    assert report.newly_discovered == ("acme_new",)
    assert all(c.kwargs.get("timeout") == 3 for c in session.get.call_args_list)


def test_run_reconciliation_name_discovery_failure_raises_and_names_the_endpoint():
    vendor = Catalog(catalog_version="1.0", generated_at="t", metrics=(_entry("node_load1"),))
    session = _routes_session(requests.exceptions.ConnectionError("refused"), _ok({}))
    with patch.object(rc, "_get_session", return_value=session):
        with pytest.raises(rc.ReconciliationSkipped, match=r"__name__/values"):
            rc.run_reconciliation(vendor, "http://prom:9090")


def test_run_reconciliation_metadata_failure_is_also_a_failure_not_partial_state():
    vendor = Catalog(catalog_version="1.0", generated_at="t", metrics=(_entry("node_load1"),))
    session = _routes_session(_ok(["node_load1"]), requests.exceptions.Timeout())
    with patch.object(rc, "_get_session", return_value=session):
        with pytest.raises(rc.ReconciliationSkipped, match=r"api/v1/metadata"):
            rc.run_reconciliation(vendor, "http://prom:9090")


def test_run_reconciliation_empty_success_is_not_a_failure():
    vendor = Catalog(catalog_version="1.0", generated_at="t", metrics=(_entry("node_load1"),))
    session = _routes_session(_ok([]), _ok({}))
    with patch.object(rc, "_get_session", return_value=session):
        report = rc.run_reconciliation(vendor, "http://prom:9090")
    assert report.runtime_name_count == 0
    assert report.catalog.get("node_load1").status == CatalogStatus.APPROVED_UNAVAILABLE.value
