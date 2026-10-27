"""
tests/test_catalog_review_reconcile.py

Reconciliation semantics once human decisions are in play: vendor catalog +
approved review records form the approved universe; persisted rejections are
an authoritative reconcile() input; live discovery decides availability.
Pure -- no HTTP, no filesystem.
"""
from __future__ import annotations

from app.catalog import reconciler as rc
from app.catalog import reviews as rv
from app.catalog.schema import Catalog, CatalogEntry, CatalogStatus, Priority

T = "2026-10-27T05:00:00Z"
APPROVED = CatalogStatus.APPROVED.value
UNAVAILABLE = CatalogStatus.APPROVED_UNAVAILABLE.value
PENDING = CatalogStatus.DISCOVERED_PENDING_REVIEW.value
REJECTED = CatalogStatus.REJECTED.value


def _vendor(*names) -> Catalog:
    return Catalog("1.0", "t", tuple(
        CatalogEntry(name=n, type="gauge", category="CPU", priority=Priority.REVIEW.value,
                     exporter="node-exporter", status=APPROVED,
                     reference_path="references/node-exporter/cpu.md")
        for n in names))


def _approval(name, type_="gauge"):
    return rv.ReviewRecord(
        name=name, decision="approved", decided_at=T, type=type_, help="h",
        category="Queues", exporter="node-exporter",
        reference_path="references/node-exporter/cpu.md",
    )


def _rejection(name, type_=None):
    return rv.ReviewRecord(name=name, decision="rejected", decided_at=T, type=type_)


def _meta(**types):
    return {n: rc.RuntimeMetricInfo(type=t, help="h") for n, t in types.items()}


def _reconcile(vendor, store, names, metadata=None, previous=None):
    universe, _ = rv.build_approved_universe(vendor, store)
    return rc.reconcile(
        universe, set(names), runtime_metadata=metadata or {},
        previous_catalog=previous, rejections=store.rejections(),
    )


def _status(report, name):
    entry = report.catalog.get(name)
    return entry.status if entry else None


# ---- the required-behavior table -----------------------------------------


def test_known_vendor_metric_present_is_approved():
    r = _reconcile(_vendor("node_load1"), rv.ReviewStore(), {"node_load1"})
    assert _status(r, "node_load1") == APPROVED


def test_known_vendor_metric_absent_from_successful_discovery_is_approved_unavailable():
    r = _reconcile(_vendor("node_load1"), rv.ReviewStore(), set())
    assert _status(r, "node_load1") == UNAVAILABLE


def test_new_metric_with_valid_type_and_no_decision_is_pending_review():
    r = _reconcile(_vendor(), rv.ReviewStore(), {"acme_new"}, _meta(acme_new="counter"))
    assert _status(r, "acme_new") == PENDING


def test_explicitly_rejected_metric_is_rejected_whether_vendor_or_runtime_only():
    store = rv.ReviewStore((_rejection("node_load1"), _rejection("acme_new", "counter")))
    present = _reconcile(_vendor("node_load1"), store, {"node_load1", "acme_new"}, _meta(acme_new="counter"))
    absent = _reconcile(_vendor("node_load1"), store, set())
    assert _status(present, "node_load1") == REJECTED and _status(present, "acme_new") == REJECTED
    assert _status(absent, "node_load1") == REJECTED
    assert set(present.kept_rejected) == {"node_load1", "acme_new"}
    assert "acme_new" not in present.newly_discovered


def test_approved_discovery_present_at_runtime_is_approved_with_its_review_metadata():
    store = rv.ReviewStore((_approval("acme_queue_depth"),))
    r = _reconcile(_vendor("node_load1"), store, {"node_load1", "acme_queue_depth"},
                   _meta(acme_queue_depth="gauge"))
    entry = r.catalog.get("acme_queue_depth")
    assert entry.status == APPROVED
    assert (entry.exporter, entry.category, entry.reference_path) == (
        "node-exporter", "Queues", "references/node-exporter/cpu.md")
    assert "acme_queue_depth" not in r.newly_discovered


def test_approved_discovery_absent_follows_the_approved_but_unavailable_policy():
    store = rv.ReviewStore((_approval("acme_queue_depth"),))
    r = _reconcile(_vendor("node_load1"), store, {"node_load1"})
    assert _status(r, "acme_queue_depth") == UNAVAILABLE
    assert r.became_unavailable == ("acme_queue_depth",)


