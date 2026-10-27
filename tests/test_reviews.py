"""
tests/test_reviews.py

app/catalog/reviews.py: record/store validation, fail-closed loading, atomic
persistence, approval validation, rejection persistence, idempotent repeated
operations, and vendor/review precedence.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from app.catalog import reviews as rv
from app.catalog.reconciler import RuntimeMetricInfo
from app.catalog.schema import Catalog, CatalogEntry, CatalogStatus, Priority

T1 = "2026-10-27T05:00:00Z"
T2 = "2026-10-27T06:00:00Z"


@pytest.fixture
def skills(tmp_path) -> Path:
    root = tmp_path / "skills"
    (root / "references" / "node-exporter").mkdir(parents=True)
    (root / "references" / "node-exporter" / "overview.md").write_text("o")
    (root / "references" / "node-exporter" / "cpu.md").write_text("c")
    (root / "references" / "node-exporter" / "node-fundamentals.md").write_text("f")
    (root / "references" / "prometheus-fundamentals.md").write_text("f")
    return root


def _approved_dict(**kw):
    d = {
        "name": "acme_queue_depth", "decision": "approved", "decided_at": T1, "type": "gauge",
        "category": "Queues", "exporter": "node-exporter",
        "reference_path": "references/node-exporter/cpu.md",
    }
    d.update(kw)
    return d


def _write(path: Path, decisions, version=1):
    path.write_text(json.dumps({"schema_version": version, "decisions": decisions}))


def _approve(store, skills, name="acme_queue_depth", **kw):
    args = dict(
        name=name, category="Queues", exporter="node-exporter",
        reference_path="references/node-exporter/cpu.md",
        discovered=RuntimeMetricInfo(type="gauge", help="depth"),
        vendor_names=frozenset(), skills_root=skills, now=T1,
    )
    args.update(kw)
    return rv.approve(store, **args)


# ---- record validation ----------------------------------------------------


@pytest.mark.parametrize("missing", ["category", "exporter", "reference_path", "type"])
def test_approval_requires_each_piece_of_integration_metadata(missing):
    d = _approved_dict()
    del d[missing]
    with pytest.raises(rv.ReviewRecordError):
        rv.ReviewRecord.from_dict(d)


@pytest.mark.parametrize("field,value", [("category", "uncategorized"), ("exporter", "unknown"), ("category", "  ")])
def test_approval_rejects_placeholder_metadata(field, value):
    with pytest.raises(rv.ReviewRecordError):
        rv.ReviewRecord.from_dict(_approved_dict(**{field: value}))


def test_rejection_must_not_carry_approval_metadata():
    with pytest.raises(rv.ReviewRecordError, match="ambiguous"):
        rv.ReviewRecord.from_dict(
            {"name": "m", "decision": "rejected", "decided_at": T1, "category": "CPU"}
        )


@pytest.mark.parametrize(
    "bad",
    [
        {"name": "has space", "decision": "rejected", "decided_at": T1},
        {"name": "m", "decision": "maybe", "decided_at": T1},
        {"name": "m", "decision": "rejected", "decided_at": "yesterday"},
        {"name": "m", "decision": "rejected", "decided_at": T1, "type": "untyped"},
        {"name": "m", "decision": "rejected", "decided_at": T1, "surprise": "x"},
        {"name": "m", "decision": "rejected"},
        {"name": "m", "decision": "rejected", "decided_at": 5},
    ],
)
def test_invalid_records_are_rejected(bad):
    with pytest.raises(rv.ReviewRecordError):
        rv.ReviewRecord.from_dict(bad)


# ---- load: fail closed ----------------------------------------------------


def test_missing_file_is_an_empty_store(tmp_path):
    assert rv.load_reviews(tmp_path / "reviews.json").records == ()


@pytest.mark.parametrize("content", ["{not json", "[]", '{"schema_version": 1}', '{"schema_version": 2, "decisions": []}',
                                      '{"schema_version": 1, "decisions": [], "extra": 1}', ""])
def test_malformed_store_raises_instead_of_loading_empty(tmp_path, content):
    path = tmp_path / "reviews.json"
    path.write_text(content)
    with pytest.raises(rv.ReviewStoreError):
        rv.load_reviews(path)


def test_one_bad_record_fails_the_whole_file_nothing_is_dropped(tmp_path):
    path = tmp_path / "reviews.json"
    _write(path, [_approved_dict(), {"name": "x", "decision": "bogus", "decided_at": T1}])
    with pytest.raises(rv.ReviewStoreError):
        rv.load_reviews(path)


def test_duplicate_and_conflicting_records_fail_closed(tmp_path):
    path = tmp_path / "reviews.json"
    _write(path, [
        {"name": "m", "decision": "rejected", "decided_at": T1},
        _approved_dict(name="m"),
    ])
    with pytest.raises(rv.ReviewStoreError, match="more than once"):
        rv.load_reviews(path)


# ---- save: atomic, deterministic ------------------------------------------


def test_round_trip_is_deterministic_and_sorted(tmp_path, skills):
    path = tmp_path / "reviews.json"
    store = _approve(rv.ReviewStore(), skills, name="zz_metric").store
    store = _approve(store, skills, name="aa_metric").store
    rv.save_reviews(store, path)
    first = path.read_bytes()
    loaded = rv.load_reviews(path)
    assert [r.name for r in loaded.records] == ["aa_metric", "zz_metric"]
    rv.save_reviews(loaded, path)
    assert path.read_bytes() == first
    assert oct(path.stat().st_mode & 0o777) == "0o644"


def test_interrupted_write_leaves_the_old_file_intact_and_no_temp_files(tmp_path, skills):
    path = tmp_path / "reviews.json"
    rv.save_reviews(_approve(rv.ReviewStore(), skills).store, path)
    before = path.read_bytes()
    bigger = _approve(rv.load_reviews(path), skills, name="other_metric").store
    with patch("app.catalog.reviews.os.replace", side_effect=OSError("disk went away")):
        with pytest.raises(OSError):
            rv.save_reviews(bigger, path)
    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir() if p.name != "skills"] == ["reviews.json"]


def test_failure_while_writing_the_temp_file_leaves_target_untouched(tmp_path, skills):
    path = tmp_path / "reviews.json"
    rv.save_reviews(_approve(rv.ReviewStore(), skills).store, path)
    before = path.read_bytes()
    with patch("app.catalog.reviews.os.fsync", side_effect=OSError("io error")):
        with pytest.raises(OSError):
            rv.save_reviews(rv.ReviewStore(), path)
    assert path.read_bytes() == before
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


# ---- approval validation --------------------------------------------------


def test_valid_approval_is_recorded_with_prometheus_type_and_help(skills):
    outcome = _approve(rv.ReviewStore(), skills)
    assert outcome.action == "added"
    rec = outcome.store.get("acme_queue_depth")
    assert (rec.type, rec.help, rec.category, rec.exporter) == ("gauge", "depth", "Queues", "node-exporter")


def test_approval_refused_without_a_prometheus_reported_type(skills):
    with pytest.raises(rv.ReviewOperationError, match="not be guessed"):
        _approve(rv.ReviewStore(), skills, discovered=RuntimeMetricInfo(type=None, help="x"))


@pytest.mark.parametrize(
    "ref,exporter,why",
    [
        ("references/node-exporter/missing.md", "node-exporter", "does not exist"),
        ("references/node-exporter/overview.md", "node-exporter", "domain reference"),
        ("references/node-exporter/node-fundamentals.md", "node-exporter", "domain reference"),
        ("references/prometheus-fundamentals.md", "node-exporter", "must look like"),
        ("../etc/passwd", "node-exporter", "must look like"),
        ("/abs/references/node-exporter/cpu.md", "node-exporter", "must look like"),
        ("references/node-exporter/cpu.md", "dcgm-exporter", "does not match"),
    ],
)
def test_approval_refuses_bad_reference_paths(skills, ref, exporter, why):
    with pytest.raises(rv.ReviewOperationError, match=why):
        _approve(rv.ReviewStore(), skills, reference_path=ref, exporter=exporter)


def test_approval_refuses_exporter_without_overview(skills):
    (skills / "references" / "node-exporter" / "overview.md").unlink()
    with pytest.raises(rv.ReviewOperationError, match="overview.md"):
        _approve(rv.ReviewStore(), skills)


def test_approval_of_a_vendor_metric_is_refused(skills):
    with pytest.raises(rv.ReviewOperationError, match="vendor catalog"):
        _approve(rv.ReviewStore(), skills, vendor_names=frozenset({"acme_queue_depth"}))


# ---- repeated operations --------------------------------------------------


def test_repeating_an_identical_approval_is_unchanged_and_keeps_the_original_timestamp(skills):
    first = _approve(rv.ReviewStore(), skills, now=T1)
    again = _approve(first.store, skills, now=T2, note="different note", discovered=RuntimeMetricInfo("gauge", "new help"))
    assert again.action == "unchanged"
    assert again.store is first.store
    assert again.store.get("acme_queue_depth").decided_at == T1


def test_changing_an_approval_requires_explicit_replace(skills):
    first = _approve(rv.ReviewStore(), skills)
    with pytest.raises(rv.ReviewOperationError, match="--replace"):
        _approve(first.store, skills, category="Other")
    replaced = _approve(first.store, skills, category="Other", replace=True, now=T2)
    assert replaced.action == "replaced"
    assert replaced.store.get("acme_queue_depth").category == "Other"


# ---- rejection persistence ------------------------------------------------


def test_reject_discovered_metric_stores_type_snapshot():
    out = rv.reject(rv.ReviewStore(), name="go_goroutines", discovered=RuntimeMetricInfo("gauge", "g"),
                    vendor_names=frozenset(), now=T1)
    rec = out.store.get("go_goroutines")
    assert (rec.decision, rec.type) == ("rejected", "gauge")
    assert out.store.rejections()["go_goroutines"].type == "gauge"


def test_reject_vendor_metric_needs_no_discovery():
    out = rv.reject(rv.ReviewStore(), name="node_load1", discovered=None,
                    vendor_names=frozenset({"node_load1"}), now=T1)
    assert out.store.get("node_load1").type is None


def test_reject_unknown_metric_is_refused_so_typos_make_no_phantom_records():
    with pytest.raises(rv.ReviewOperationError, match="nothing to reject"):
        rv.reject(rv.ReviewStore(), name="typo_metric", discovered=None, vendor_names=frozenset())
    with pytest.raises(rv.ReviewOperationError):
        rv.reject(rv.ReviewStore(), name="typo_metric", discovered=RuntimeMetricInfo(None, "x"), vendor_names=frozenset())


def test_repeated_rejection_is_unchanged_and_conflicting_flip_needs_replace(skills):
    base = rv.reject(rv.ReviewStore(), name="m_a", discovered=RuntimeMetricInfo("gauge", "a"), vendor_names=frozenset(), now=T1)
    again = rv.reject(base.store, name="m_a", discovered=RuntimeMetricInfo("gauge", "changed help"), vendor_names=frozenset(), now=T2)
    assert again.action == "unchanged" and again.store.get("m_a").decided_at == T1
    with pytest.raises(rv.ReviewOperationError, match="--replace"):
        _approve(base.store, skills, name="m_a")
    flipped = _approve(base.store, skills, name="m_a", replace=True, now=T2)
    assert flipped.store.get("m_a").decision == "approved"


def test_clear_removes_a_decision_and_unknown_clear_is_refused():
    base = rv.reject(rv.ReviewStore(), name="node_load1", discovered=None, vendor_names=frozenset({"node_load1"}), now=T1)
    assert rv.clear(base.store, name="node_load1").store.records == ()
    with pytest.raises(rv.ReviewOperationError):
        rv.clear(rv.ReviewStore(), name="node_load1")


# ---- precedence: approved universe ----------------------------------------


def _vendor(*names) -> Catalog:
    return Catalog("1.0", "t", tuple(
        CatalogEntry(name=n, type="gauge", category="CPU", priority=Priority.REVIEW.value,
                     exporter="node-exporter", status="approved", reference_path="references/node-exporter/cpu.md")
        for n in names))


def test_approved_review_joins_the_universe_as_an_approved_entry(skills):
    store = _approve(rv.ReviewStore(), skills).store
    universe, superseded = rv.build_approved_universe(_vendor("node_load1"), store)
    entry = universe.get("acme_queue_depth")
    assert entry.status == CatalogStatus.APPROVED.value
    assert (entry.exporter, entry.reference_path, entry.type) == ("node-exporter", "references/node-exporter/cpu.md", "gauge")
    assert universe.get("node_load1") is not None and superseded == ()


def test_vendor_entry_wins_over_a_redundant_approval_which_is_reported_not_dropped(tmp_path):
    path = tmp_path / "reviews.json"
    _write(path, [_approved_dict(name="node_load1", category="Different")])
    store = rv.load_reviews(path)  # still on disk and loadable
    universe, superseded = rv.build_approved_universe(_vendor("node_load1"), store)
    assert superseded == ("node_load1",)
    assert universe.get("node_load1").category == "CPU"  # vendor definition, not the record's
    assert len(universe.metrics) == 1
    assert store.get("node_load1") is not None


def test_rejection_is_not_applied_by_the_universe_builder_it_is_a_reconcile_input():
    store = rv.reject(rv.ReviewStore(), name="node_load1", discovered=None, vendor_names=frozenset({"node_load1"}), now=T1).store
    universe, _ = rv.build_approved_universe(_vendor("node_load1"), store)
    assert universe.get("node_load1").status == "approved"
    assert "node_load1" in store.rejections()