def test_approved_discovery_reappearing_is_flagged_available_again():
    store = rv.ReviewStore((_approval("acme_queue_depth"),))
    down = _reconcile(_vendor(), store, set())
    up = _reconcile(_vendor(), store, {"acme_queue_depth"}, previous=down.catalog)
    assert _status(up, "acme_queue_depth") == APPROVED
    assert up.became_available_again == ("acme_queue_depth",)


def test_unresolved_type_is_still_skipped_never_guessed():
    r = _reconcile(_vendor(), rv.ReviewStore(), {"acme_mystery"})
    assert r.catalog.get("acme_mystery") is None
    assert r.undetermined_type_skipped == ("acme_mystery",)


def test_discovery_failure_still_refuses_to_reconcile_even_with_decisions():
    import pytest
    store = rv.ReviewStore((_rejection("node_load1"),))
    universe, _ = rv.build_approved_universe(_vendor("node_load1"), store)
    with pytest.raises(rc.ReconciliationSkipped):
        rc.reconcile(universe, None, rejections=store.rejections())


# ---- rejection persistence across repeated reconciliation -----------------


def test_rejection_survives_repeated_reconciliation_runs_feeding_the_previous_snapshot():
    store = rv.ReviewStore((_rejection("node_load1"), _rejection("acme_new", "counter")))
    names = {"node_load1", "acme_new", "acme_other"}
    meta = _meta(acme_new="counter", acme_other="gauge")
    first = _reconcile(_vendor("node_load1"), store, names, meta)
    second = _reconcile(_vendor("node_load1"), store, names, meta, previous=first.catalog)
    third = _reconcile(_vendor("node_load1"), store, set(), meta, previous=second.catalog)
    for report in (first, second):
        assert _status(report, "node_load1") == REJECTED
        assert _status(report, "acme_new") == REJECTED
        assert _status(report, "acme_other") == PENDING
    assert _status(third, "node_load1") == REJECTED


def test_a_pending_metric_is_never_promoted_by_repetition_or_unrelated_decisions():
    store = rv.ReviewStore((_rejection("other", "gauge"),))
    first = _reconcile(_vendor(), store, {"acme_new"}, _meta(acme_new="gauge"))
    again = _reconcile(_vendor(), store, {"acme_new"}, _meta(acme_new="gauge"), previous=first.catalog)
    assert _status(first, "acme_new") == PENDING == _status(again, "acme_new")


def test_rejected_runtime_only_metric_uses_stored_type_when_runtime_metadata_is_missing():
    store = rv.ReviewStore((_rejection("acme_new", "histogram"),))
    r = _reconcile(_vendor(), store, {"acme_new"}, metadata={})
    entry = r.catalog.get("acme_new")
    assert entry.status == REJECTED and entry.type == "histogram"
    assert "acme_new" not in r.undetermined_type_skipped


# ---- store-authoritative rejections ---------------------------------------


def test_a_cleared_rejection_is_not_resurrected_from_the_previous_snapshot():
    store_with = rv.ReviewStore((_rejection("node_load1"),))
    before = _reconcile(_vendor("node_load1"), store_with, {"node_load1"})
    assert _status(before, "node_load1") == REJECTED
    after = _reconcile(_vendor("node_load1"), rv.ReviewStore(), {"node_load1"}, previous=before.catalog)
    assert _status(after, "node_load1") == APPROVED


def test_without_a_rejections_input_the_original_previous_catalog_behavior_is_unchanged():
    previous = Catalog("1.0", "t0", (CatalogEntry(
        name="node_load1", type="gauge", category="CPU", priority="Review",
        exporter="node-exporter", status=REJECTED),))
    r = rc.reconcile(_vendor("node_load1"), {"node_load1"}, previous_catalog=previous)
    assert _status(r, "node_load1") == REJECTED


def test_vendor_entry_wins_over_a_redundant_approval_and_both_paths_agree_on_status():
    store = rv.ReviewStore((_approval("node_load1"),))
    r = _reconcile(_vendor("node_load1"), store, {"node_load1"})
    assert r.catalog.get("node_load1").category == "CPU"
    assert _status(r, "node_load1") == APPROVED
    assert len([m for m in r.catalog.metrics if m.name == "node_load1"]) == 1
